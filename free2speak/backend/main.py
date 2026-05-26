import base64
import json
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware

from db import connect, init_schema
from opus_client import emit_tool as opus_emit_tool
from prompts.gemini_analysis import SCHEMA as ANALYSIS_SCHEMA, build as build_analysis_prompt
from prompts.opus_drill import TOOL as DRILL_TOOL, build as build_drill_prompt
from prompts.opus_roleplay import TOOL as ROLEPLAY_TOOL, build as build_roleplay_prompt
from models import (
    Roleplay,
    ErrorCandidate,
    GraduateCandidate,
    DrillCard,
    Decision,
    PracticeState,
    ReviewBundle,
    TodayStats,
)

TZ = ZoneInfo("Asia/Taipei")

GEMINI_AUDIO_MODEL = os.environ.get("GEMINI_AUDIO_MODEL", "gemini-2.5-flash")
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_AUDIO_MODEL}:generateContent"
)
# 20 MB ceiling for inline_data uploads. Above this we'd need Gemini's Files API
# (separate upload + URI ref). A 10-min m4a/mp3 fits well under this.
MAX_INLINE_AUDIO_BYTES = 20 * 1024 * 1024
# Cap on active errors sent to Gemini per analysis — keeps prompt bounded.
ACTIVE_ERROR_LIMIT = 100
SESSIONS_DIR = Path("/data/sessions")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    conn = connect()
    init_schema(conn)
    conn.close()
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _today_local() -> date:
    return datetime.now(TZ).date()


def _compute_streak(conn) -> int:
    """Count consecutive days ending today (or yesterday if today is empty) with at
    least one practice or drill activity. Returns 0 if no recent activity."""
    rows = conn.execute("""
        SELECT day FROM (
            SELECT date(uploaded_at) AS day FROM sessions WHERE uploaded_at IS NOT NULL
            UNION
            SELECT date(completed_at) AS day FROM drills WHERE completed_at IS NOT NULL
        )
        WHERE day IS NOT NULL
        ORDER BY day DESC
    """).fetchall()
    if not rows:
        return 0

    active_days = {date.fromisoformat(r["day"]) for r in rows}
    today = _today_local()

    # Start from today if active, else yesterday (give the user grace before the day ends).
    cursor = today if today in active_days else today - timedelta(days=1)
    if cursor not in active_days:
        return 0

    streak = 0
    while cursor in active_days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _call_gemini_with_audio(audio_bytes: bytes, mime: str, prompt: str) -> dict:
    """Send audio + text prompt to Gemini, return parsed structured-output JSON."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not set")

    body = {
        "contents": [{
            "parts": [
                {"inline_data": {
                    "mime_type": mime,
                    "data": base64.b64encode(audio_bytes).decode("ascii"),
                }},
                {"text": prompt},
            ],
        }],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseSchema": ANALYSIS_SCHEMA,
        },
    }

    r = requests.post(f"{GEMINI_API_URL}?key={api_key}", json=body, timeout=300)
    if r.status_code == 429:
        raise HTTPException(status_code=429, detail="Gemini rate limit exceeded")
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"Gemini {r.status_code}: {r.text[:300]}")

    data = r.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise HTTPException(status_code=502, detail=f"Unexpected Gemini shape: {e}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"Gemini returned non-JSON: {e}")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/today/stats", response_model=TodayStats)
def get_today_stats():
    conn = connect()
    try:
        today_iso = _today_local().isoformat()
        practice_done = conn.execute(
            "SELECT 1 FROM sessions WHERE date(uploaded_at) = ? LIMIT 1",
            (today_iso,),
        ).fetchone() is not None
        drill_done = conn.execute(
            "SELECT 1 FROM drills WHERE date(completed_at) = ? LIMIT 1",
            (today_iso,),
        ).fetchone() is not None
        active_errors = conn.execute(
            "SELECT COUNT(*) FROM errors WHERE status = 'active'"
        ).fetchone()[0]
        streak = _compute_streak(conn)
        return TodayStats(
            streak_count=streak,
            practice_done_today=practice_done,
            drill_done_today=drill_done,
            active_errors_count=active_errors,
        )
    finally:
        conn.close()


def _active_roleplay_row(conn):
    return conn.execute(
        "SELECT id, date, topic, rationale, body_md FROM roleplays "
        "WHERE status='active' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()


@app.get("/today/roleplay", response_model=Roleplay)
def get_today_roleplay():
    today_iso = _today_local().isoformat()
    conn = connect()
    try:
        row = _active_roleplay_row(conn)
        if row:
            return Roleplay(
                id=row["id"], date=row["date"], topic=row["topic"],
                rationale=row["rationale"] or "", script=row["body_md"] or "",
            )

        # No active roleplay — generate via Opus.
        active_errors = conn.execute(
            "SELECT id, title, body_md FROM errors WHERE status='active' "
            "ORDER BY last_seen_date DESC LIMIT ?",
            (ACTIVE_ERROR_LIMIT,),
        ).fetchall()
        recent_sessions = conn.execute(
            "SELECT id, summary, uploaded_at FROM sessions "
            "ORDER BY uploaded_at DESC LIMIT 5"
        ).fetchall()
        recent_topics = [r["topic"] for r in conn.execute(
            "SELECT topic FROM roleplays ORDER BY created_at DESC LIMIT 10"
        ).fetchall()]
    finally:
        conn.close()

    prompt = build_roleplay_prompt(
        active_errors=active_errors,
        recent_sessions=[dict(s) for s in recent_sessions],
        recent_topics=recent_topics,
    )
    print(f"[roleplay] generating (no active row)...", flush=True)
    t0 = time.perf_counter()
    result = opus_emit_tool(prompt, ROLEPLAY_TOOL)
    print(f"[roleplay] generated: topic={result.get('topic')!r} "
          f"({time.perf_counter() - t0:.1f}s)", flush=True)

    rp_id = uuid.uuid4().hex
    conn = connect()
    try:
        try:
            conn.execute(
                "INSERT INTO roleplays (id, date, topic, rationale, body_md, status) "
                "VALUES (?, ?, ?, ?, ?, 'active')",
                (rp_id, today_iso, result.get("topic", "(untitled)"),
                 result.get("rationale", ""), result.get("body_md", "")),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Concurrent insert beat us via the partial unique index. Return the
            # other writer's active row instead of failing.
            existing = _active_roleplay_row(conn)
            if existing:
                print("[roleplay] race lost — returning concurrent insert", flush=True)
                return Roleplay(
                    id=existing["id"], date=existing["date"], topic=existing["topic"],
                    rationale=existing["rationale"] or "", script=existing["body_md"] or "",
                )
            raise
    finally:
        conn.close()

    return Roleplay(
        id=rp_id, date=today_iso, topic=result.get("topic", ""),
        rationale=result.get("rationale", ""), script=result.get("body_md", ""),
    )


@app.post("/upload")
async def upload_audio(file: UploadFile = File(...), mode: str = Form(...)):
    if mode not in ("roleplay", "freestyle"):
        raise HTTPException(status_code=400, detail=f"mode must be 'roleplay' or 'freestyle', got {mode!r}")

    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty upload")
    if len(audio_bytes) > MAX_INLINE_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Audio too large for inline upload (>{MAX_INLINE_AUDIO_BYTES // (1024*1024)} MB).",
        )
    mime = (file.content_type or "audio/webm").split(";")[0].strip()

    conn = connect()
    try:
        active_errors = conn.execute(
            "SELECT id, title, body_md FROM errors WHERE status='active' "
            "ORDER BY last_seen_date DESC LIMIT ?",
            (ACTIVE_ERROR_LIMIT,),
        ).fetchall()
        # In roleplay mode link the session to the active roleplay so completing
        # the review later can transition that roleplay to 'done'. In freestyle
        # mode leave roleplay_id NULL — review completion won't consume anything.
        rp_id = None
        if mode == "roleplay":
            rp = _active_roleplay_row(conn)
            rp_id = rp["id"] if rp else None
    finally:
        conn.close()

    prompt = build_analysis_prompt(active_errors)
    analysis = _call_gemini_with_audio(audio_bytes, mime, prompt)

    # Persist the audio file so we can re-analyze later if prompts change.
    session_id = uuid.uuid4().hex
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    ext = mime.split("/")[-1]
    (SESSIONS_DIR / f"{session_id}.{ext}").write_bytes(audio_bytes)

    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO sessions
                (id, roleplay_id, mode, transcript, summary, fluency_notes, raw_response)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                rp_id,
                mode,
                analysis.get("transcript", ""),
                analysis.get("summary", ""),
                analysis.get("fluency_notes", ""),
                json.dumps(analysis, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {"session_id": session_id, "mode": mode}


def _latest_pending_session(conn):
    """Latest session whose review isn't fully done. None if no resume target."""
    return conn.execute(
        "SELECT id, roleplay_id, mode, raw_response, decisions "
        "FROM sessions WHERE review_done = 0 "
        "ORDER BY uploaded_at DESC LIMIT 1"
    ).fetchone()


def _parse_analysis(row) -> dict:
    if not row or not row["raw_response"]:
        return {}
    try:
        return json.loads(row["raw_response"])
    except json.JSONDecodeError:
        return {}


def _parse_decisions(row) -> dict:
    if not row:
        return {}
    raw = row["decisions"] if "decisions" in row.keys() else None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


@app.get("/today/practice/state", response_model=PracticeState)
def get_practice_state():
    """Tells the frontend which step to land on. Drives resume-after-bail."""
    conn = connect()
    try:
        row = _latest_pending_session(conn)
        if not row:
            return PracticeState(step="roleplay")
        analysis = _parse_analysis(row)
        decisions = _parse_decisions(row)
        addition_ids = [a["id"] for a in analysis.get("additions", []) if "id" in a]
        grad_ids = [g["id"] for g in analysis.get("graduations", []) if "id" in g]
        if any(aid not in decisions for aid in addition_ids):
            return PracticeState(step="additions", session_id=row["id"])
        if any(gid not in decisions for gid in grad_ids):
            return PracticeState(step="graduations", session_id=row["id"])
        # All decisions made but review_done somehow still 0 — finalize and clear.
        _finalize_session(conn, row)
        return PracticeState(step="roleplay")
    finally:
        conn.close()


@app.get("/today/review", response_model=ReviewBundle)
def get_today_review():
    """Returns only undecided additions/graduations from the latest pending session."""
    conn = connect()
    try:
        row = _latest_pending_session(conn)
    finally:
        conn.close()

    analysis = _parse_analysis(row)
    decisions = _parse_decisions(row)
    additions = [
        ErrorCandidate(
            id=a["id"], title=a["title"], you_said=a["you_said"],
            native=a["native"], note=a.get("note", ""),
        )
        for a in analysis.get("additions", [])
        if all(k in a for k in ("id", "title", "you_said", "native"))
        and a["id"] not in decisions
    ]
    graduations = [
        GraduateCandidate(id=g["id"], title=g["title"], evidence=g["evidence"])
        for g in analysis.get("graduations", [])
        if all(k in g for k in ("id", "title", "evidence"))
        and g["id"] not in decisions
    ]
    return ReviewBundle(additions=additions, graduations=graduations)


def _finalize_session(conn, row) -> None:
    """Mark session as review-complete; if it was roleplay mode, retire the roleplay."""
    conn.execute("UPDATE sessions SET review_done = 1 WHERE id = ?", (row["id"],))
    if row["roleplay_id"]:
        conn.execute(
            "UPDATE roleplays SET status='done' WHERE id = ? AND status='active'",
            (row["roleplay_id"],),
        )
    conn.commit()


@app.post("/sessions/{session_id}/decide")
def decide(session_id: str, decision: Decision):
    if decision.action not in ("added", "skipped", "graduated", "kept"):
        raise HTTPException(status_code=400, detail=f"unknown action {decision.action!r}")

    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, roleplay_id, mode, raw_response, decisions, review_done "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="session not found")
        if row["review_done"]:
            raise HTTPException(status_code=409, detail="session already complete")

        analysis = _parse_analysis(row)
        decisions = _parse_decisions(row)

        is_addition = any(a.get("id") == decision.candidate_id for a in analysis.get("additions", []))
        is_graduation = any(g.get("id") == decision.candidate_id for g in analysis.get("graduations", []))
        if not is_addition and not is_graduation:
            raise HTTPException(status_code=404, detail=f"candidate {decision.candidate_id} not in analysis")

        if is_addition and decision.action not in ("added", "skipped"):
            raise HTTPException(status_code=400, detail="addition candidates require 'added' or 'skipped'")
        if is_graduation and decision.action not in ("graduated", "kept"):
            raise HTTPException(status_code=400, detail="graduation candidates require 'graduated' or 'kept'")

        # Idempotent: if already recorded, return without redoing side effects.
        if decisions.get(decision.candidate_id) == decision.action:
            return {"recorded": True, "idempotent": True}

        if decision.action == "added":
            c = next(a for a in analysis["additions"] if a["id"] == decision.candidate_id)
            body_md = (
                f"**you_said**: {c.get('you_said', '')}\n\n"
                f"**native**: {c.get('native', '')}\n\n"
                f"**note**: {c.get('note', '')}"
            )
            today_iso = _today_local().isoformat()
            conn.execute(
                """
                INSERT INTO errors
                    (title, body_md, first_seen_date, last_seen_date, source_session_id, status)
                VALUES (?, ?, ?, ?, ?, 'active')
                """,
                (c.get("title", "(untitled)"), body_md, today_iso, today_iso, row["id"]),
            )
        elif decision.action == "graduated":
            g = next(g for g in analysis["graduations"] if g["id"] == decision.candidate_id)
            err_id = g.get("error_id")
            if err_id is not None:
                conn.execute(
                    "UPDATE errors SET status='graduated', graduated_at=datetime('now') "
                    "WHERE id = ? AND status='active'",
                    (err_id,),
                )

        decisions[decision.candidate_id] = decision.action
        conn.execute(
            "UPDATE sessions SET decisions = ? WHERE id = ?",
            (json.dumps(decisions, ensure_ascii=False), row["id"]),
        )

        # If every analysis candidate is now decided, finalize.
        all_ids = (
            [a["id"] for a in analysis.get("additions", []) if "id" in a]
            + [g["id"] for g in analysis.get("graduations", []) if "id" in g]
        )
        if all(cid in decisions for cid in all_ids):
            _finalize_session(conn, row)
        else:
            conn.commit()

        return {"recorded": True}
    finally:
        conn.close()


@app.get("/today/drill", response_model=list[DrillCard])
def get_today_drill():
    today_iso = _today_local().isoformat()
    conn = connect()
    try:
        existing = conn.execute(
            "SELECT id FROM drills WHERE date = ? LIMIT 1",
            (today_iso,),
        ).fetchone()
        if existing:
            rows = conn.execute(
                "SELECT id, kind, prompt, answer, source_error_id "
                "FROM drill_cards WHERE drill_id = ? ORDER BY order_index",
                (existing["id"],),
            ).fetchall()
            return [
                DrillCard(
                    id=str(r["id"]), prompt=r["prompt"], answer=r["answer"],
                    source_error_id=str(r["source_error_id"]) if r["source_error_id"] is not None else None,
                )
                for r in rows
            ]

        # Generate via Opus.
        active_errors = conn.execute(
            "SELECT id, title, body_md FROM errors WHERE status='active' "
            "ORDER BY last_seen_date DESC LIMIT ?",
            (ACTIVE_ERROR_LIMIT,),
        ).fetchall()
        recent_sessions = conn.execute(
            "SELECT id, transcript, summary, uploaded_at FROM sessions "
            "ORDER BY uploaded_at DESC LIMIT 5"
        ).fetchall()
    finally:
        conn.close()

    prompt = build_drill_prompt(
        active_errors=active_errors,
        recent_sessions=[dict(s) for s in recent_sessions],
    )
    print(f"[drill] generating for {today_iso}...", flush=True)
    t0 = time.perf_counter()
    result = opus_emit_tool(prompt, DRILL_TOOL)
    print(f"[drill] generated for {today_iso}: {len(result.get('cards', []))} cards "
          f"({time.perf_counter() - t0:.1f}s)", flush=True)
    cards = result.get("cards", [])
    if not cards:
        raise HTTPException(status_code=503, detail="Drill cold-start: not enough material to generate")

    drill_id = uuid.uuid4().hex
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO drills (id, date, rationale) VALUES (?, ?, ?)",
            (drill_id, today_iso, result.get("rationale", "")),
        )
        for i, c in enumerate(cards):
            conn.execute(
                "INSERT INTO drill_cards "
                "(drill_id, order_index, kind, prompt, answer, rationale, source_error_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (drill_id, i, c.get("kind", "translate"), c.get("prompt", ""),
                 c.get("answer", ""), c.get("rationale", ""), c.get("source_error_id")),
            )
        conn.commit()
        rows = conn.execute(
            "SELECT id, kind, prompt, answer, source_error_id "
            "FROM drill_cards WHERE drill_id = ? ORDER BY order_index",
            (drill_id,),
        ).fetchall()
    finally:
        conn.close()

    return [
        DrillCard(
            id=str(r["id"]), prompt=r["prompt"], answer=r["answer"],
            source_error_id=str(r["source_error_id"]) if r["source_error_id"] is not None else None,
        )
        for r in rows
    ]
