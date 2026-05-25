import base64
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware

from db import connect, init_schema
from models import (
    Roleplay,
    ErrorCandidate,
    GraduateCandidate,
    DrillCard,
    AdditionsApply,
    GraduationsApply,
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


ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "transcript": {"type": "string"},
        "summary": {"type": "string"},
        "fluency_notes": {"type": "string"},
        "additions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "you_said": {"type": "string"},
                    "native": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["id", "title", "you_said", "native"],
            },
        },
        "graduations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "error_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["id", "error_id", "title", "evidence"],
            },
        },
    },
    "required": ["transcript", "additions", "graduations"],
}


def _build_analysis_prompt(active_errors: list) -> str:
    """Render the audio-analysis prompt with active errors injected.

    Adapted from `1.0-archive/prompts/gemini-analysis.md` and extended to also
    detect graduations (active errors the user used correctly this session).
    """
    parts = [
        "You'll receive a recording of me practicing English with an AI partner.",
        "Analyze MY performance only — don't critique the AI's lines.",
        "If you can't tell us apart, assume I'm the less-confident, less-fluent voice.",
        "",
        "Background: my L1 is Mandarin Chinese (Taiwan). The most common L1-transfer "
        "patterns to expect are: missing articles, dropped 3rd-person -s, present-tense "
        "narration of past events, treating uncountable nouns as countable, "
        "modal/conditional misuse (e.g. 'must' where 'would' is right), and word-by-word "
        "compound nouns ('flash card and app' instead of 'flashcard app'). These are "
        "high-signal targets — flag them aggressively even if the meaning is clear.",
        "",
        "Output JSON in this exact shape:",
        "{",
        '  "transcript": "full dialogue with [Me] and [AI] tags",',
        '  "summary": "1-2 Chinese sentences summarizing the session",',
        '  "fluency_notes": "observations about my fluency, confidence, tone",',
        '  "additions": [',
        '    {"id": "add-1", "title": "short English label naming the error",',
        '     "you_said": "the full sentence I actually said, with the error in place",',
        '     "native": "the same sentence rewritten naturally with the fix applied",',
        '     "note": "Chinese explanation, 1-2 sentences"}',
        "  ],",
        '  "graduations": [',
        '    {"id": "grad-1", "error_id": <integer from the active errors list below>,',
        '     "title": "title copied from the matched error",',
        '     "evidence": "the full sentence where I used the error\'s correct form"}',
        "  ]",
        "}",
        "",
        "Rules:",
        "- you_said and native must be COMPLETE SENTENCES from the transcript, not bare phrases. This applies even when the error is a single noun, article, or modifier — include the whole sentence around it so I remember the context.",
        "- Wrap the specific error portion in double quotes \" \" within you_said, and the corresponding correction in \" \" within native, at the same position. Keeps the diff visually obvious.",
        "  BAD:",
        '    "you_said": "all the infrastructures"',
        '    "native":   "all the infrastructure"',
        "  GOOD:",
        '    "you_said": "I have prepared all the \"infrastructures\" for the app he wanted."',
        '    "native":   "I had prepared all the \"infrastructure\" for the app he wanted."',
        "- If the same grammatical pattern appears in MULTIPLE sentences this session, return ONE addition for the whole pattern, not one per instance. Group them:",
        "  - title: name the pattern, not a single instance",
        "  - you_said: full sentences for each instance, joined by ' / '",
        "  - native: corrected versions in the same order, joined by ' / '",
        "  - note: explain the pattern and mention it recurred",
        "  GOOD grouping example (3 instances of the same past-tense issue collapsed, each with quote-highlighting at the error):",
        '    {"title": "Past tense for past-event narration",',
        '     "you_said": "It was smooth because I have prepared the app he \"want\". / Before I \"leave\" my uncle\'s house, he said... / In my original design, the app only \"provide\" caption.",',
        '     "native":   "It was smooth because I had prepared the app he \"wanted\". / Before I \"left\" my uncle\'s house, he said... / In my original design, the app only \"provided\" caption.",',
        '     "note": "敘述過去事件時動詞要用過去式。本次出現多處。"}',
        "  Different patterns stay separate (past-tense and subject-verb agreement are different patterns even if they involve the same word).",
        "- additions: NEW errors I made this session, not already in active errors",
        "- graduations: STRICT criteria — only include if I demonstrably used the active error's CORRECTED form:",
        "    * the evidence sentence must contain ZERO instance of the error pattern",
        "    * if the evidence sentence still contains the same mistake the error tracks, do NOT graduate",
        "    * a sentence flagged in `additions` CANNOT also appear as graduation evidence — pick one role per sentence",
        "    * when in doubt, omit. A missed graduation is fine. A false graduation undoes real learning.",
        "- note, summary, and fluency_notes must all be in Traditional Chinese (Taiwan)",
        "- Skip these — they are NOT errors worth flagging:",
        "    * accent or pronunciation glitches",
        "    * minor pauses, hesitations, filler words (uh, um)",
        "    * mid-utterance self-corrections: if I started saying something wrong and "
        "fixed it within the same sentence or in the next utterance, the corrected form "
        "stands. Don't flag the slip. Example to skip: 'Anthropic, Anthropic's IPO' — "
        "that's a stutter, not a repetition error.",
        "- IDs are simple sequences (add-1, add-2, grad-1, ...) — don't reuse across categories",
        "",
    ]
    if active_errors:
        parts.append("Active errors (use these IDs in graduations):")
        for e in active_errors:
            body = (e["body_md"] or "")[:200].replace("\n", " ")
            parts.append(f"- id={e['id']} · {e['title']} · {body}")
    else:
        parts.append("Active errors: (none — graduations will be empty)")
    return "\n".join(parts)


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


@app.get("/today/roleplay", response_model=Roleplay)
def get_today_roleplay():
    return Roleplay(
        id="stub-1",
        date="2026-05-23",
        topic="apartment",
        rationale="練 utilities / deposit / lease / negotiate",
        script=(
            "AI:  Hi, are you here for the 2pm showing?\n"
            "你:  對，是看兩房那間嗎？\n\n"
            "AI:  Yeah, follow me. What would you like to know first?\n"
            "你:  想了解租金、押金、什麼時候可以入住\n"
        ),
    )


@app.post("/upload")
async def upload_audio(file: UploadFile = File(...)):
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
        today_iso = _today_local().isoformat()
        rp = conn.execute(
            "SELECT id, topic FROM roleplays WHERE date = ? LIMIT 1",
            (today_iso,),
        ).fetchone()
    finally:
        conn.close()

    prompt = _build_analysis_prompt(active_errors)
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
                (id, roleplay_id, transcript, summary, fluency_notes, raw_response)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                rp["id"] if rp else None,
                analysis.get("transcript", ""),
                analysis.get("summary", ""),
                analysis.get("fluency_notes", ""),
                json.dumps(analysis, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "session_id": session_id,
        "date": today_iso,
        "topic": rp["topic"] if rp else "(no roleplay)",
    }


@app.get("/today/review", response_model=ReviewBundle)
def get_today_review():
    conn = connect()
    try:
        row = conn.execute(
            "SELECT raw_response FROM sessions ORDER BY uploaded_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    if not row or not row["raw_response"]:
        return ReviewBundle(additions=[], graduations=[])

    try:
        analysis = json.loads(row["raw_response"])
    except json.JSONDecodeError:
        return ReviewBundle(additions=[], graduations=[])

    additions = [
        ErrorCandidate(
            id=a["id"],
            title=a["title"],
            you_said=a["you_said"],
            native=a["native"],
            note=a.get("note", ""),
        )
        for a in analysis.get("additions", [])
        if all(k in a for k in ("id", "title", "you_said", "native"))
    ]
    graduations = [
        GraduateCandidate(id=g["id"], title=g["title"], evidence=g["evidence"])
        for g in analysis.get("graduations", [])
        if all(k in g for k in ("id", "title", "evidence"))
    ]
    return ReviewBundle(additions=additions, graduations=graduations)


@app.post("/errors/additions")
def apply_additions(body: AdditionsApply):
    if not body.candidate_ids:
        return {"added": 0}

    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, raw_response FROM sessions ORDER BY uploaded_at DESC LIMIT 1"
        ).fetchone()
        if not row or not row["raw_response"]:
            raise HTTPException(status_code=404, detail="No session to source candidates from")

        candidates_by_id = {
            a["id"]: a for a in json.loads(row["raw_response"]).get("additions", [])
        }
        today_iso = _today_local().isoformat()
        added = 0
        for cid in body.candidate_ids:
            c = candidates_by_id.get(cid)
            if not c:
                continue
            body_md = (
                f"**you_said**: {c.get('you_said', '')}\n\n"
                f"**native**: {c.get('native', '')}\n\n"
                f"**note**: {c.get('note', '')}"
            )
            conn.execute(
                """
                INSERT INTO errors
                    (title, body_md, first_seen_date, last_seen_date, source_session_id, status)
                VALUES (?, ?, ?, ?, ?, 'active')
                """,
                (c.get("title", "(untitled)"), body_md, today_iso, today_iso, row["id"]),
            )
            added += 1
        conn.commit()
        return {"added": added}
    finally:
        conn.close()


@app.post("/errors/graduations")
def apply_graduations(body: GraduationsApply):
    if not body.error_ids:
        return {"graduated": 0}

    conn = connect()
    try:
        row = conn.execute(
            "SELECT raw_response FROM sessions ORDER BY uploaded_at DESC LIMIT 1"
        ).fetchone()
        if not row or not row["raw_response"]:
            raise HTTPException(status_code=404, detail="No session to resolve graduations against")

        grad_to_error = {
            g["id"]: g.get("error_id")
            for g in json.loads(row["raw_response"]).get("graduations", [])
            if "id" in g and "error_id" in g
        }
        db_ids = [
            grad_to_error[gid] for gid in body.error_ids
            if grad_to_error.get(gid) is not None
        ]
        if not db_ids:
            return {"graduated": 0}

        placeholders = ",".join("?" * len(db_ids))
        cur = conn.execute(
            f"UPDATE errors SET status='graduated', graduated_at=datetime('now') "
            f"WHERE id IN ({placeholders}) AND status='active'",
            db_ids,
        )
        conn.commit()
        return {"graduated": cur.rowcount}
    finally:
        conn.close()


@app.get("/today/drill", response_model=list[DrillCard])
def get_today_drill():
    return [
        DrillCard(
            id="drill-1",
            prompt="把這句翻成英文：「這個租約有議價空間嗎？」",
            answer="Is there any room for negotiation on this lease?",
            source_error_id="err-room-for-negotiation",
        ),
        DrillCard(
            id="drill-2",
            prompt="把這句翻成英文：「他們會再回覆我關於押金的事。」",
            answer="They'll get back to me about the deposit.",
            source_error_id="err-get-back-to",
        ),
        DrillCard(
            id="drill-3",
            prompt="填空：I used ___ live in Taipei.",
            answer="I used to live in Taipei.",
            source_error_id="err-used-to",
        ),
    ]
