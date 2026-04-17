import sqlite3
import os
from contextlib import contextmanager

DATA_DIR = os.environ.get("DATA_DIR", "/data")
STATIC_DIR = os.environ.get("STATIC_DIR", "/app/static")
DB_PATH = os.path.join(DATA_DIR, "twelve.db")


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS books (
                id          TEXT PRIMARY KEY,
                title       TEXT,
                author      TEXT,
                epub_path   TEXT NOT NULL,
                status      TEXT NOT NULL,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS paragraphs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id         TEXT NOT NULL REFERENCES books(id),
                paragraph_id    TEXT NOT NULL,
                spine_href      TEXT NOT NULL,
                para_index      INTEGER NOT NULL,
                tag             TEXT NOT NULL,
                text            TEXT NOT NULL,
                UNIQUE(book_id, paragraph_id)
            );

            CREATE TABLE IF NOT EXISTS bookmarks (
                book_id         TEXT PRIMARY KEY REFERENCES books(id),
                paragraph_id    TEXT NOT NULL,
                updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
