"""Tests for the read-only observability UI: routes, escaping, isolation, enforcement."""

import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from factory.db import connect
from factory.run import Run
from factory.ui import fmt_duration, make_handler, render_index, render_run, ui_connect


def seed(conn) -> str:
    """One finished run: a passing agent phase with streaming, a failed review phase."""
    run = Run.create(conn, request="ship the <b>bold</b> feature", agent="builder", model="m-1")
    with run.phase(name="build", kind="agent", owner="builder") as phase:
        phase.log("attempt", {"attempt": 1, "session_id": "r1-builder"})
        phase.log("pi_event", {"type": "tool_call", "name": "write"})
        phase.log("pi_event", {"type": "message", "chars": 10})
        phase.log("envelope_ok", {"attempt": 1, "status": "success"})
    try:
        with run.phase(name="review", kind="agent", owner="reviewer") as phase:
            raise RuntimeError("boom: the reviewer exploded")
    except RuntimeError:
        pass
    run.finish(accepted=True)
    return run.id


@pytest.fixture()
def traced_db(tmp_path):
    db_path = tmp_path / "factory.db"
    conn = connect(db_path)
    run_id = seed(conn)
    conn.close()
    return db_path, run_id


def test_index_lists_runs_and_escapes_html(traced_db):
    db_path, run_id = traced_db
    page = render_index(ui_connect(db_path))
    assert run_id in page and "accepted" in page
    assert "&lt;b&gt;bold&lt;/b&gt;" in page          # request text is escaped
    assert "<b>bold</b>" not in page                  # nothing raw survives


def test_run_page_waterfall_errors_and_pi_counts(traced_db):
    db_path, run_id = traced_db
    page, code = render_run(ui_connect(db_path), run_id)
    assert code == 200
    assert "#1" in page and "#2" in page              # waterfall in seq order
    assert "build" in page and "review" in page
    assert "RuntimeError: boom: the reviewer exploded" in page  # failure surfaces
    assert "2 pi stream events" in page               # streaming shown as counts
    assert "tool_call × 1" in page
    assert "envelope_ok" in page                      # orchestrator timeline present


def test_unknown_run_is_404(traced_db):
    db_path, _ = traced_db
    page, code = render_run(ui_connect(db_path), "nope")
    assert code == 404 and "No run with id" in page


def test_fmt_duration():
    assert fmt_duration("2026-01-01T00:00:00+00:00", "2026-01-01T00:01:05+00:00") == "65s"
    assert fmt_duration(None, "2026-01-01T00:01:05+00:00") == ""   # still running
    assert fmt_duration("bad", "worse") == ""


def test_ui_connection_refuses_writes(traced_db):
    db_path, _ = traced_db
    conn = ui_connect(db_path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute(
            "INSERT INTO sessions (id, request, status, started_at) VALUES ('x', 'x', 'running', 'now')"
        )


def test_server_routes_end_to_end(traced_db):
    db_path, run_id = traced_db
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(f"{base}/") as resp:
            assert resp.status == 200 and run_id.encode() in resp.read()
        with urllib.request.urlopen(f"{base}/run/{run_id}") as resp:
            assert resp.status == 200 and b"phase waterfall" in resp.read()
        for path in (f"{base}/run/unknown", f"{base}/nope"):
            try:
                urllib.request.urlopen(path)
                raise AssertionError(f"{path} should have 404'd")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_ui_reads_while_a_writer_is_mid_transaction(traced_db):
    """WAL contract at the UI level: an uncommitted factory write never blocks the page."""
    db_path, run_id = traced_db
    writer = connect(db_path)  # begins implicit transactions on execute
    writer.execute("BEGIN")
    writer.execute(
        "INSERT INTO sessions (id, request, status, started_at) VALUES ('hot', 'in flight', 'running', 'now')"
    )
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db_path))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/") as resp:
                page = resp.read().decode()
        finally:
            server.shutdown()
            server.server_close()
        assert run_id in page and "hot" not in page  # committed rows only
    finally:
        writer.rollback()
        writer.close()


def test_missing_db_is_friendly_not_crash(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp_path / "absent.db"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/") as resp:
            page = resp.read().decode()
        assert resp.status == 200 and "No trace db" in page
    finally:
        server.shutdown()
        server.server_close()


def test_db_without_tables_is_friendly(tmp_path):
    db_path = tmp_path / "empty.db"
    db_path.write_bytes(b"")  # exists but has no schema
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db_path))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/") as resp:
            page = resp.read().decode()
        assert resp.status == 200 and "trace unavailable" in page
    finally:
        server.shutdown()
        server.server_close()


def test_refresh_only_while_running(traced_db):
    db_path, run_id = traced_db
    finished = render_run(ui_connect(db_path), run_id)[0]
    assert 'http-equiv="refresh"' not in finished
    conn = connect(db_path)
    live = Run.create(conn, request="watch me", agent="builder", model="m-1")
    page = render_run(ui_connect(db_path), live.id)[0]
    assert 'http-equiv="refresh"' in page             # a live run refreshes


# --- regression tests from the multi-agent code review -----------------------


def test_hostile_pi_event_type_is_escaped(traced_db):
    db_path, run_id = traced_db
    conn = connect(db_path)
    run = Run.get(conn, run_id)
    with run.phase(name="pwned", kind="agent", owner="builder") as phase:
        phase.log("pi_event", {"type": "</details><img src=x onerror=alert(1)>"})
    page, code = render_run(ui_connect(db_path), run_id)
    assert code == 200
    assert "<img src=x" not in page                     # nothing raw survives
    assert "&lt;/details&gt;&lt;img src=x onerror=alert(1)&gt;" in page


def test_truncation_marker_reports_omitted_chars(traced_db):
    db_path, run_id = traced_db
    conn = connect(db_path)
    long_request = "x" * 200
    live = Run.create(conn, request=long_request, agent="builder", model="m-1")
    page = render_index(ui_connect(db_path))
    assert f"[+{200 - 96} chars omitted]" in page
    full_page, _ = render_run(ui_connect(db_path), live.id)
    assert long_request in full_page                   # the run page is untruncated


def test_sub_second_phases_get_proportional_bars(traced_db):
    db_path, run_id = traced_db
    page, code = render_run(ui_connect(db_path), run_id)
    assert code == 200
    assert "width:" in page                            # bars render from real seconds


def test_render_error_becomes_a_page_not_a_drop(traced_db, monkeypatch):
    import factory.ui as ui_module

    db_path, _ = traced_db
    monkeypatch.setattr(ui_module, "render_index", lambda conn: 1 / 0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db_path))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/")
            raise AssertionError("expected a 500")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            assert "render error" in body and "ZeroDivisionError" in body
    finally:
        server.shutdown()
        server.server_close()


def test_main_rejects_out_of_range_port(tmp_path):
    from factory.__main__ import main

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "defaults:\n  coding_agent: pi\n  model: m\n"
        "agents:\n  - name: a\n    purpose: p\n"
    )
    for bad_port in ("70000", "-1", "0"):
        assert main(["ui", "--port", bad_port, "--config", str(cfg)]) == 2
