"""SQLite trace store (WAL). Agents write, readers poll — one transport, no exceptions.

Every connection opens WAL so reads never block the running writer. The db is a
queryable mirror of files on disk; losing it loses nothing that matters yet.
"""

import sqlite3
from pathlib import Path

DEFAULT_DB_NAME = "factory.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    request    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'running',   -- running | accepted | failed
    agent      TEXT,
    model      TEXT,
    repo       TEXT NOT NULL DEFAULT '',          -- target repo the run touched ('' = legacy rows from before --repo)
    started_at TEXT NOT NULL,
    ended_at   TEXT
);

CREATE TABLE IF NOT EXISTS phases (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL REFERENCES sessions(id),
    seq        INTEGER NOT NULL,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,                     -- agent | code
    owner      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'fail',      -- fail until a clean exit flips it
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    error      TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL REFERENCES sessions(id),
    phase_id   INTEGER REFERENCES phases(id),
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);

CREATE TABLE IF NOT EXISTS envelopes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    phase_id   INTEGER NOT NULL REFERENCES phases(id),
    attempt    INTEGER NOT NULL,
    valid      INTEGER NOT NULL,                  -- 1 = parsed against the output type
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the trace db with WAL and the schema applied.

    ``check_same_thread=False`` is required: the streaming tracer inserts events
    from the pump thread while the main thread owns the connection. Existing dbs
    get the ``repo`` column via ALTER — one trace now serves several target
    repos, so every run must say which repo it touched.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "repo" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN repo TEXT NOT NULL DEFAULT ''")
        conn.commit()
    return conn
