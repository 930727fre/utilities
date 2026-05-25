PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS roleplays (
    id          TEXT PRIMARY KEY,
    date        TEXT NOT NULL,
    topic       TEXT NOT NULL,
    rationale   TEXT,
    body_md     TEXT,
    status      TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('active','done')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    roleplay_id     TEXT,
    transcript      TEXT,
    summary         TEXT,
    fluency_notes   TEXT,
    raw_response    TEXT,
    review_done     INTEGER NOT NULL DEFAULT 0 CHECK (review_done IN (0,1)),
    uploaded_at     TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (roleplay_id) REFERENCES roleplays(id)
);

CREATE TABLE IF NOT EXISTS errors (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    title             TEXT NOT NULL,
    body_md           TEXT NOT NULL,
    first_seen_date   TEXT,
    last_seen_date    TEXT,
    source_session_id TEXT,
    status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','graduated')),
    graduated_at      TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (source_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS drills (
    id            TEXT PRIMARY KEY,
    date          TEXT NOT NULL DEFAULT '',
    rationale     TEXT,
    completed_at  TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS drill_cards (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    drill_id         TEXT NOT NULL,
    order_index      INTEGER NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('translate','fill_blank')),
    prompt           TEXT NOT NULL,
    answer           TEXT NOT NULL,
    rationale        TEXT,
    source_error_id  INTEGER,
    FOREIGN KEY (drill_id) REFERENCES drills(id),
    FOREIGN KEY (source_error_id) REFERENCES errors(id)
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
