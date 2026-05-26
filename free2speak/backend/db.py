import sqlite3
from pathlib import Path

DB_PATH = Path("/data/free2speak.db")
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    # Migrations for existing DBs created before a column was added. sqlite has no
    # IF NOT EXISTS on ADD COLUMN, so we try and swallow the "duplicate column" error.
    for table, col, defn in (
        ("drills", "date", "TEXT NOT NULL DEFAULT ''"),
        ("sessions", "mode", "TEXT NOT NULL DEFAULT 'roleplay' CHECK (mode IN ('roleplay','freestyle'))"),
        ("sessions", "decisions", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
        except sqlite3.OperationalError:
            pass
    # Backfill drills.date for rows created before the column existed — derive from
    # the UTC `created_at` plus the Asia/Taipei +8h offset.
    conn.execute(
        "UPDATE drills SET date = date(created_at, '+8 hours') "
        "WHERE date = '' OR date IS NULL"
    )
    # Indexes that depend on migrated columns live here (not in schema.sql) so they
    # don't run before the ALTER TABLE on existing DBs.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_drills_date ON drills(date)")
    # Dedupe existing active roleplays before creating the partial unique index.
    # Pre-migration the endpoint inserted every roleplay with status='active', so
    # legacy DBs have many. Keep the most recent (by created_at, tie-break on id)
    # active; demote the rest to 'done'.
    conn.execute("""
        UPDATE roleplays SET status='done'
        WHERE status='active' AND id NOT IN (
            SELECT id FROM roleplays
            WHERE status='active'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        )
    """)
    # Partial unique index: at most one active roleplay across the table. Created
    # after schema/ALTER ran so it sees the right column. Will reject any future
    # concurrent inserts of status='active' — INSERT-then-SELECT in the endpoint
    # catches IntegrityError and returns the already-existing active row.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_roleplay "
        "ON roleplays(status) WHERE status='active'"
    )
    conn.commit()
