"""SQLite trace store (WAL). Agents write, readers poll — one transport, no exceptions.

Every connection opens WAL so reads never block the running writer. The db is a
queryable mirror of files on disk; losing it loses nothing that matters yet.
"""

import sqlite3
import threading
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
    launched_by TEXT NOT NULL DEFAULT '',         -- what created it: '' (CLI) | 'board' (factory serve)
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

-- The board (Phases 6-8): tasks are cards; runs are their executions.
CREATE TABLE IF NOT EXISTS tasks (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    prompt     TEXT NOT NULL DEFAULT '',
    repo       TEXT NOT NULL,                     -- resolved path of the target repo
    origin     TEXT NOT NULL DEFAULT 'owner',     -- owner | system (a card the factory discovered)
    runnable   INTEGER NOT NULL DEFAULT 1,        -- 0 = a system-proposed card awaiting the owner
    status     TEXT NOT NULL DEFAULT 'backlog',   -- backlog | working | in_pr | archived
    branch     TEXT NOT NULL DEFAULT '',          -- task branch (branch per task, Phase 8)
    pr_url     TEXT NOT NULL DEFAULT '',          -- set when delivery landed the PR
    note       TEXT NOT NULL DEFAULT '',          -- last failure reason / status hint
    origin_run TEXT NOT NULL DEFAULT '',          -- system cards: the run that discovered them
    latest_run TEXT NOT NULL DEFAULT '',          -- most recent session id for this card
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_board ON tasks(status, created_at);

-- Human gates (Phase 7): the chain parks, the owner answers, the same session resumes.
CREATE TABLE IF NOT EXISTS pauses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT REFERENCES sessions(id),     -- NULL only in unit tests without a run
    phase_id    INTEGER REFERENCES phases(id),
    task_id     TEXT REFERENCES tasks(id),
    type        TEXT NOT NULL,                    -- questions | plan_approval | pr_approval
    request     TEXT NOT NULL DEFAULT '{}',       -- JSON payload shown to the owner
    answer      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'waiting',  -- waiting | answered | approved | rejected | expired
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_pauses_run ON pauses(run_id, id);
"""


class _FrozenCursor:
    """A cursor whose statement was fully consumed before the lock was released.

    ``execute`` materializes every row and captures ``lastrowid``/``rowcount``
    while still inside the connection lock, then closes the real cursor: by the
    time another thread may run a statement, this one is finished and reset —
    the shared statement cache can never be re-entered mid-fetch.
    """

    def __init__(self, cur: sqlite3.Cursor):
        self.description = cur.description
        self._rows = cur.fetchall() if cur.description is not None else []
        self.lastrowid = cur.lastrowid
        self.rowcount = cur.rowcount
        cur.close()

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list:
        rows, self._rows = self._rows, []
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class LockedConnection(sqlite3.Connection):
    """A trace connection safe to share across threads (``check_same_thread=False``).

    The board runs one worker thread per repo, each with its own pi event-pump
    thread, plus the dispatcher, the PR poller and HTTP handlers — all on this
    one connection. CPython's sqlite3 is not safe for that: identical SQL texts
    share one cached prepared statement (two threads mid-fetch on it raise
    ``sqlite3.InterfaceError``), and one thread's ``commit()`` may close another
    thread's open implicit transaction. A single RLock serializes every entry
    into the connection — execute (through full result consumption), commit,
    rollback — so only one thread is ever inside this connection's sqlite3
    state. A commit landing after another thread's commit is a harmless no-op;
    each recorded statement is autonomous and intended, so statement-level
    serialization preserves exactly the single-threaded semantics.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._serial_lock = threading.RLock()

    def execute(self, sql: str, parameters=()) -> _FrozenCursor:
        with self._serial_lock:
            return _FrozenCursor(super().execute(sql, parameters))

    def commit(self) -> None:
        with self._serial_lock:
            super().commit()

    def rollback(self) -> None:
        with self._serial_lock:
            super().rollback()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the trace db with WAL and the schema applied.

    ``check_same_thread=False`` is required: the streaming tracer inserts events
    from the pump thread while the main thread owns the connection, and the board
    (``factory serve``) runs one worker thread per repo plus request handlers on
    this single connection — hence the :class:`LockedConnection` serialization.
    Existing dbs get the ``repo`` and ``launched_by``
    columns via ALTER — one trace now serves several target repos, so every run
    must say which repo it touched, and the board's reaper must be able to tell
    its own runs from CLI runs sharing this db. The ``tasks``/``pauses`` tables
    are CREATE IF NOT EXISTS: older dbs gain them on first open.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False, factory=LockedConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "repo" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN repo TEXT NOT NULL DEFAULT ''")
        conn.commit()
    if "launched_by" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN launched_by TEXT NOT NULL DEFAULT ''")
        conn.commit()
    return conn


def trace_exemptions(repo_dir: Path, db_path: Path) -> set[str]:
    """Trace db + WAL sidecars as repo-relative posix paths.

    The writes boundary (gate rollbacks, ``git add -A``, reviewer diffs) must
    never treat the factory's own trace as agent debris: a ``git clean`` here
    deletes the trace mid-run and the open connection keeps writing to an
    unlinked inode — the trace is lost silently (surfaced live in Phase 5).
    Empty when the trace lives at home and the target repo is elsewhere —
    the target's ``git clean`` cannot touch it.
    """
    try:
        rel = db_path.resolve().relative_to(repo_dir.resolve())
    except ValueError:
        return set()
    return {
        rel.as_posix(),
        (rel.parent / f"{rel.name}-wal").as_posix(),
        (rel.parent / f"{rel.name}-shm").as_posix(),
    }
