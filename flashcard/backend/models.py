from pydantic import BaseModel, ConfigDict
from typing import Optional


class Card(BaseModel):
    id: str
    word: str
    sentence: str = ''
    note: str = ''
    due: str = ''
    stability: float = 0
    difficulty: float = 0
    elapsed_days: int = 0
    scheduled_days: int = 0
    lapses: int = 0
    state: int = 0
    last_review: str = ''
    lang: str = 'en'
    created_at: str = ''


class CardUpdate(BaseModel):
    word: Optional[str] = None
    sentence: Optional[str] = None
    note: Optional[str] = None
    due: Optional[str] = None
    stability: Optional[float] = None
    difficulty: Optional[float] = None
    elapsed_days: Optional[int] = None
    scheduled_days: Optional[int] = None
    lapses: Optional[int] = None
    state: Optional[int] = None
    last_review: Optional[str] = None
    lang: Optional[str] = None


class Settings(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)

    fsrs_params: str = ''
    streak_count: str = '0'
    streak_last_date: str = ''
    daily_new_count: str = '0'
    last_modified: str = ''


class SettingsUpdate(BaseModel):
    fsrs_params: Optional[str] = None
    streak_count: Optional[str] = None
    streak_last_date: Optional[str] = None
    daily_new_count: Optional[str] = None


class SyncPayload(BaseModel):
    cards: list[Card]
    settings: Settings
