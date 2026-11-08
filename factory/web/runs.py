"""Run pages: the runs index and one run's timeline.

Everything reads like a person wrote it: durations as "6m 35s", timestamps as
"4 min ago" (kept fresh by app.js, full local datetime in the tooltip), phase
names as plain-English labels. The runs index carries only the basics — what,
status, where, when, how long; the run page is a vertical timeline whose
per-phase detail shows the *meaningful* lines (envelope summary, gate
results, errors) and hides the raw event plumbing entirely. While the run is
live the timeline+events block sits in a ``[data-live]`` region the page's
JavaScript re-polls (``/fragment/run/{id}``), so watching a run never reloads.
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
    rel_span,
    short,
    started_epoch,
    static_asset,
    ui_connect,
)

INDEX_LIMIT = 100


# --- data access (read-only) --------------------------------------------------


def list_runs(conn: sqlite3.Connection, limit: int = INDEX_LIMIT):
    """Recent runs, newest first."""
    return conn.execute(
        "SELECT * FROM sessions s ORDER BY s.started_at DESC, s.id DESC LIMIT ?", (limit,)
    ).fetchall()


def run_detail(conn: sqlite3.Connection, run_id: str):
    """(session, phases, events, envelopes) for one run, or None if unknown."""
    run = conn.execute("SELECT * FROM sessions WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        return None
    phases = conn.execute(
        "SELECT * FROM phases WHERE run_id = ? ORDER BY seq", (run_id,)
    ).fetchall()
    events = conn.execute(
        "SELECT * FROM events WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()
    try:
        envelopes = conn.execute(
            "SELECT e.phase_id, e.attempt, e.valid, e.data FROM envelopes e"
            " JOIN phases p ON e.phase_id = p.id WHERE p.run_id = ? ORDER BY e.id",
            (run_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        envelopes = []  # legacy dbs have no envelopes table; the UI never migrates
    return run, phases, events, envelopes


def _col(row: sqlite3.Row, key: str, default=""):
    """Row value for ``key``, or ``default`` on legacy dbs lacking the column.

    The UI opens the db ``query_only`` and never migrates, so traces written
    before the ``sessions.repo`` column existed must still render.
    """
    return row[key] if key in row.keys() else default


# --- rendering ----------------------------------------------------------------


def _repo_name(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] if path else "-"


def _runs_table(rows) -> str:
    """The basics only: what, status, where, when, how long."""
    body = []
    for r in rows:
        repo = _col(r, "repo") or ""
        body.append(
            "<tr>"
            f"<td><a href='/run/{esc(r['id'])}'><code>{esc(r['id'])}</code></a></td>"
            f"<td>{badge(r['status'])}</td>"
            f"<td>{esc(short(r['request'], 110))}</td>"
            f"<td class='muted'>{esc(short(_repo_name(repo), 24))}</td>"
            f"<td class='muted'>{rel_span(started_epoch(r['started_at']))}</td>"
            f"<td>{esc(fmt_duration(r['started_at'], r['ended_at']))}</td>"
            "</tr>"
        )
    return (
        "<table><tr><th>run</th><th>status</th><th>request</th>"
        "<th>repo</th><th>started</th><th>took</th></tr>" + "".join(body) + "</table>"
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


def _envelope_line(env) -> str | None:
    """The one human sentence an envelope carries, plus notable counts."""
    if env is None:
        return None
    try:
        data = json.loads(env["data"])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    summary = str(data.get("summary") or "").strip()
    extras = []
    if data.get("plan_path"):
        extras.append(f"plan: {data['plan_path']}")
    if data.get("changed_files") is not None:
        extras.append(f"changed {len(data['changed_files'])} file(s)")
    if isinstance(data.get("questions"), list):
        extras.append(f"{len(data['questions'])} question(s)")
    if isinstance(data.get("findings"), list):
        extras.append(f"{len(data['findings'])} finding(s)")
    if isinstance(data.get("discovered"), list) and data["discovered"]:
        extras.append(f"{len(data['discovered'])} discovered item(s)")
    mark = "✓" if env["valid"] else "✗"
    line = f"{mark} {esc(summary or 'output parsed')}"
    if extras:
        line += f" <span class='muted'>· {' · '.join(esc(e) for e in extras)}</span>"
    return f"<p class='env'>{line}</p>"


def _gates_line(gate_events) -> str | None:
    """'gates: diff_matches_claims ✓ · tests_pass ✗' from the gate_results event.

    The payload is GateReport.to_payload(): ``{"gate": …, "passed": bool, …}``
    keyed by gate name.
    """
    for e in gate_events:
        try:
            results = json.loads(e["payload"])
        except ValueError:
            continue
        if not isinstance(results, dict) or not results:
            continue
        marks = []
        for name, r in sorted(results.items()):
            ok = bool(isinstance(r, dict) and r.get("passed"))
            cls = "gate-ok" if ok else "gate-bad"
            marks.append(f"<span class='{cls}'>{esc(name)} {'✓' if ok else '✗'}</span>")
        return "<p class='env'>gates: " + " · ".join(marks) + "</p>"
    return None


def _timeline_html(phases, events, envelopes) -> str:
    if not phases:
        return "<h2>timeline</h2><p class='muted'>no phases traced</p>"
    by_phase: dict[int, list] = {}
    for e in events:
        pid = _col(e, "phase_id", None)  # legacy dbs have no events.phase_id at all
        if pid is not None:
            by_phase.setdefault(pid, []).append(e)
    env_by_phase: dict[int, sqlite3.Row] = {}
    for env in envelopes:
        if env["valid"]:
            env_by_phase[env["phase_id"]] = env  # latest valid attempt wins
    steps = []
    for p in phases:
        state, badge_status = _tl_state(p)
        label = esc(phase_label(p["name"]))
        dur = fmt_duration(p["started_at"], p["ended_at"]) or "…"
        # Curated detail: what the phase *meant* (envelope summary, gates,
        # error) — never the raw event dump. Complex stays hidden.
        here = by_phase.get(p["id"], [])
        pi_here = sum(1 for e in here if e["type"] == "pi_event")
        detail_parts = [
            line
            for line in (
                _envelope_line(env_by_phase.get(p["id"])),
                _gates_line([e for e in here if e["type"] == "gate_results"]),
            )
            if line
        ]
        if pi_here:
            detail_parts.append(f"<p class='muted'>+ {pi_here} agent stream events (hidden)</p>")
        if p["error"]:
            detail_parts.append(f"<pre>{esc(p['error'])}</pre>")
        if not detail_parts:
            detail_parts.append("<p class='muted'>nothing to show for this phase</p>")
        detail = "".join(detail_parts)
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
    """Chain decisions as a short table; agent streaming as one collapsible line.

    The pi-stream collapsible really collapses: expanding it shows the
    per-kind breakdown instead of nothing.
    """
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
                f"<tr><td class='muted'>{rel_span(started_epoch(e['created_at']))}</td>"
                f"<td><code>{esc(e['type'])}</code></td>"
                f"<td>{pretty_payload(e['payload'])}</td></tr>"
            )
    if pi_counts:
        rows = "".join(
            f"<tr><td class='muted'>{esc(k)}</td><td>{v}</td></tr>"
            for k, v in sorted(pi_counts.items(), key=lambda kv: -kv[1])
        )
        pi_summary = (
            "<details id='pi-events'><summary class='muted'>"
            f"{sum(pi_counts.values())} agent stream events — expand for the breakdown"
            f" ({len(pi_counts)} kinds)</summary>"
            f"<table class='pi'><tr><th>stream event</th><th>count</th></tr>{rows}</table>"
            "</details>"
        )
    else:
        pi_summary = ""
    table = (
        "<table><tr><th>when</th><th>type</th><th>payload</th></tr>" + "".join(timeline) + "</table>"
        if timeline
        else "<p class='muted'>no orchestrator events</p>"
    )
    return f"<h2>events</h2>{pi_summary}{table}"


def _run_summary(run) -> str:
    took = fmt_duration(run["started_at"], run["ended_at"])
    return (
        "<div class='summary'>"
        f"<p><strong>request</strong></p><pre>{esc(run['request'])}</pre>"
        f"<p>{badge(run['status'])} &nbsp; ran in <strong>{esc(_repo_name(_col(run, 'repo')))}</strong>"
        f" · started {rel_span(started_epoch(run['started_at']))}"
        + (f" · took {esc(took)}" if took else "")
        + (f"<span class='muted'> · agent {esc(run['agent'] or '-')} · model {esc(run['model'] or '-')}"
           if run["agent"] or run["model"] else "")
        + "</span></p></div>"
    )


def _run_inner(phases, events, envelopes) -> str:
    """Timeline + events: everything below the summary, and the fragment body."""
    return _timeline_html(phases, events, envelopes) + _events_html(events)


def render_run(conn: sqlite3.Connection, run_id: str) -> tuple[str, int]:
    """The run page: (html, http status). Unknown ids get a 404 page."""
    detail = run_detail(conn, run_id)
    if detail is None:
        return page("unknown run", f"<p>No run with id <code>{esc(run_id)}</code>.</p>"), 404
    run, phases, events, envelopes = detail
    inner = _run_inner(phases, events, envelopes)
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
    run, phases, events, envelopes = detail
    return _run_inner(phases, events, envelopes), 200


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
