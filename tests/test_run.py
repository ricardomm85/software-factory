"""Tests for the spine: runs, phases (born fail), events, and settlement."""

import pytest

from factory.db import connect
from factory.run import Run


@pytest.fixture()
def conn(tmp_path):
    return connect(tmp_path / "factory.db")


def test_schema_is_idempotent_and_wal(tmp_path):
    c = connect(tmp_path / "a.db")
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c2 = connect(tmp_path / "a.db")  # reopening must not fail
    assert c2.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_run_create_and_finish(conn):
    run = Run.create(conn, request="do a thing", agent="scout", model="zai/glm-5.3-flash")
    assert conn.execute("SELECT status FROM sessions WHERE id = ?", (run.id,)).fetchone()[0] == "running"
    run.finish(accepted=True)
    assert conn.execute("SELECT status FROM sessions WHERE id = ?", (run.id,)).fetchone()[0] == "accepted"


def test_phase_born_fail_then_pass_on_clean_exit(conn):
    run = Run.create(conn, request="x", agent="scout", model="m")
    with run.phase(name="build", kind="agent", owner="builder") as phase:
        assert conn.execute("SELECT status FROM phases WHERE id = ?", (phase.id,)).fetchone()[0] == "fail"
    row = conn.execute("SELECT status, error FROM phases WHERE id = ?", (phase.id,)).fetchone()
    assert row["status"] == "pass" and row["error"] is None


def test_phase_stays_fail_on_exception(conn):
    run = Run.create(conn, request="x", agent="scout", model="m")
    with pytest.raises(RuntimeError):
        with run.phase(name="build", kind="agent", owner="builder") as phase:
            raise RuntimeError("boom")
    row = conn.execute("SELECT status, error FROM phases WHERE id = ?", (phase.id,)).fetchone()
    assert row["status"] == "fail" and "boom" in row["error"]


def test_events_carry_run_and_phase_ids(conn):
    run = Run.create(conn, request="x", agent="scout", model="m")
    with run.phase(name="plan", kind="agent", owner="planner") as phase:
        phase.log("attempt", {"n": 1})
    rows = conn.execute("SELECT run_id, phase_id, type FROM events").fetchall()
    assert len(rows) == 1 and rows[0]["type"] == "attempt" and rows[0]["run_id"] == run.id


def test_phases_are_sequential(conn):
    run = Run.create(conn, request="x", agent="scout", model="m")
    with run.phase(name="plan", owner="planner"):
        pass
    with run.phase(name="build", owner="builder"):
        pass
    seqs = [p["seq"] for p in run.phases()]
    assert seqs == [1, 2]


def test_get_unknown_run_raises(conn):
    with pytest.raises(ValueError, match="unknown run id"):
        Run.get(conn, "nope1234")


def test_connection_accepts_cross_thread_writes(conn):
    """The streaming tracer logs from the pump thread — sqlite must allow it."""
    run = Run.create(conn, request="x", agent="scout", model="m")
    errors = []

    def _insert():
        try:
            conn.execute(
                "INSERT INTO events (run_id, phase_id, type, payload, created_at)"
                " VALUES (?, NULL, 'x', '{}', 'now')",
                (run.id,),
            )
            conn.commit()
        except Exception as exc:  # sqlite3.ProgrammingError without check_same_thread=False
            errors.append(exc)

    import threading

    t = threading.Thread(target=_insert)
    t.start()
    t.join()
    assert not errors, f"cross-thread insert failed: {errors}"
