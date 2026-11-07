"""Read-only observability UI over the SQLite trace (Phase 5).

A stdlib-only local web page: no frameworks, no external assets, loopback only.
Every request opens the database on a ``query_only`` connection — the UI can
watch a live run while the factory writes (WAL readers never block) and SQLite
itself refuses any write the UI could attempt. Routes:

  GET /          recent runs: status, duration, phase and event counts
  GET /run/{id}  one run: summary, per-phase waterfall, notable events, pi counts

The run page refreshes every ``REFRESH_S`` seconds while the run is still
``running`` — watching a run live should not require reading transcripts.
"""

import html
import json
import sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

REFRESH_S = 5
INDEX_LIMIT = 100
TEXT_CAP = 240  # payload display cap; full text stays one click away

_CSS = """
body{font-family:system-ui,sans-serif;margin:1.5rem auto;max-width:64rem;padding:0 1rem;color:#1a1a1a;background:#fafafa}
h1{font-size:1.25rem;margin:.2rem 0} h2{font-size:1rem;margin:1.2rem 0 .4rem}
nav{margin:.4rem 0 1rem}
table{border-collapse:collapse;width:100%;font-size:.85rem;margin:.4rem 0}
th,td{border-bottom:1px solid #ddd;padding:.35rem .5rem;text-align:left;vertical-align:top}
th{color:#555;text-transform:uppercase;font-size:.68rem;letter-spacing:.05em}
.badge{display:inline-block;padding:.1rem .45rem;border-radius:.6rem;font-size:.72rem;color:#fff;white-space:nowrap}
.badge.pass,.badge.accepted{background:#2e7d32}
.badge.fail,.badge.failed{background:#c62828}
.badge.running{background:#ef6c00}
pre{white-space:pre-wrap;word-break:break-word;margin:.15rem 0;font-size:.78rem}
.bar{height:.5rem;background:#4a6fa5;border-radius:2px;min-width:2px}
.summary{background:#fff;border:1px solid #ddd;border-radius:6px;padding:.8rem;margin:.6rem 0;font-size:.9rem}
.muted{color:#777;font-size:.78rem}
code{font-size:.85em}
"""


# --- data access (read-only) ------------------------------------------------


def ui_connect(db_path: Path) -> sqlite3.Connection:
    """Open an existing trace db for reading only.

    ``query_only`` makes SQLite reject any write statement on this connection,
    which is stronger than a coding convention: the UI cannot corrupt the trace
    even if a rendering bug tried to. The file must already exist — the UI never
    creates a database.
    """
    conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


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


def fmt_duration(started_at, ended_at) -> str:
    """Human duration between two ISO timestamps; '' when either is missing."""
    try:
        t0, t1 = datetime.fromisoformat(started_at), datetime.fromisoformat(ended_at)
        return f"{(t1 - t0).total_seconds():.0f}s"
    except (TypeError, ValueError):
        return ""


# --- rendering ---------------------------------------------------------------


def _esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


def _badge(status: str) -> str:
    return f'<span class="badge {_esc(status)}">{_esc(status)}</span>'


def _short(text: str, cap: int = 96) -> str:
    return text if len(text) <= cap else text[:cap] + " …"


def _pretty_payload(payload: str) -> str:
    """Pretty-print a JSON payload, escaping everything; long ones collapse."""
    try:
        text = json.dumps(json.loads(payload), ensure_ascii=False, indent=2, sort_keys=True)
    except ValueError:
        text = payload
    if len(text) <= TEXT_CAP:
        return f"<pre>{_esc(text)}</pre>"
    return (
        f"<pre>{_esc(_short(text, TEXT_CAP))}</pre>"
        f"<details><summary class='muted'>show full</summary>"
        f"<pre>{_esc(text)}</pre></details>"
    )


def _page(title: str, body: str, refresh: bool = False) -> str:
    meta = f'<meta http-equiv="refresh" content="{REFRESH_S}">' if refresh else ""
    return (
        f"<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">{meta}\n"
        f"<title>{_esc(title)}</title><style>{_CSS}</style></head><body>\n"
        f"<h1>factory trace</h1>\n<nav><a href='/'>← runs</a></nav>\n{body}\n"
        f"<p class='muted'>read-only · SQLite WAL · refreshes every {REFRESH_S}s while a run is live</p>\n"
        f"</body></html>\n"
    )


def _refresh_for(*statuses) -> bool:
    return any(s == "running" for s in statuses)


def render_index(conn: sqlite3.Connection) -> str:
    rows = list_runs(conn)
    body_rows = []
    for r in rows:
        body_rows.append(
            "<tr>"
            f"<td><a href='/run/{_esc(r['id'])}'><code>{_esc(r['id'])}</code></a></td>"
            f"<td>{_badge(r['status'])}</td>"
            f"<td>{_esc(r['agent'] or '-')}</td><td class='muted'>{_esc(r['model'] or '-')}</td>"
            f"<td>{_esc(_short(r['request']))}</td>"
            f"<td>{r['n_phases']}</td><td>{r['n_events']}</td>"
            f"<td class='muted'>{_esc(r['started_at'])}</td>"
            f"<td>{_esc(fmt_duration(r['started_at'], r['ended_at']))}</td>"
            "</tr>"
        )
    table = (
        "<table><tr><th>run</th><th>status</th><th>agent</th><th>model</th><th>request</th>"
        "<th>phases</th><th>events</th><th>started (UTC)</th><th>took</th></tr>"
        + "".join(body_rows)
        + "</table>"
        if rows
        else "<p>No runs traced yet. Try <code>factory run \"say hi\"</code>.</p>"
    )
    return _page("factory runs", table, refresh=_refresh_for(*(r["status"] for r in rows)))


def render_run(conn: sqlite3.Connection, run_id: str) -> tuple[str, int]:
    """The run page: (html, http status). Unknown ids get a 404 page."""
    detail = run_detail(conn, run_id)
    if detail is None:
        return _page("unknown run", f"<p>No run with id <code>{_esc(run_id)}</code>.</p>"), 404

    run, phases, events = detail
    summary = (
        "<div class='summary'>"
        f"<p><strong>request</strong></p><pre>{_esc(run['request'])}</pre>"
        f"<p>{_badge(run['status'])} &nbsp; agent <strong>{_esc(run['agent'] or '-')}</strong>"
        f" · model <span class='muted'>{_esc(run['model'] or '-')}</span>"
        f" · started {_esc(run['started_at'])} UTC"
        + (f" · took {fmt_duration(run['started_at'], run['ended_at'])}" if run["ended_at"] else "")
        + "</p></div>"
    )

    # Waterfall: one row per phase, bar width proportional to its share of the run.
    durations = [fmt_duration(p["started_at"], p["ended_at"]) for p in phases]
    totals = [float(d[:-1]) if d else 0.0 for d in durations]
    total = sum(totals) or 1.0
    rows = []
    for p, dur, secs in zip(phases, durations, totals):
        pct = round(100.0 * secs / total, 1)
        rows.append(
            "<tr>"
            f"<td>#{p['seq']}</td><td><code>{_esc(p['name'])}</code></td>"
            f"<td class='muted'>{_esc(p['kind'])}/{_esc(p['owner'])}</td>"
            f"<td>{_badge(p['status'])}</td><td>{_esc(dur)}</td>"
            f"<td style='width:30%'><div class='bar' style='width:{pct}%'></div></td>"
            f"<td>{_esc(_short(p['error'], TEXT_CAP)) if p['error'] else ''}</td>"
            "</tr>"
        )
    waterfall = (
        "<h2>phase waterfall</h2><table>"
        "<tr><th>#</th><th>phase</th><th>kind/owner</th><th>status</th><th>took</th><th></th><th>error</th></tr>"
        + "".join(rows)
        + "</table>"
        if phases
        else "<h2>phase waterfall</h2><p class='muted'>no phases traced</p>"
    )

    # Events: orchestrator decisions as a timeline, agent streaming as counts.
    pi_counts: dict[str, int] = {}
    timeline = []
    for e in events:
        if e["type"] == "pi_event":
            try:
                kind = json.loads(e["payload"]).get("type", "?")
            except ValueError:
                kind = "?"
            pi_counts[f"{kind}"] = pi_counts.get(kind, 0) + 1
        else:
            timeline.append(
                f"<tr><td class='muted'>{_esc(e['created_at'])}</td>"
                f"<td><code>{_esc(e['type'])}</code></td>"
                f"<td>{_pretty_payload(e['payload'])}</td></tr>"
            )
    pi_summary = (
        "<details><summary class='muted'>"
        f"{sum(pi_counts.values())} pi stream events ({', '.join(f'{k} × {v}' for k, v in sorted(pi_counts.items()))})"
        "</summary></details>"
        if pi_counts
        else ""
    )
    events_html = (
        "<h2>events</h2>"
        + pi_summary
        + (
            "<table><tr><th>at (UTC)</th><th>type</th><th>payload</th></tr>" + "".join(timeline) + "</table>"
            if timeline
            else "<p class='muted'>no orchestrator events</p>"
        )
    )

    page = _page(
        f"run {run['id']}",
        summary + waterfall + events_html,
        refresh=_refresh_for(run["status"]),
    )
    return page, 200


# --- http server -------------------------------------------------------------


def _not_found(path: str) -> str:
    return _page("not found", f"<p>Unknown page <code>{_esc(path)}</code>.</p>")


def make_handler(db_path: Path) -> type[BaseHTTPRequestHandler]:
    """Build a handler class bound to one trace db path."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if not db_path.exists():
                body = _page(
                    "no trace yet",
                    f"<p>No trace db at <code>{_esc(db_path)}</code> yet — "
                    "run something first, e.g. <code>factory run \"say hi\"</code>.</p>",
                )
                self._send(body, 200)
                return
            try:
                conn = ui_connect(db_path)
            except sqlite3.Error as exc:
                self._send(_page("trace unavailable", f"<p>{_esc(exc)}</p>"), 200)
                return
            try:
                if path == "/":
                    self._send(render_index(conn), 200)
                elif path.startswith("/run/"):
                    run_id = path.removeprefix("/run/").strip("/")
                    body, code = render_run(conn, run_id) if run_id else (_not_found(path), 404)
                    self._send(body, code)
                else:
                    self._send(_not_found(path), 404)
            except sqlite3.Error as exc:
                # e.g. a db file that exists but has no schema yet
                self._send(_page("trace unavailable", f"<p>{_esc(exc)}</p>"), 200)
            finally:
                conn.close()

        def _send(self, body: str, code: int) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:  # keep the terminal quiet
            pass

    return Handler


def make_server(db_path: Path, port: int) -> ThreadingHTTPServer:
    """Bind loopback only: the trace is local data, not a web service."""
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(db_path))


def serve(db_path: Path, port: int) -> None:
    make_server(db_path, port).serve_forever()
