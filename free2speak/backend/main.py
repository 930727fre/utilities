import base64
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware

import storage
from analyzer import analyze as claude_analyze
from opus_client import emit_tool as opus_emit_tool
from prompts.gemini_transcribe import SCHEMA as TRANSCRIBE_SCHEMA, build as build_transcribe_prompt
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    storage.ensure_dirs()
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


def _gemini_transcribe(audio_bytes: bytes, mime: str) -> str:
    """Send audio to Gemini 2.5 Flash, return verbatim transcript string.

    Path B: Gemini's only job is transcription. Analysis is downstream at
    Claude. Structured output pins it to `{"transcript": "..."}` so we don't
    have to parse prose.
    """
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
                {"text": build_transcribe_prompt()},
            ],
        }],
        "generationConfig": {
            "temperature": 0.0,
            "responseMimeType": "application/json",
            "responseSchema": TRANSCRIBE_SCHEMA,
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
        return json.loads(text)["transcript"]
    except (json.JSONDecodeError, KeyError) as e:
        raise HTTPException(status_code=502, detail=f"Gemini returned bad JSON: {e}")




@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/today/stats", response_model=TodayStats)
def get_today_stats():
    today_iso = _today_local().isoformat()
    return TodayStats(
        streak_count=storage.compute_streak(_today_local()),
        practice_done_today=storage.has_session_today(today_iso),
        drill_done_today=today_iso in storage.drill_dates(),
        active_errors_count=storage.count_active_errors(),
    )


@app.get("/today/roleplay", response_model=Roleplay)
def get_today_roleplay():
    today_iso = _today_local().isoformat()
    active = storage.get_active_roleplay()
    if active:
        return Roleplay(
            id=active["id"], date=active["date"], topic=active["topic"],
            rationale=active["rationale"], script=active["body_md"],
        )

    # No active roleplay — generate via Opus.
    active_errors = storage.list_active_errors(limit=ACTIVE_ERROR_LIMIT)
    recent_sessions = storage.recent_sessions(n=5)
    recent_topics = storage.recent_roleplay_topics(n=10)

    prompt = build_roleplay_prompt(
        active_errors=active_errors,
        recent_sessions=recent_sessions,
        recent_topics=recent_topics,
    )
    print("[roleplay] generating (no active row)...", flush=True)
    t0 = time.perf_counter()
    result = opus_emit_tool(prompt, ROLEPLAY_TOOL)
    print(f"[roleplay] generated: topic={result.get('topic')!r} "
          f"({time.perf_counter() - t0:.1f}s)", flush=True)

    # Re-check for race: another concurrent call may have won. If so, return
    # theirs. Cheap on filesystem — just re-lists active/.
    active = storage.get_active_roleplay()
    if active:
        print("[roleplay] race lost — returning concurrent insert", flush=True)
        return Roleplay(
            id=active["id"], date=active["date"], topic=active["topic"],
            rationale=active["rationale"], script=active["body_md"],
        )

    rp_id = storage.add_roleplay(
        date_iso=today_iso,
        topic=result.get("topic", "(untitled)"),
        rationale=result.get("rationale", ""),
        body_md=result.get("body_md", ""),
    )
    return Roleplay(
        id=rp_id, date=today_iso, topic=result.get("topic", ""),
        rationale=result.get("rationale", ""), script=result.get("body_md", ""),
    )


@app.post("/upload")
async def upload_audio(
    file: UploadFile = File(...),
    mode: str = Form(...),
    auto_analyze: bool = Form(True),
):
    """Two flows:

    - `auto_analyze=true` (default): Gemini transcribes → Claude analyzes →
      structured additions/graduations end up in raw_response. Frontend
      routes to tinder-swipe review.

    - `auto_analyze=false` ("discuss mode"): Gemini transcribes only. No
      Claude call. Session stored with empty raw_response — decisions come
      later via `apply_review.py` (Claude + user talk it through, then a
      script writes the agreed additions/graduations). In roleplay mode we
      finalize the roleplay immediately since the practice portion is done;
      the review just materializes async.
    """
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

    active_errors = storage.list_active_errors(limit=ACTIVE_ERROR_LIMIT)
    rp_id = None
    if mode == "roleplay":
        active = storage.get_active_roleplay()
        rp_id = active["id"] if active else None

    print("[upload] gemini transcribing...", flush=True)
    t0 = time.perf_counter()
    transcript = _gemini_transcribe(audio_bytes, mime)
    print(f"[upload] transcribed ({time.perf_counter() - t0:.1f}s, {len(transcript)} chars)",
          flush=True)

    if auto_analyze:
        print("[upload] claude analyzing...", flush=True)
        t1 = time.perf_counter()
        analysis = claude_analyze(transcript, active_errors)
        print(f"[upload] analyzed ({time.perf_counter() - t1:.1f}s, "
              f"{len(analysis.get('additions', []))} additions, "
              f"{len(analysis.get('graduations', []))} graduations)", flush=True)
    else:
        print("[upload] discuss-mode: skipping claude analyze", flush=True)
        analysis = {
            "summary": "",
            "fluency_notes": "",
            "additions": [],
            "graduations": [],
        }
    analysis["transcript"] = transcript

    session_id = storage.new_session_id()
    ext = mime.split("/")[-1]
    (storage.SESSIONS / f"{session_id}.{ext}").write_bytes(audio_bytes)

    storage.add_session(
        sid=session_id,
        roleplay_id=rp_id,
        mode=mode,
        audio_ext=ext,
        transcript=transcript,
        summary=analysis.get("summary", ""),
        fluency_notes=analysis.get("fluency_notes", ""),
        raw_response=analysis,
    )

    # Discuss-mode + roleplay: retire the roleplay now. Review will be
    # applied async via apply_review.py without touching roleplay state.
    if not auto_analyze and mode == "roleplay" and rp_id:
        storage.finish_roleplay(rp_id)
        print(f"[upload] discuss-mode: finished roleplay {rp_id}", flush=True)

    return {"session_id": session_id, "mode": mode, "auto_analyzed": auto_analyze}


@app.get("/today/practice/state", response_model=PracticeState)
def get_practice_state():
    """Tells the frontend which step to land on. Drives resume-after-bail."""
    sess = storage.latest_pending_session()
    if sess is None:
        return PracticeState(step="roleplay")

    analysis = sess.get("raw_response") or {}
    decisions = storage.read_decisions(sess["id"])
    addition_ids = [a["id"] for a in analysis.get("additions", []) if "id" in a]
    grad_ids = [g["id"] for g in analysis.get("graduations", []) if "id" in g]
    if any(aid not in decisions for aid in addition_ids):
        return PracticeState(step="additions", session_id=sess["id"])
    if any(gid not in decisions for gid in grad_ids):
        return PracticeState(step="graduations", session_id=sess["id"])
    # All decisions made — finalize and clear.
    _finalize_session(sess)
    return PracticeState(step="roleplay")


@app.get("/today/review", response_model=ReviewBundle)
def get_today_review():
    """Returns only undecided additions/graduations from the latest pending session."""
    sess = storage.latest_pending_session()
    analysis = (sess or {}).get("raw_response") or {}
    decisions = storage.read_decisions(sess["id"]) if sess else {}
    additions = [
        ErrorCandidate(
            id=a["id"], title=a["title"], you_said=a["you_said"],
            native=a["native"],
            register=a.get("register", ""),
            l1_diagnosis=a.get("l1_diagnosis", ""),
            note=a.get("note", ""),
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


def _finalize_session(sess: dict) -> None:
    """Retire the linked roleplay if this was a roleplay-mode session. There's
    no `review_done` flag on disk — it's derived from decisions + candidates
    per `storage.is_review_done`. So finalize is just the roleplay flip."""
    if sess.get("mode") == "roleplay" and sess.get("roleplay_id"):
        storage.finish_roleplay(sess["roleplay_id"])


@app.post("/sessions/{session_id}/decide")
def decide(session_id: str, decision: Decision):
    if decision.action not in ("added", "skipped", "graduated", "kept"):
        raise HTTPException(status_code=400, detail=f"unknown action {decision.action!r}")

    sess = storage.get_session(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    if storage.is_review_done(session_id):
        raise HTTPException(status_code=409, detail="session already complete")

    analysis = sess.get("raw_response") or {}
    decisions = storage.read_decisions(session_id)

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
        # Persist register + l1_diagnosis as first-class sections so they
        # survive round-tripping through the errors book and can be re-read
        # during drill generation. Empty sections are dropped so old-format
        # errors don't get phantom headers.
        parts = [
            f"**you_said**: {c.get('you_said', '')}",
            f"**native**: {c.get('native', '')}",
        ]
        if c.get("register"):
            parts.append(f"**register**: {c['register']}")
        if c.get("l1_diagnosis"):
            parts.append(f"**l1_diagnosis**: {c['l1_diagnosis']}")
        if c.get("note"):
            parts.append(f"**note**: {c['note']}")
        body_md = "\n\n".join(parts)
        today_iso = _today_local().isoformat()
        storage.add_error(
            title=c.get("title", "(untitled)"),
            body_md=body_md,
            source_session_id=session_id,
            source_candidate_id=decision.candidate_id,
            today_iso=today_iso,
        )
    elif decision.action == "graduated":
        g = next(g for g in analysis["graduations"] if g["id"] == decision.candidate_id)
        err_id = g.get("error_id")
        if err_id is not None:
            storage.graduate_error(int(err_id))

    storage.append_decision(session_id, decision.candidate_id, decision.action)

    # If every candidate is now decided, finalize (flip roleplay to done).
    if storage.is_review_done(session_id):
        _finalize_session(sess)

    return {"recorded": True}


@app.get("/today/drill", response_model=list[DrillCard])
def get_today_drill():
    today_iso = _today_local().isoformat()
    existing = storage.get_drill(today_iso)
    if existing:
        cards = existing.get("cards", [])
        return [
            DrillCard(
                id=str(i),
                prompt=c.get("prompt", ""),
                answer=c.get("answer", ""),
                source_error_id=str(c["source_error_id"]) if c.get("source_error_id") is not None else None,
            )
            for i, c in enumerate(cards)
        ]

    # Generate via Opus.
    active_errors = storage.list_active_errors(limit=ACTIVE_ERROR_LIMIT)
    recent = storage.recent_sessions(n=5)

    prompt = build_drill_prompt(
        active_errors=active_errors,
        recent_sessions=recent,
    )
    print(f"[drill] generating for {today_iso}...", flush=True)
    t0 = time.perf_counter()
    result = opus_emit_tool(prompt, DRILL_TOOL)
    print(f"[drill] generated for {today_iso}: {len(result.get('cards', []))} cards "
          f"({time.perf_counter() - t0:.1f}s)", flush=True)
    cards = result.get("cards", [])
    if not cards:
        raise HTTPException(status_code=503, detail="Drill cold-start: not enough material to generate")

    storage.add_drill(
        date_iso=today_iso,
        rationale=result.get("rationale", ""),
        cards=cards,
    )
    saved = storage.get_drill(today_iso)
    return [
        DrillCard(
            id=str(i),
            prompt=c.get("prompt", ""),
            answer=c.get("answer", ""),
            source_error_id=str(c["source_error_id"]) if c.get("source_error_id") is not None else None,
        )
        for i, c in enumerate((saved or {}).get("cards", []))
    ]
