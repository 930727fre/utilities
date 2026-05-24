from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware

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

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/today/stats", response_model=TodayStats)
def get_today_stats():
    return TodayStats(
        streak_count=7,
        practice_done_today=False,
        drill_done_today=False,
        active_errors_count=42,
    )


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
