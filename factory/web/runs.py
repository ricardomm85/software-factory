"""Run pages: the runs index and one run's timeline (absorbs the old ui.py).

The run page is a vertical timeline — one step per phase, plain-English
labels, the current step pulsing and open — with the orchestrator's events
and pi stream counts collapsed below it. While the run is live the whole
timeline+events block sits in a ``[data-live]`` region the page's JavaScript
re-polls (``/fragment/run/{id}``), so watching a run never reloads the page.
"""

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .scaffold import (
    badge,
    esc,
    fmt_duration,
    page,
    phase_label,
    pretty_payload,
    short,
    static_asset,
    ui_connect,
)

INDEX_LIMIT = 100


# --- data access (read-only) --------------------------------------------------


def list_runs(conn: sqlite3.Connection, limit: int = INDEX_LIMIT):
    """Recent runs, newest first, with phase and event counts."""
    return conn.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM phases p WHERE p.run_id = s.id) AS n_phases,"
        " (SELECT COUNT(*) FROM events e WHERE e.run_id = s.id) AS n_events"
        " FROM sessions s ORDER BY s.started_at DESC, s.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def run_detail(conn: sqlite3.Connection, run_id: str):
    """(session, phases, events) for one run, or None if the id is unknown."""
    run = conn.execute("SELECT * FROM sessions WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        return None
    phases = conn.execute(
        "SELECT * FROM phases WHERE run_id = ? ORDER BY seq", (run_id,)
    ).fetchall()
    events = conn.execute(
        "SELECT * FROM events WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()
    return run, phases, events


def _col(row: sqlite3.Row, key: str, default=""):
    """Row value for ``key``, or ``default`` on legacy dbs lacking the column.

    The UI opens the db ``query_only`` and never migrates, so traces written
    before the ``sessions.repo`` column existed must still render.
    """
    return row[key] if key in row.keys() else default


# --- rendering ----------------------------------------------------------------


def _runs_table(rows) -> str:
    body = []
    for r in rows:
        body.append(
            "<tr>"
            f"<td><a href='/run/{esc(r['id'])}'><code>{esc(r['id'])}</code></a></td>"
            f"<td>{badge(r['status'])}</td>"
            f"<td>{esc(r['agent'] or '-')}</td><td class='muted'>{esc(r['model'] or '-')}</td>"
            f"<td>{esc(short(r['request']))}</td>"
            f"<td class='muted'>{esc(short(_col(r, 'repo') or '-', 40))}</td>"
            f"<td>{r['n_phases']}</td><td>{r['n_events']}</td>"
            f"<td class='muted'>{esc(r['started_at'])}</td>"
            f"<td>{esc(fmt_duration(r['started_at'], r['ended_at']))}</td>"
            "</tr>"
        )
    return (
        "<table><tr><th>run</th><th>status</th><th>agent</th><th>model</th><th>request</th>"
        "<th>repo</th><th>phases</th><th>events</th><th>started (UTC)</th><th>took</th></tr>"
        + "".join(body)
        + "</table>"
    )


def render_runs_fragment(conn: sqlite3.Connection) -> str:
    """Just the runs table — the payload /fragment/runs serves to the poller."""
    return _runs_table(list_runs(conn))


def render_index(conn: sqlite3.Connection) -> str:
    rows = list_runs(conn)
    table = (
        _runs_table(rows)
        if rows
        else "<p>No runs traced yet. Try <code>factory run \"say hi\"</code>.</p>"
    )
    live = any(r["status"] == "running" for r in rows)
    body = f"<div data-live data-fragment-src='/fragment/runs'>{table}</div>" if live else table
    return page("factory runs", body, live=live)


def _tl_state(p) -> tuple[str, str]:
    """(css state, badge status) for one phase row.

    Phases are born fail and only flip on a clean exit: an open phase is
    running, not failing — showing fail for it reads as an error.
    """
    if p["ended_at"] is None:
        return "running", "running"
    if p["status"] in ("pass", "accepted"):
        return "done", p["status"]
    return "fail", p["status"]


def _timeline_html(phases, events) -> str:
    if not phases:
        return "<h2>timeline</h2><p class='muted'>no phases traced</p>"
    by_phase: dict[int, list] = {}
    for e in events:
        pid = _col(e, "phase_id", None)  # legacy dbs have no events.phase_id at all
        if pid is not None:
            by_phase.setdefault(pid, []).append(e)
    steps = []
    for p in phases:
        state, badge_status = _tl_state(p)
        label = esc(phase_label(p["name"]))
        dur = fmt_duration(p["started_at"], p["ended_at"]) or "…"
        detail_parts = []
        pi_here = 0
        for e in by_phase.get(p["id"], []):
            if e["type"] == "pi_event":
                pi_here += 1
                continue
            detail_parts.append(
                f"<div class='muted'>{esc(e['created_at'])} <code>{esc(e['type'])}</code></div>"
                f"{pretty_payload(e['payload'])}"
            )
        if pi_here:
            detail_parts.append(f"<p class='muted'>+ {pi_here} pi stream events (see events below)</p>")
        if p["error"]:
            detail_parts.append(f"<pre>{esc(p['error'])}</pre>")
        detail = "".join(detail_parts) or "<p class='muted'>nothing recorded in this phase</p>"
        open_attr = " open" if state == "running" else ""
        steps.append(
            f"<li class='tl {state}' id='tl-{p['id']}'>"
            f"<div class='tl-rail'><span class='tl-dot'></span></div>"
            f"<div class='tl-main'>"
            f"<div class='tl-head'><span class='tl-label'>{label}</span>"
            f"<span class='muted'><code>{esc(p['name'])}</code> · #{p['seq']} · "
            f"{esc(p['kind'])}/{esc(p['owner'])} · {esc(dur)}</span> {badge(badge_status)}</div>"
            f"<details class='tl-detail' id='tld-{p['id']}'{open_attr}>"
            f"<summary class='muted'>detail</summary>{detail}</details>"
            f"</div></li>"
        )
    return f"<h2>timeline</h2><ol class='timeline'>{''.join(steps)}</ol>"


def _events_html(events) -> str:
    """Orchestrator decisions as a table, agent streaming as counts."""
    pi_counts: dict[str, int] = {}
    timeline = []
    for e in events:
        if e["type"] == "pi_event":
            try:
                kind = str(json.loads(e["payload"]).get("type", "?"))
            except ValueError:
                kind = "?"
            pi_counts[kind] = pi_counts.get(kind, 0) + 1
        else:
            timeline.append(
                f"<tr><td class='muted'>{esc(e['created_at'])}</td>"
                f"<td><code>{esc(e['type'])}</code></td>"
                f"<td>{pretty_payload(e['payload'])}</td></tr>"
            )
    pi_summary = (
        "<details id='pi-events'><summary class='muted'>"
        f"{sum(pi_counts.values())} pi stream events "
        f"({', '.join(f'{esc(k)} × {v}' for k, v in sorted(pi_counts.items()))})"
        "</summary></details>"
        if pi_counts
        else ""
    )
    table = (
        "<table><tr><th>at (UTC)</th><th>type</th><th>payload</th></tr>" + "".join(timeline) + "</table>"
        if timeline
        else "<p class='muted'>no orchestrator events</p>"
    )
    return f"<h2>events</h2>{pi_summary}{table}"


def _run_summary(run) -> str:
    return (
        "<div class='summary'>"
        f"<p><strong>request</strong></p><pre>{esc(run['request'])}</pre>"
        f"<p>{badge(run['status'])} &nbsp; agent <strong>{esc(run['agent'] or '-')}</strong>"
        f" · model <span class='muted'>{esc(run['model'] or '-')}</span>"
        + (
            f" · repo <code>{esc(short(_col(run, 'repo'), 60))}</code>" if _col(run, "repo") else ""
        )
        + f" · started {esc(run['started_at'])} UTC"
        + (f" · took {fmt_duration(run['started_at'], run['ended_at'])}" if run["ended_at"] else "")
        + "</p></div>"
    )


def _run_inner(phases, events) -> str:
    """Timeline + events: everything below the summary, and the fragment body."""
    return _timeline_html(phases, events) + _events_html(events)


def render_run(conn: sqlite3.Connection, run_id: str) -> tuple[str, int]:
    """The run page: (html, http status). Unknown ids get a 404 page."""
    detail = run_detail(conn, run_id)
    if detail is None:
        return page("unknown run", f"<p>No run with id <code>{esc(run_id)}</code>.</p>"), 404
    run, phases, events = detail
    inner = _run_inner(phases, events)
    live = run["status"] == "running"
    body = _run_summary(run) + (
        f"<div data-live data-fragment-src='/fragment/run/{esc(run_id)}'>{inner}</div>"
        if live
        else inner
    )
    return page(f"run {run['id']}", body, live=live), 200


def render_run_fragment(conn: sqlite3.Connection, run_id: str) -> tuple[str, int]:
    """Timeline + events alone — the payload /fragment/run/{id} serves."""
    detail = run_detail(conn, run_id)
    if detail is None:
        return f"<p>No run with id <code>{esc(run_id)}</code>.</p>", 404
    run, phases, events = detail
    return _run_inner(phases, events), 200


# --- http server (the standalone `factory ui` trace viewer) --------------------


def _not_found(path: str) -> str:
    return page("not found", f"<p>Unknown page <code>{esc(path)}</code>.</p>")


def make_trace_handler(db_path: Path) -> type[BaseHTTPRequestHandler]:
    """Build a handler class bound to one trace db path."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if path.startswith("/static/"):
                name = path.removeprefix("/static/").strip("/")
                asset = static_asset(name)
                if asset is None:
                    self._send(_not_found(path), 404)
                else:
                    self._send_bytes(*asset)
                return
            if not db_path.exists():
                body = page(
                    "no trace yet",
                    f"<p>No trace db at <code>{esc(db_path)}</code> yet — "
                    "run something first, e.g. <code>factory run \"say hi\"</code>.</p>",
                )
                self._send(body, 200)
                return
            try:
                conn = ui_connect(db_path)
            except sqlite3.Error as exc:
                self._send(page("trace unavailable", f"<p>{esc(exc)}</p>"), 200)
                return
            try:
                if path == "/":
                    self._send(render_index(conn), 200)
                elif path.startswith("/run/"):
                    run_id = path.removeprefix("/run/").strip("/")
                    body, code = render_run(conn, run_id) if run_id else (_not_found(path), 404)
                    self._send(body, code)
                elif path.startswith("/fragment/"):
                    self._fragment(conn, path)
                else:
                    self._send(_not_found(path), 404)
            except sqlite3.Error as exc:
                # e.g. a db file that exists but has no schema yet
                self._send(page("trace unavailable", f"<p>{esc(exc)}</p>"), 200)
            except Exception as exc:
                # A rendering bug gets its own page, not a dropped connection.
                self._send(page("render error", f"<p>{esc(type(exc).__name__)}: {esc(exc)}</p>"), 500)
            finally:
                conn.close()

        def _fragment(self, conn, path: str) -> None:
            rest = path.removeprefix("/fragment/").strip("/")
            if rest == "runs":
                self._send(render_runs_fragment(conn), 200)
            elif rest.startswith("run/"):
                body, code = render_run_fragment(conn, rest.removeprefix("run/").strip("/"))
                self._send(body, code)
            else:
                self._send(_not_found(path), 404)

        def _send(self, body: str, code: int) -> None:
            self._send_bytes(body.encode("utf-8"), "text/html; charset=utf-8", code)

        def _send_bytes(self, data: bytes, content_type: str, code: int = 200) -> None:
            try:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                if content_type != "text/html; charset=utf-8":
                    self.send_header("Cache-Control", "max-age=60")
                self.end_headers()
                self.wfile.write(data)
            except OSError:
                pass  # the client hung up mid-refresh; a socket has no one to tell

        def log_message(self, *args) -> None:  # keep the terminal quiet
            pass

    return Handler


def make_trace_server(db_path: Path, port: int) -> ThreadingHTTPServer:
    """Bind loopback only: the trace is local data, not a web service."""
    return ThreadingHTTPServer(("127.0.0.1", port), make_trace_handler(db_path))


def serve(db_path: Path, port: int) -> None:
    """Bind, then serve until interrupted; always release the socket."""
    server = make_trace_server(db_path, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
