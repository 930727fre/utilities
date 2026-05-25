from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import asyncio
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
from fsrs import Scheduler as FSRSScheduler, Card as FSRSCard, Rating as FSRSRating, State as FSRSState
from models import Card, CardUpdate, ReviewRequest, Settings, SettingsUpdate, SyncPayload

TZ = ZoneInfo("Asia/Taipei")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "/data/flashcard.db"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cards (
            id TEXT PRIMARY KEY,
            word TEXT NOT NULL,
            sentence TEXT DEFAULT '',
            note TEXT DEFAULT '',
            due TEXT DEFAULT '',
            stability REAL DEFAULT 0,
            difficulty REAL DEFAULT 0,
            elapsed_days INTEGER DEFAULT 0,
            scheduled_days INTEGER DEFAULT 0,
            lapses INTEGER DEFAULT 0,
            state INTEGER DEFAULT 0,
            last_review TEXT DEFAULT '',
            lang TEXT DEFAULT 'en',
            created_at TEXT DEFAULT '',
            reps INTEGER DEFAULT 0,
            learning_steps INTEGER DEFAULT 0
        )
    """)
    for col, defn in [('reps', 'INTEGER DEFAULT 0'), ('learning_steps', 'INTEGER DEFAULT 0')]:
        try:
            conn.execute(f"ALTER TABLE cards ADD COLUMN {col} {defn}")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
    """)
    defaults = {
        'fsrs_params': '',
        'streak_count': '0',
        'streak_last_date': '',
        'daily_new_count': '0',
        'last_modified': now_iso(),
    }
    for k, v in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
        )
    conn.commit()


def fetch_settings(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r['key']: r['value'] for r in rows}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/heartbeat")
async def heartbeat():
    async def stream():
        while True:
            yield "data: ping\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/stats")
def get_stats():
    conn = get_db()
    s = fetch_settings(conn)
    s = apply_streak_logic(conn, s)

    now = datetime.now(timezone.utc).isoformat()
    due_count = conn.execute(
        "SELECT COUNT(*) FROM cards WHERE state != 0 AND due != '' AND due <= ?", (now,)
    ).fetchone()[0]
    new_count = conn.execute(
        "SELECT COUNT(*) FROM cards WHERE state = 0"
    ).fetchone()[0]
    daily_new_count = int(s.get('daily_new_count', 0) or 0)
    new_available = max(0, min(new_count, 20 - daily_new_count))

    conn.close()
    return {
        "streak_count": int(s.get('streak_count', '0') or '0'),
        "due_count": due_count,
        "new_available": new_available,
    }


@app.get("/cards/queue")
def get_queue():
    conn = get_db()
    s = fetch_settings(conn)

    now = datetime.now(timezone.utc).isoformat()
    due_cards = conn.execute(
        "SELECT * FROM cards WHERE state != 0 AND due != '' AND due <= ? ORDER BY RANDOM()",
        (now,)
    ).fetchall()

    daily_new_count = int(s.get('daily_new_count', 0) or 0)
    remaining = max(0, 20 - daily_new_count)
    new_cards = conn.execute(
        "SELECT * FROM cards WHERE state = 0 ORDER BY created_at ASC LIMIT ?",
        (remaining,)
    ).fetchall()

    conn.close()
    return {
        "due": [dict(r) for r in due_cards],
        "new": [dict(r) for r in new_cards],
        "daily_new_count": daily_new_count,
    }


@app.get("/cards/search")
def search_cards(q: str = ""):
    if not q.strip():
        return []
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM cards WHERE LOWER(word) LIKE ? LIMIT 8",
        (f"%{q.lower()}%",)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/cards")
def get_cards():
    conn = get_db()
    rows = conn.execute("SELECT * FROM cards").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/cards/batch")
def batch_add_cards(cards: list[Card]):
    conn = get_db()
    for c in cards:
        conn.execute(
            """
            INSERT OR IGNORE INTO cards
              (id, word, sentence, note, due, stability, difficulty,
               elapsed_days, scheduled_days, lapses, state, last_review, lang, created_at,
               reps, learning_steps)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (c.id, c.word, c.sentence, c.note, c.due, c.stability, c.difficulty,
             c.elapsed_days, c.scheduled_days, c.lapses, c.state, c.last_review,
             c.lang, c.created_at, c.reps, c.learning_steps),
        )
    conn.commit()
    conn.close()
    return {"inserted": len(cards)}


@app.patch("/cards/{card_id}")
def update_card(card_id: str, body: CardUpdate):
    conn = get_db()
    if not conn.execute("SELECT id FROM cards WHERE id = ?", (card_id,)).fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Card not found")

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE cards SET {set_clause} WHERE id = ?",
            (*updates.values(), card_id),
        )
        conn.commit()

    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    conn.close()
    result = dict(row)
    print(f"[update_card] id={card_id} word={result.get('word')} state={result.get('state')} due={result.get('due')}", flush=True)
    return result


@app.post("/cards/{card_id}/examples")
def regenerate_examples(card_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Card not found")

    d = dict(row)
    word = d["word"]
    note = d["note"] or ""

    prompt = (
        "You are given an English word and its Chinese definitions grouped by part of speech "
        "(e.g. n., v., vt., vi., adj., adv., prep., conj., pron., interj.).\n"
        "For EACH part-of-speech group, write ONE natural English example sentence using the word in that role.\n"
        "Output ONLY a numbered list of sentences, one per group. No commentary, no translations, no quotes.\n\n"
        f"Word: {word}\n"
        f"Definitions: {note}\n"
        "Examples:"
    )

    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.3},
            },
            timeout=120,
        )
        resp.raise_for_status()
        raw = resp.json()["response"].strip()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=502, detail=f"Ollama call failed: {e}")

    # Parse "1. ...\n2. ..." → list of sentences, strip leading numbering
    lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    sentences = []
    for ln in lines:
        m = re.match(r"^\d+[.)]\s*(.*)$", ln)
        sentences.append(m.group(1).strip() if m else ln)
    if not sentences:
        conn.close()
        raise HTTPException(status_code=502, detail="Ollama returned no sentences")

    new_sentence = "\n".join(f"{i}. {s}" for i, s in enumerate(sentences, 1))
    conn.execute("UPDATE cards SET sentence = ? WHERE id = ?", (new_sentence, card_id))
    conn.commit()
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    conn.close()
    result = dict(row)
    print(f"[regenerate_examples] id={card_id} word={word} n={len(sentences)}", flush=True)
    return result


@app.post("/cards/{card_id}/review")
def review_card(card_id: str, body: ReviewRequest):
    conn = get_db()
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Card not found")

    d = dict(row)
    was_new = d['state'] == 0
    now = datetime.now(timezone.utc)

    # py-fsrs has no New(0) state; new cards are constructed bare
    if was_new:
        fsrs_card = FSRSCard()
    else:
        fsrs_card = FSRSCard(
            state=FSRSState(d['state']),
            step=d['learning_steps'],
            stability=d['stability'] or None,
            difficulty=d['difficulty'] or None,
            due=datetime.fromisoformat(d['due']) if d['due'] else None,
            last_review=datetime.fromisoformat(d['last_review']) if d['last_review'] else None,
        )

    scheduler = FSRSScheduler()
    next_card, _ = scheduler.review_card(fsrs_card, FSRSRating(body.rating), now)

    old_last_review = datetime.fromisoformat(d['last_review']) if d['last_review'] else now
    elapsed_days = (now - old_last_review).days
    scheduled_days = max(0, (next_card.due - now).days) if next_card.due else 0
    new_lapses = d['lapses'] + (1 if d['state'] == 2 and int(next_card.state) == 3 else 0)
    new_reps = d['reps'] + 1

    conn.execute("""
        UPDATE cards SET
            due=?, stability=?, difficulty=?, elapsed_days=?, scheduled_days=?,
            lapses=?, state=?, last_review=?, reps=?, learning_steps=?
        WHERE id=?
    """, (
        next_card.due.isoformat() if next_card.due else '',
        next_card.stability or 0,
        next_card.difficulty or 0,
        elapsed_days,
        scheduled_days,
        new_lapses,
        int(next_card.state),
        now.isoformat(),
        new_reps,
        next_card.step if next_card.step is not None else 0,
        card_id,
    ))

    if was_new:
        s = fetch_settings(conn)
        new_daily = int(s.get('daily_new_count', '0') or '0') + 1
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                     ('daily_new_count', str(new_daily)))

    conn.commit()
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    conn.close()
    result = dict(row)
    print(f"[review_card] id={card_id} word={result.get('word')} rating={body.rating} state={result.get('state')} due={result.get('due')}", flush=True)
    return result


def apply_streak_logic(conn: sqlite3.Connection, s: dict) -> dict:
    """Increment/reset streak and reset daily_new_count when a new day starts."""
    now = datetime.now(TZ)
    today = now.date()

    raw_last = s.get('streak_last_date', '').strip()
    try:
        last_date = datetime.fromisoformat(raw_last).astimezone(TZ).date() if raw_last else None
    except ValueError:
        last_date = None

    if last_date != today:
        yesterday = today - timedelta(days=1)
        current_streak = int(s.get('streak_count', 0) or 0)
        new_streak = current_streak + 1 if last_date == yesterday else 1
        now_iso = now.isoformat()

        updates = {
            'streak_count': str(new_streak),
            'streak_last_date': now_iso,
            'daily_new_count': '0',
            'last_modified': now_iso,
        }
        for k, v in updates.items():
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )
        conn.commit()
        s.update(updates)

    return s


@app.get("/settings")
def get_settings():
    conn = get_db()
    s = fetch_settings(conn)
    s = apply_streak_logic(conn, s)
    conn.close()
    return s


@app.patch("/settings")
def update_settings(body: SettingsUpdate):
    conn = get_db()
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    updates['last_modified'] = now_iso()
    for k, v in updates.items():
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v))
        )
    conn.commit()
    s = fetch_settings(conn)
    conn.close()
    return s


@app.post("/sync")
def sync_all(payload: SyncPayload):
    conn = get_db()
    for c in payload.cards:
        conn.execute(
            """
            INSERT OR REPLACE INTO cards
              (id, word, sentence, note, due, stability, difficulty,
               elapsed_days, scheduled_days, lapses, state, last_review, lang, created_at,
               reps, learning_steps)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (c.id, c.word, c.sentence, c.note, c.due, c.stability, c.difficulty,
             c.elapsed_days, c.scheduled_days, c.lapses, c.state, c.last_review,
             c.lang, c.created_at, c.reps, c.learning_steps),
        )
    s = payload.settings.model_dump()
    s['last_modified'] = now_iso()
    for k, v in s.items():
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v))
        )
    conn.commit()
    conn.close()
    return {"ok": True}
