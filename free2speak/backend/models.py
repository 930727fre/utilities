from pydantic import BaseModel


class ErrorCandidate(BaseModel):
    id: str
    title: str
    you_said: str
    native: str
    note: str = ""


class GraduateCandidate(BaseModel):
    id: str
    title: str
    evidence: str
    occurrences: int = 1


class Roleplay(BaseModel):
    id: str
    date: str
    topic: str
    rationale: str
    script: str


class DrillCard(BaseModel):
    id: str
    prompt: str
    answer: str
    source_error_id: str | None = None


class Decision(BaseModel):
    candidate_id: str
    action: str  # 'added' | 'skipped' | 'graduated' | 'kept'


class ReviewBundle(BaseModel):
    additions: list[ErrorCandidate]
    graduations: list[GraduateCandidate]


class PracticeState(BaseModel):
    step: str  # 'roleplay' | 'additions' | 'graduations'
    session_id: str | None = None


class TodayStats(BaseModel):
    streak_count: int
    practice_done_today: bool
    drill_done_today: bool
    active_errors_count: int
