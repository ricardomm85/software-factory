"""Tests for the read-only observability pages: routes, escaping, isolation, enforcement."""

import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from factory.db import connect
from factory.run import Run
from factory.web.runs import (
    fmt_duration,
    make_trace_handler,
    render_index,
    render_run,
    render_run_fragment,
    render_runs_fragment,
)
from factory.web.scaffold import phase_label, ui_connect


def seed(conn) -> str:
    """One finished run: a passing agent phase with streaming, a failed review phase."""
    run = Run.create(conn, request="ship the <b>bold</b> feature", agent="builder", model="m-1", repo="/tmp/target")
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
    assert ">target<" in page                         # the repo column shows the basename


def test_phase_label_map():
    assert phase_label("plan") == "planning"
    assert phase_label("build:r2") == "building"
    assert phase_label("review:scorer:r1") == "reviewing (scorer)"
    assert phase_label("score:r1") == "scoring findings"
    assert phase_label("fix:r3") == "fixing findings"
    assert phase_label("deliver") == "opening the PR"
    assert phase_label("checkpoint") == "waiting on you"
    assert phase_label("custom_phase") == "custom_phase"   # honest fallback


def test_run_page_timeline_errors_and_pi_counts(traced_db):
    db_path, run_id = traced_db
    page, code = render_run(ui_connect(db_path), run_id)
    assert code == 200
    assert "#1" in page and "#2" in page              # timeline in seq order
    assert "<code>build</code>" in page and "<code>review</code>" in page  # raw names stay
    assert "building" in page and "reviewing" in page  # plain-English labels
    assert "RuntimeError: boom: the reviewer exploded" in page  # failure surfaces
    assert "2 agent stream events" in page            # streaming shown as one honest line
    assert "tool_call" in page and "1</td>" in page   # the breakdown table is real content
    assert "envelope_ok" in page                      # orchestrator timeline present
    assert "class='tl done'" in page and "class='tl fail'" in page  # step states
    assert "nothing to show for this phase" not in page  # curated detail always says something


def test_open_phase_shows_running_not_failed(traced_db):
    """Phases are born fail: an open (streaming) phase must read as running,
    not as an error — a live plan phase once looked 'failed' to the owner."""
    from factory.db import connect
    from factory.run import Run

    db_path, _ = traced_db
    conn = connect(db_path)
    run = Run.create(conn, "live now", agent="a", model="m")
    conn.execute(
        "INSERT INTO phases (run_id, seq, name, kind, owner, status, started_at)"
        " VALUES (?, 1, 'plan', 'agent', 'planner', 'fail', '2026-01-01T00:00:00+00:00')",
        (run.id,),
    )
    conn.commit()
    page, code = render_run(ui_connect(db_path), run.id)
    assert code == 200
    assert '<span class="badge running">running</span>' in page
    assert '<span class="badge fail">fail</span>' not in page  # nothing failed


def test_unknown_run_is_404(traced_db):
    db_path, _ = traced_db
    page, code = render_run(ui_connect(db_path), "nope")
    assert code == 404 and "No run with id" in page


def test_fmt_duration():
    assert fmt_duration("2026-01-01T00:00:00+00:00", "2026-01-01T00:01:05+00:00") == "1m 05s"
    assert fmt_duration("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:43+00:00") == "43s"
    assert fmt_duration("2026-01-01T00:00:00+00:00", "2026-01-01T01:02:00+00:00") == "1h 02m"
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
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_trace_handler(db_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(f"{base}/") as resp:
            assert resp.status == 200 and run_id.encode() in resp.read()
        with urllib.request.urlopen(f"{base}/run/{run_id}") as resp:
            assert resp.status == 200 and b"timeline" in resp.read()
        for path in (f"{base}/run/unknown", f"{base}/nope"):
            try:
                urllib.request.urlopen(path)
                raise AssertionError(f"{path} should have 404'd")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_static_assets_served_with_types_and_whitelist(traced_db):
    db_path, _ = traced_db
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_trace_handler(db_path))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(f"{base}/static/style.css") as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/css")
            assert b"--bg" in resp.read()               # the dark palette variables
        with urllib.request.urlopen(f"{base}/static/app.js") as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/javascript")
        for path in ("/static/nope.js", "/static/../factory/board.py", "/static/"):
            try:
                urllib.request.urlopen(f"{base}{path}")
                raise AssertionError(f"{path} should have 404'd")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_fragments_serve_timeline_and_table(traced_db):
    db_path, run_id = traced_db
    conn = ui_connect(db_path)
    fragment, code = render_run_fragment(conn, run_id)
    assert code == 200 and "timeline" in fragment and "events" in fragment
    assert "<div data-live" not in fragment             # fragments are bare payloads
    assert render_runs_fragment(conn).startswith("<table>")
    fragment, code = render_run_fragment(conn, "nope")
    assert code == 404


def test_live_run_page_polls_a_fragment(traced_db):
    db_path, _ = traced_db
    conn = connect(db_path)
    live = Run.create(conn, request="watch me", agent="builder", model="m-1")
    page, code = render_run(ui_connect(db_path), live.id)
    assert code == 200
    assert "data-live" in page and f"/fragment/run/{live.id}" in page


def test_relative_timestamps_and_basic_columns(traced_db):
    """Times read 'x min ago' (JS keeps them fresh and localizes the tooltip);
    the index carries the basics only — no agent/model/phases/events columns."""
    db_path, run_id = traced_db
    page, _ = render_run(ui_connect(db_path), run_id)
    assert "data-ts=" in page                          # relative timestamps present
    assert "started <span class='rel'" in page         # the summary line reads human
    index = render_index(ui_connect(db_path))
    assert "data-ts=" in index and "<th>took</th>" in index
    assert "<th>agent</th>" not in index and "<th>model</th>" not in index
    assert "<th>phases</th>" not in index and "<th>events</th>" not in index


def test_phase_detail_curates_envelope_and_gates(traced_db):
    """Phase detail shows what the phase *meant* — the envelope's own summary and
    the gate verdicts — not a raw event dump."""
    import json

    db_path, run_id = traced_db
    conn = connect(db_path)
    run = Run.get(conn, run_id)
    with run.phase(name="build:r2", kind="agent", owner="builder") as phase:
        phase.log("gate_results", {"diff_matches_claims": {"gate": "diff", "pass": True}})
        conn.execute(
            "INSERT INTO envelopes (phase_id, attempt, valid, data, created_at)"
            " VALUES (?, 1, 1, ?, '2026-01-01T00:00:00+00:00')",
            (phase.id, json.dumps({
                "status": "success",
                "summary": "edited the README",
                "changed_files": ["README.md"],
            })),
        )
        conn.commit()
    page, code = render_run(ui_connect(db_path), run_id)
    assert code == 200
    assert "✓ edited the README" in page
    assert "changed 1 file(s)" in page
    assert "diff_matches_claims ✓" in page             # gates verdict, humanized
    assert "'attempt'" not in page.split("<h2>events</h2>")[0]  # no raw event dump up top


def test_ui_reads_while_a_writer_is_mid_transaction(traced_db):
    """WAL contract at the UI level: an uncommitted factory write never blocks the page."""
    db_path, run_id = traced_db
    writer = connect(db_path)  # begins implicit transactions on execute
    writer.execute("BEGIN")
    writer.execute(
        "INSERT INTO sessions (id, request, status, started_at) VALUES ('hot', 'in flight', 'running', 'now')"
    )
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_trace_handler(db_path))
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
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_trace_handler(tmp_path / "absent.db"))
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
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_trace_handler(db_path))
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
    assert f"[+{200 - 110} chars omitted]" in page   # the request column caps at 110
    full_page, _ = render_run(ui_connect(db_path), live.id)
    assert long_request in full_page                   # the run page is untruncated


def test_render_error_becomes_a_page_not_a_drop(traced_db, monkeypatch):
    import factory.web.runs as runs_module

    db_path, _ = traced_db
    monkeypatch.setattr(runs_module, "render_index", lambda conn: 1 / 0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_trace_handler(db_path))
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


def test_legacy_db_without_repo_column_still_renders(tmp_path):
    """Pre-decoupling traces have no sessions.repo; the read-only UI never migrates
    (query_only), so both pages must render legacy rows without a repo field."""
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, request TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'running', agent TEXT, model TEXT,"
        " started_at TEXT NOT NULL, ended_at TEXT);"
        "CREATE TABLE phases (id INTEGER PRIMARY KEY, run_id TEXT, seq INT, name TEXT,"
        " kind TEXT, owner TEXT, status TEXT, started_at TEXT, ended_at TEXT, error TEXT);"
        "CREATE TABLE events (id INTEGER PRIMARY KEY, run_id TEXT, created_at TEXT,"
        " type TEXT, payload TEXT);"
        "INSERT INTO sessions (id, request, status, agent, model, started_at, ended_at)"
        " VALUES ('old', 'legacy request', 'accepted', 'a', 'm',"
        " '2026-01-01T00:00:00+00:00', '2026-01-01T00:01:00+00:00');"
    )
    legacy.commit()
    legacy.close()

    conn = ui_connect(db_path)
    index = render_index(conn)              # raised IndexError before the fix
    assert "old" in index and "legacy request" in index
    page, code = render_run(conn, "old")    # ditto
    assert code == 200 and "legacy request" in page


def test_main_rejects_out_of_range_port(tmp_path):
    from factory.__main__ import main

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "defaults:\n  coding_agent: pi\n  model: m\n"
        "agents:\n  - name: a\n    purpose: p\n"
    )
    for bad_port in ("70000", "-1", "0"):
        assert main(["ui", "--port", bad_port, "--config", str(cfg)]) == 2
