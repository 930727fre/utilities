from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import sqlite3
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from models import Card, CardUpdate, Settings, SettingsUpdate, SyncPayload

TZ = ZoneInfo("Asia/Taipei")

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
            created_at TEXT DEFAULT ''
        )
    """)
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
        "streak_count": s.get('streak_count', '0'),
        "due_count": due_count,
        "new_available": new_available,
    }


@app.get("/cards/queue")
def get_queue():
    conn = get_db()
    s = fetch_settings(conn)

    now = datetime.now(timezone.utc).isoformat()
    due_cards = conn.execute(
        "SELECT * FROM cards WHERE state != 0 AND due != '' AND due <= ? ORDER BY due ASC",
        (now,)
    ).fetchall()

    if due_cards:
        cards = [dict(r) for r in due_cards]
    else:
        daily_new_count = int(s.get('daily_new_count', 0) or 0)
        remaining = max(0, 20 - daily_new_count)
        new_cards = conn.execute(
            "SELECT * FROM cards WHERE state = 0 ORDER BY created_at ASC LIMIT ?",
            (remaining,)
        ).fetchall()
        cards = [dict(r) for r in new_cards]

    conn.close()
    return {
        "cards": cards,
        "daily_new_count": s.get('daily_new_count', '0'),
        "fsrs_params": s.get('fsrs_params', ''),
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
               elapsed_days, scheduled_days, lapses, state, last_review, lang, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (c.id, c.word, c.sentence, c.note, c.due, c.stability, c.difficulty,
             c.elapsed_days, c.scheduled_days, c.lapses, c.state, c.last_review,
             c.lang, c.created_at),
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
    return dict(row)


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
               elapsed_days, scheduled_days, lapses, state, last_review, lang, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (c.id, c.word, c.sentence, c.note, c.due, c.stability, c.difficulty,
             c.elapsed_days, c.scheduled_days, c.lapses, c.state, c.last_review,
             c.lang, c.created_at),
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
