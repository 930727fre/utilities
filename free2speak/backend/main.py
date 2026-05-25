from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, UploadFile, File
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
    _ = await file.read()
    return {
        "session_id": "stub-session-1",
        "date": "2026-05-23",
        "topic": "apartment",
    }


@app.get("/today/review", response_model=ReviewBundle)
def get_today_review():
    return ReviewBundle(
        additions=[
            ErrorCandidate(
                id="add-1",
                title="break the lease vs. cancel the contract",
                you_said="Can I cancel the contract?",
                native="Can I break the lease?",
            ),
            ErrorCandidate(
                id="add-2",
                title="room for negotiation",
                you_said="Can we negotiate more?",
                native="Is there room for negotiation?",
            ),
            ErrorCandidate(
                id="add-3",
                title="follow up (intransitive)",
                you_said="follow up it",
                native="follow up on it / follow it up",
            ),
        ],
        graduations=[
            GraduateCandidate(
                id="grad-1",
                title="used to + V (過去習慣)",
                evidence="I used to live in Taipei before moving here.",
            ),
            GraduateCandidate(
                id="grad-2",
                title="Thanks for X-ing",
                evidence="Thanks for showing me around.",
            ),
        ],
    )


@app.post("/errors/additions")
def apply_additions(body: AdditionsApply):
    return {"added": len(body.candidate_ids)}


@app.post("/errors/graduations")
def apply_graduations(body: GraduationsApply):
    return {"graduated": len(body.error_ids)}


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
