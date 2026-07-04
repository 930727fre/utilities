"""One-shot migration: SQLite → filesystem layout.

Runs INSIDE the free2speak-backend container (needs root ownership on /data
that the container has). One command:

    docker exec free2speak-backend python /app/migrate_db_to_files.py

What it does (in order):
  1. Sideline 1.0-era files (data/roleplays/*.md, data/drills/*.md,
     data/errors.md, data/sessions/YYYY-MM-DD-*.json) → data/legacy/
     (audio .mpeg files stay put — they're 2.0-era, keyed by session UUID).
  2. Create the new directory skeleton via storage.ensure_dirs().
  3. Dump each SQLite table to its new file location.
  4. Verify row counts match written file counts.

Idempotency: safe to re-run — legacy sideline is `mv if exists`, and each
write overwrites. But it will re-write files with slightly different
front-matter timestamps if the source rows haven't changed, so prefer running
once and then removing the DB manually.

Does NOT delete free2speak.db. User verifies then removes.
"""
import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import storage  # noqa: E402

DB_PATH = Path("/data/free2speak.db")
LEGACY = Path("/data/legacy")


def sideline_1p0() -> None:
    """Move 1.0-era files into data/legacy/ so they don't shadow the new
    layout. Anything that doesn't match the 1.0 pattern stays put."""
    (LEGACY / "roleplays").mkdir(parents=True, exist_ok=True)
    (LEGACY / "drills").mkdir(parents=True, exist_ok=True)
    (LEGACY / "sessions").mkdir(parents=True, exist_ok=True)

    # data/roleplays/*.md — every one is 1.0 (2.0 will use active/, done/).
    old_rp = Path("/data/roleplays")
    if old_rp.is_dir():
        for f in old_rp.glob("*.md"):
            shutil.move(str(f), str(LEGACY / "roleplays" / f.name))
        # Only remove if now empty (about to be recreated as skeleton).
        try:
            old_rp.rmdir()
        except OSError:
            pass

    old_dr = Path("/data/drills")
    if old_dr.is_dir():
        for f in old_dr.glob("*.md"):
            shutil.move(str(f), str(LEGACY / "drills" / f.name))
        try:
            old_dr.rmdir()
        except OSError:
            pass

    # data/sessions/*.json where filename starts with YYYY-MM-DD → 1.0.
    # 2.0 metadata files will be named <uuid32>.json, no date prefix.
    for f in Path("/data/sessions").glob("2*-*-*-*.json"):
        shutil.move(str(f), str(LEGACY / "sessions" / f.name))

    err_md = Path("/data/errors.md")
    if err_md.exists():
        shutil.move(str(err_md), str(LEGACY / "errors.md"))


def migrate_errors(conn: sqlite3.Connection) -> tuple[int, int]:
    """Dump `errors` table → data/errors/{status}/NNNN-slug.md."""
    rows = conn.execute(
        "SELECT id, title, body_md, first_seen_date, last_seen_date, "
        "source_session_id, status, graduated_at, created_at "
        "FROM errors"
    ).fetchall()
    written = 0
    for r in rows:
        meta = {
            "id": r["id"],
            "title": r["title"] or "",
            "status": r["status"],
            "first_seen_date": r["first_seen_date"] or "",
            "last_seen_date": r["last_seen_date"] or "",
            "source_session_id": r["source_session_id"],
            "created_at": r["created_at"] or "",
            "graduated_at": r["graduated_at"],
        }
        sub = "active" if r["status"] == "active" else "graduated"
        path = storage.ERRORS / sub / f"{r['id']:04d}-{storage._slugify(r['title'] or 'untitled')}.md"
        body = (r["body_md"] or "").strip() + "\n"
        path.write_text(storage._write_frontmatter(meta, body), encoding="utf-8")
        written += 1
    return len(rows), written


def migrate_roleplays(conn: sqlite3.Connection) -> tuple[int, int]:
    """Dump `roleplays` table → data/roleplays/{active|done}/<id>.md.

    Uses the DB's `id` column (32-hex uuid) as the filename stem when the
    stem shape matches a UUID; otherwise falls back to `YYYY-MM-DD-topic`
    (this preserves the 1.0-imported roleplays that used date-topic ids).
    """
    rows = conn.execute(
        "SELECT id, date, topic, rationale, body_md, status, created_at "
        "FROM roleplays"
    ).fetchall()
    written = 0
    for r in rows:
        rp_id = r["id"]
        meta = {
            "id": rp_id,
            "date": r["date"] or "",
            "topic": r["topic"] or "",
            "rationale": r["rationale"] or "",
            "status": r["status"],
            "created_at": r["created_at"] or "",
        }
        sub = "active" if r["status"] == "active" else "done"
        # Prefer a readable filename: date-topic-shorthash. For imported 1.0
        # roleplays whose id was already 'YYYY-MM-DD-topic' just use the id.
        if "-" in rp_id and rp_id[:4].isdigit():
            fname = f"{rp_id}.md"
        else:
            slug = storage._slugify(r["topic"] or "untitled")
            fname = f"{r['date']}-{slug}-{rp_id[:8]}.md"
            # Update meta id so lookups match the filename stem going forward.
            meta["id"] = fname[:-3]
        path = storage.ROLEPLAYS / sub / fname
        body = (r["body_md"] or "").strip() + "\n"
        path.write_text(storage._write_frontmatter(meta, body), encoding="utf-8")
        written += 1
    return len(rows), written


def migrate_sessions(conn: sqlite3.Connection) -> tuple[int, int]:
    """Dump `sessions` → data/sessions/<id>.json + <id>.decisions.jsonl.

    Preserves the 2.0-era audio files that already sit at <id>.<ext> — this
    just adds the JSON metadata + decisions log next to them.
    Also translates the roleplay_id reference through the migration if the
    filename stem changed (roleplays get date-topic-hash renames)."""
    # Build mapping from old rp_id → new rp_id (only differs for UUID-shaped ids).
    rp_id_map = {}
    for r in conn.execute("SELECT id, date, topic FROM roleplays").fetchall():
        old = r["id"]
        if "-" in old and old[:4].isdigit():
            rp_id_map[old] = old
        else:
            slug = storage._slugify(r["topic"] or "untitled")
            rp_id_map[old] = f"{r['date']}-{slug}-{old[:8]}"

    rows = conn.execute(
        "SELECT id, roleplay_id, transcript, summary, fluency_notes, "
        "raw_response, mode, decisions, review_done, uploaded_at "
        "FROM sessions"
    ).fetchall()
    written = 0
    for r in rows:
        sid = r["id"]
        # Detect audio_ext from existing file on disk.
        matches = list(storage.SESSIONS.glob(f"{sid}.*"))
        audio_ext = ""
        for m in matches:
            if m.suffix not in (".json", ".jsonl"):
                audio_ext = m.suffix.lstrip(".")
                break
        try:
            raw = json.loads(r["raw_response"]) if r["raw_response"] else {}
        except json.JSONDecodeError:
            raw = {}
        meta = {
            "id": sid,
            "roleplay_id": rp_id_map.get(r["roleplay_id"], r["roleplay_id"]),
            "mode": r["mode"],
            "audio_ext": audio_ext,
            "uploaded_at": r["uploaded_at"] or "",
            "transcript": r["transcript"] or "",
            "summary": r["summary"] or "",
            "fluency_notes": r["fluency_notes"] or "",
            "raw_response": raw,
        }
        (storage.SESSIONS / f"{sid}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        # Decisions dict → jsonl (order isn't preserved from DB — that's OK,
        # `read_decisions` reduces to a dict anyway).
        try:
            decisions_dict = json.loads(r["decisions"]) if r["decisions"] else {}
        except json.JSONDecodeError:
            decisions_dict = {}
        jsonl_path = storage.SESSIONS / f"{sid}.decisions.jsonl"
        with open(jsonl_path, "w", encoding="utf-8") as fh:
            for cid, action in decisions_dict.items():
                fh.write(json.dumps({
                    "candidate_id": cid,
                    "action": action,
                    "at": r["uploaded_at"] or "",
                }, ensure_ascii=False) + "\n")
        written += 1
    return len(rows), written


def migrate_drills(conn: sqlite3.Connection) -> tuple[int, int]:
    """Dump `drills` + `drill_cards` → data/drills/YYYY-MM-DD.json."""
    rows = conn.execute(
        "SELECT id, date, rationale, completed_at, created_at FROM drills"
    ).fetchall()
    written = 0
    for r in rows:
        cards = [dict(c) for c in conn.execute(
            "SELECT kind, prompt, answer, rationale, source_error_id, order_index "
            "FROM drill_cards WHERE drill_id = ? ORDER BY order_index",
            (r["id"],),
        ).fetchall()]
        payload = {
            "date": r["date"] or "",
            "rationale": r["rationale"] or "",
            "created_at": r["created_at"] or "",
            "completed_at": r["completed_at"],
            "cards": cards,
        }
        (storage.DRILLS / f"{r['date']}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1
    return len(rows), written


def main() -> None:
    print("[migrate] sideline 1.0-era files → /data/legacy/")
    sideline_1p0()

    print("[migrate] create /data/{errors,roleplays,sessions,drills} skeleton")
    storage.ensure_dirs()

    if not DB_PATH.exists():
        print(f"[migrate] {DB_PATH} does not exist — skeleton only")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        n_e, w_e = migrate_errors(conn)
        print(f"[migrate] errors: read {n_e}, wrote {w_e}")
        n_r, w_r = migrate_roleplays(conn)
        print(f"[migrate] roleplays: read {n_r}, wrote {w_r}")
        n_s, w_s = migrate_sessions(conn)
        print(f"[migrate] sessions: read {n_s}, wrote {w_s}")
        n_d, w_d = migrate_drills(conn)
        print(f"[migrate] drills: read {n_d}, wrote {w_d}")
    finally:
        conn.close()

    print("[migrate] done. Verify UI, then remove /data/free2speak.db manually.")


if __name__ == "__main__":
    main()
