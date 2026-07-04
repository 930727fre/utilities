"""Filesystem-based state layer. Replaces the old SQLite `db.py`.

Design contract:
  * The filesystem under `/data` IS the state. No RDBMS. No migrations.
  * Rollback = `rm` / `mv` — no SQL, no schema evolution.
  * Directory placement encodes status: `errors/active/*.md` vs
    `errors/graduated/*.md`, `roleplays/active/*.md` vs `roleplays/done/*.md`.
    The "only one active roleplay" invariant is enforced by
    `roleplays/active/` containing at-most-one file, no partial-unique-index
    contortion needed.
  * Human-editable payloads (errors, roleplays) use YAML front-matter +
    markdown body. Machine-only payloads (sessions raw_response, drills)
    use plain JSON.
  * Session decisions land in an append-only jsonl beside the metadata —
    every swipe = one line, race-free without file locks.

Legacy 1.0-archive markdown lives under `/data/legacy/` and is never touched
by the runtime; it's kept for grep/audit and matches the memory-note user
philosophy: "state 全在檔案系統，刪檔即 reset".
"""
import json
import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

DATA = Path("/data")
ERRORS = DATA / "errors"
ROLEPLAYS = DATA / "roleplays"
SESSIONS = DATA / "sessions"
DRILLS = DATA / "drills"

TZ = ZoneInfo("Asia/Taipei")


# ── init ───────────────────────────────────────────────────────────────────

def ensure_dirs() -> None:
    for p in (
        ERRORS / "active", ERRORS / "graduated",
        ROLEPLAYS / "active", ROLEPLAYS / "done",
        SESSIONS, DRILLS,
    ):
        p.mkdir(parents=True, exist_ok=True)


# ── front-matter helpers ──────────────────────────────────────────────────

def _read_frontmatter(text: str) -> tuple[dict, str]:
    """Split `---\\nyaml\\n---\\nbody` into (meta_dict, body_str)."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta = yaml.safe_load(text[4:end]) or {}
    return meta, text[end + 5:]


def _write_frontmatter(meta: dict, body: str) -> str:
    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{fm}\n---\n{body}"


# ── slug + id helpers ─────────────────────────────────────────────────────

_UNSAFE = re.compile(r"[^\w\-]+", re.UNICODE)


def _slugify(text: str, maxlen: int = 60) -> str:
    s = _UNSAFE.sub("-", text.strip().lower()).strip("-")
    return (s or "untitled")[:maxlen]


def _next_error_id() -> int:
    """Max existing ID + 1, scanning both active and graduated."""
    max_id = 0
    for sub in ("active", "graduated"):
        for f in (ERRORS / sub).glob("[0-9]*-*.md"):
            try:
                n = int(f.name.split("-", 1)[0])
                if n > max_id:
                    max_id = n
            except (ValueError, IndexError):
                continue
    return max_id + 1


def _error_path(err_id: int) -> Path | None:
    """Find current file for error id in active/ or graduated/."""
    for sub in ("active", "graduated"):
        matches = list((ERRORS / sub).glob(f"{err_id:04d}-*.md"))
        if matches:
            return matches[0]
    return None


# ── errors ────────────────────────────────────────────────────────────────

def list_active_errors(limit: int | None = None) -> list[dict]:
    """Ordered by last_seen_date DESC (most recent first)."""
    rows = []
    for f in (ERRORS / "active").glob("[0-9]*-*.md"):
        meta, body = _read_frontmatter(f.read_text(encoding="utf-8"))
        rows.append({
            "id": meta.get("id"),
            "title": meta.get("title", ""),
            "body_md": body.strip(),
            "last_seen_date": meta.get("last_seen_date") or "",
        })
    rows.sort(key=lambda r: r["last_seen_date"], reverse=True)
    return rows[:limit] if limit else rows


def count_active_errors() -> int:
    return sum(1 for _ in (ERRORS / "active").glob("[0-9]*-*.md"))


def add_error(*, title: str, body_md: str, source_session_id: str | None,
              today_iso: str) -> int:
    err_id = _next_error_id()
    meta = {
        "id": err_id,
        "title": title,
        "status": "active",
        "first_seen_date": today_iso,
        "last_seen_date": today_iso,
        "source_session_id": source_session_id,
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
    }
    path = ERRORS / "active" / f"{err_id:04d}-{_slugify(title)}.md"
    path.write_text(_write_frontmatter(meta, body_md.strip() + "\n"), encoding="utf-8")
    return err_id


def graduate_error(err_id: int) -> bool:
    """Move active/{id}-*.md → graduated/, update meta. Idempotent."""
    src = _error_path(err_id)
    if src is None or src.parent.name != "active":
        return False
    text = src.read_text(encoding="utf-8")
    meta, body = _read_frontmatter(text)
    meta["status"] = "graduated"
    meta["graduated_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    dst = ERRORS / "graduated" / src.name
    dst.write_text(_write_frontmatter(meta, body), encoding="utf-8")
    src.unlink()
    return True


# ── roleplays ─────────────────────────────────────────────────────────────

def _roleplay_files_by_dir(sub: str) -> list[Path]:
    return sorted((ROLEPLAYS / sub).glob("*.md"))


def get_active_roleplay() -> dict | None:
    files = _roleplay_files_by_dir("active")
    if not files:
        return None
    # If somehow > 1 (shouldn't happen), take the newest by mtime; user can
    # `rm` the strays manually.
    f = max(files, key=lambda p: p.stat().st_mtime)
    meta, body = _read_frontmatter(f.read_text(encoding="utf-8"))
    return {
        "id": meta.get("id", f.stem),
        "date": meta.get("date", ""),
        "topic": meta.get("topic", ""),
        "rationale": meta.get("rationale", ""),
        "body_md": body.strip(),
        "path": str(f),
    }


def add_roleplay(*, date_iso: str, topic: str, rationale: str, body_md: str) -> str:
    """Insert as new active roleplay. Filename: YYYY-MM-DD-<topic-slug>.md.
    Returns rp_id (= filename stem)."""
    slug = _slugify(topic)
    base = f"{date_iso}-{slug}"
    # Disambiguate if same-day same-topic collision — append short hash.
    candidate = ROLEPLAYS / "active" / f"{base}.md"
    if candidate.exists() or any((ROLEPLAYS / "done").glob(f"{base}*.md")):
        candidate = ROLEPLAYS / "active" / f"{base}-{uuid.uuid4().hex[:6]}.md"
    rp_id = candidate.stem
    meta = {
        "id": rp_id,
        "date": date_iso,
        "topic": topic,
        "rationale": rationale,
        "status": "active",
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
    }
    candidate.write_text(_write_frontmatter(meta, body_md.strip() + "\n"), encoding="utf-8")
    return rp_id


def finish_roleplay(rp_id: str) -> bool:
    """Move active/{rp_id}.md → done/. Idempotent."""
    src = ROLEPLAYS / "active" / f"{rp_id}.md"
    if not src.exists():
        return False
    text = src.read_text(encoding="utf-8")
    meta, body = _read_frontmatter(text)
    meta["status"] = "done"
    dst = ROLEPLAYS / "done" / src.name
    dst.write_text(_write_frontmatter(meta, body), encoding="utf-8")
    src.unlink()
    return True


def find_roleplay(rp_id: str) -> dict | None:
    """Look up in active/ then done/."""
    for sub in ("active", "done"):
        f = ROLEPLAYS / sub / f"{rp_id}.md"
        if f.exists():
            meta, body = _read_frontmatter(f.read_text(encoding="utf-8"))
            return {
                "id": rp_id,
                "date": meta.get("date", ""),
                "topic": meta.get("topic", ""),
                "rationale": meta.get("rationale", ""),
                "body_md": body.strip(),
                "status": meta.get("status", sub),
            }
    return None


def recent_roleplay_topics(n: int = 10) -> list[str]:
    """Topics from newest N roleplays across active + done (for avoid-repeat)."""
    all_files = _roleplay_files_by_dir("active") + _roleplay_files_by_dir("done")
    all_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    topics = []
    for f in all_files[:n]:
        meta, _ = _read_frontmatter(f.read_text(encoding="utf-8"))
        t = meta.get("topic")
        if t:
            topics.append(t)
    return topics


# ── sessions ──────────────────────────────────────────────────────────────

def new_session_id() -> str:
    """Session IDs follow `YYYY-MM-DDTHH-MM-SS-<8hex>` so `ls data/sessions/`
    sorts chronologically and each id encodes its own timestamp. Colons
    replaced with `-` so the id is URL-safe without percent-encoding when
    it flows through `/sessions/{id}/decide`. 8 hex chars of entropy is
    plenty for per-user uniqueness."""
    stamp = datetime.now(TZ).strftime("%Y-%m-%dT%H-%M-%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _session_meta_path(sid: str) -> Path:
    return SESSIONS / f"{sid}.json"


def _session_decisions_path(sid: str) -> Path:
    return SESSIONS / f"{sid}.decisions.jsonl"


def add_session(*, sid: str, roleplay_id: str | None, mode: str,
                audio_ext: str, transcript: str, summary: str,
                fluency_notes: str, raw_response: dict) -> None:
    meta = {
        "id": sid,
        "roleplay_id": roleplay_id,
        "mode": mode,
        "audio_ext": audio_ext,
        "uploaded_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "transcript": transcript,
        "summary": summary,
        "fluency_notes": fluency_notes,
        "raw_response": raw_response,
    }
    _session_meta_path(sid).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    # Touch decisions file so latest-pending scan sees it explicitly even
    # before the first swipe.
    _session_decisions_path(sid).touch(exist_ok=False)


def get_session(sid: str) -> dict | None:
    p = _session_meta_path(sid)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def read_decisions(sid: str) -> dict:
    """Reduce the jsonl into {candidate_id: action}. Last-write-wins per cid
    (which shouldn't happen — the `decide` endpoint 409s on already-recorded)."""
    p = _session_decisions_path(sid)
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            out[row["candidate_id"]] = row["action"]
        except (json.JSONDecodeError, KeyError):
            continue
    return out


def append_decision(sid: str, candidate_id: str, action: str) -> None:
    entry = {
        "candidate_id": candidate_id,
        "action": action,
        "at": datetime.now(TZ).isoformat(timespec="seconds"),
    }
    with open(_session_decisions_path(sid), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def is_review_done(sid: str) -> bool:
    """True iff every candidate id in raw_response.additions + graduations has
    been decided. Derived from files, not stored explicitly."""
    sess = get_session(sid)
    if sess is None:
        return False
    raw = sess.get("raw_response") or {}
    all_ids = [a.get("id") for a in raw.get("additions", []) if a.get("id")]
    all_ids += [g.get("id") for g in raw.get("graduations", []) if g.get("id")]
    if not all_ids:
        return True  # nothing to review
    decisions = read_decisions(sid)
    return all(cid in decisions for cid in all_ids)


def latest_pending_session() -> dict | None:
    """Newest session whose review is not yet done."""
    metas = sorted(SESSIONS.glob("*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for p in metas:
        sid = p.stem
        if not is_review_done(sid):
            return get_session(sid)
    return None


def recent_sessions(n: int = 5) -> list[dict]:
    metas = sorted(SESSIONS.glob("*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return [get_session(p.stem) for p in metas[:n]]


def session_dates_iso() -> set[str]:
    """Set of YYYY-MM-DD strings from every session's uploaded_at."""
    out = set()
    for p in SESSIONS.glob("*.json"):
        try:
            sess = json.loads(p.read_text(encoding="utf-8"))
            ts = sess.get("uploaded_at", "")
            if ts:
                out.add(ts[:10])
        except (json.JSONDecodeError, OSError):
            continue
    return out


def has_session_today(today_iso: str) -> bool:
    return today_iso in session_dates_iso()


# ── drills ────────────────────────────────────────────────────────────────

def _drill_path(date_iso: str) -> Path:
    return DRILLS / f"{date_iso}.json"


def get_drill(date_iso: str) -> dict | None:
    p = _drill_path(date_iso)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def add_drill(*, date_iso: str, rationale: str, cards: list[dict]) -> None:
    """Cards get 1-based order_index injected."""
    for i, c in enumerate(cards):
        c["order_index"] = i
    payload = {
        "date": date_iso,
        "rationale": rationale,
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "cards": cards,
    }
    _drill_path(date_iso).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def drill_dates() -> set[str]:
    return {p.stem for p in DRILLS.glob("*.json")}


# ── stats ─────────────────────────────────────────────────────────────────

def compute_streak(today: date) -> int:
    """Consecutive days ending today (or yesterday if today's empty) with at
    least one session OR a drill generated. Matches old DB logic — drill
    existence for a date = that date counts (drill 'completed_at' is unused
    in current code)."""
    active_days = {date.fromisoformat(d) for d in session_dates_iso() | drill_dates()}
    if not active_days:
        return 0
    cursor = today if today in active_days else today - timedelta(days=1)
    if cursor not in active_days:
        return 0
    streak = 0
    while cursor in active_days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak
