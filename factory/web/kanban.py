"""Kanban rendering: the board page, its cards, fragments, and pause pages.

Pure rendering — every function here consumes a ``Board.board_snapshot()``
dict or a pause row and returns HTML. Strip priority on a Working card, top
to bottom: an open pause (the owner must act), then the live phase (running
with an open phase row), then the settled run badge, then the bare rerun
escape hatch for claimed cards whose run never got created.
"""

import json

from .scaffold import ARCHIVE_SEARCH_HINT, badge, esc, fmt_elapsed, page, short, ui_connect


def _pause_headline(pause: dict) -> str:
    try:
        req = json.loads(pause["request"]) if isinstance(pause["request"], str) else pause["request"]
    except ValueError:
        req = {}
    if pause["type"] == "questions":
        n = len(req.get("questions") or [])
        return f"{n} question{'s' if n != 1 else ''}"
    if pause["type"] == "plan_approval":
        return f"plan: {req.get('plan_path') or 'unwritten'}"
    if pause["type"] == "cleanup_approval":
        return "worktree has this run's edits"
    return "approve the PR push"


def _task_card(t: dict) -> str:
    """One kanban card. Every agent-produced string passes through esc."""
    tid = esc(t["id"])
    parts = [f"<div class='card' id='task-{tid}'>"]
    title_html = f"<span class='title'>{esc(t['title'])}</span>"
    if t["origin"] == "system":
        title_html += " <span class='badge system'>system</span>"
    parts.append(f"<div>{title_html}</div>")
    meta = [esc(t["repo_name"])]
    if t["position"]:
        meta.append(f"queue #{t['position']}")
    if t["branch"]:
        meta.append(f"branch <code>{esc(t['branch'])}</code>")
    if t["origin_run"]:
        meta.append(f"discovered by <a href='/run/{esc(t['origin_run'])}'>{esc(t['origin_run'])}</a>")
    parts.append(f"<div class='muted meta'>{' · '.join(meta)}</div>")
    if t["note"]:
        parts.append(f"<div class='muted'>{esc(short(t['note'], 160))}</div>")
    pause = t["pause"]
    if pause is not None:
        parts.append(
            f"<div class='strip wait'><span class='badge waiting'>waiting on you</span> "
            f"{esc(_pause_headline(pause))} "
            f"<a href='/pause/{int(pause['id'])}'>open →</a></div>"
        )
    phase = t["phase"]
    if pause is None and t["run_status"] == "running":
        if phase is not None:
            parts.append(
                f"<div class='strip live'><span class='pulse'></span> {esc(phase['label'])} · "
                f"<span class='elapsed' data-started-epoch='{phase['started_epoch']}'>"
                f"{esc(fmt_elapsed(phase['elapsed_s']))}</span> "
                f"<a href='/run/{esc(t['latest_run'])}'>watch run</a></div>"
            )
        else:
            parts.append(
                f"<div class='strip'>{badge('running')} "
                f"<a href='/run/{esc(t['latest_run'])}'>watch run</a></div>"
            )
    elif t["run_status"]:
        parts.append(
            f"<div class='strip'>{badge(t['run_status'])} "
            f"<a href='/run/{esc(t['latest_run'])}'>run</a> "
            f"<form method='post' action='/tasks/{tid}/rerun'>"
            f"<button type='submit'>rerun</button></form></div>"
        )
    elif t["rerun"]:
        parts.append(
            f"<div class='strip'><form method='post' action='/tasks/{tid}/rerun'>"
            f"<button type='submit'>rerun</button></form></div>"
        )
    if t["pr_url"]:
        run_link = (
            f" · <a href='/run/{esc(t['latest_run'])}'>run</a>" if t["latest_run"] else ""
        )
        parts.append(
            f"<div class='strip'><a href='{esc(t['pr_url'])}'>PR</a>{run_link} · "
            f"auto-archives on merge — checks every 60s "
            f"<form method='post' action='/tasks/{tid}/archive'>"
            f"<button type='submit'>archive</button></form></div>"
        )
    if t["status"] == "backlog" and not t["runnable"]:
        parts.append(
            f"<form method='post' action='/tasks/{tid}/promote'>"
            f"<button type='submit'>review &amp; run</button></form>"
        )
    if t["status"] == "backlog" and t["runnable"] and pause is None:
        parts.append(
            f"<form method='post' action='/tasks/{tid}/archive'>"
            f"<button type='submit'>discard</button></form>"
        )
    parts.append("</div>")
    return "".join(parts)


def _column(name: str, tasks: list[dict]) -> str:
    cards = "".join(_task_card(t) for t in tasks) or "<p class='muted'>empty</p>"
    return f"<div class='col'><h2>{name} <span class='muted'>({len(tasks)})</span></h2>{cards}</div>"


def board_fragment(snap: dict) -> str:
    """The three columns alone — what /fragment/board serves the poller."""
    return (
        "<div class='board'>"
        + _column("Backlog", snap["backlog"])
        + _column("Working", snap["working"])
        + _column("In PR", snap["in_pr"])
        + "</div>"
    )


def _archived_section(snap: dict) -> str:
    rows = []
    for t in snap["archived"]:
        search = esc(f"{t['title']} {t['repo_name']} {t['note']}".lower())
        run_link = f"<a href='/run/{esc(t['latest_run'])}'>run</a>" if t["latest_run"] else "—"
        pr_link = f"<a href='{esc(t['pr_url'])}'>PR</a>" if t["pr_url"] else "—"
        rows.append(
            f"<tr data-search='{search}'>"
            f"<td>{esc(t['title'])}</td>"
            f"<td class='muted'>{esc(t['repo_name'])}</td>"
            f"<td class='muted'>{esc(short(t['note'], 120))}</td>"
            f"<td class='muted'>{esc(t['updated_at'])}</td>"
            f"<td>{run_link}</td><td>{pr_link}</td></tr>"
        )
    if rows:
        table = (
            "<table><tr><th>task</th><th>repo</th><th>result</th>"
            "<th>archived (UTC)</th><th>run</th><th>PR</th></tr>"
            # the tbody id is the JS filter's handle (app.js reads #archive-rows)
            f"<tbody id='archive-rows'>" + "".join(rows) + "</tbody></table>"
        )
    else:
        table = "<p class='muted'>nothing archived yet</p>"
    total = snap["archived_total"]
    shown = len(snap["archived"])
    extra = f" <span class='muted'>(showing {shown} of {total})</span>" if total > shown else ""
    return (
        f"<section class='archive'><h2>Archived <span class='muted'>({total})</span>{extra}</h2>"
        f"<div class='tools'>"
        f"<input type='search' data-archive-search placeholder='{ARCHIVE_SEARCH_HINT}'></div>"
        f"{table}</section>"
    )


def render_board(board) -> str:
    """The board page: new-task form, live kanban region, archived table.

    The new-task form and the archived table sit OUTSIDE the polled region —
    an open form or a half-typed filter must survive a fragment swap. When
    nothing is live the region wrapper is omitted entirely (nothing polls).
    """
    snap = board.board_snapshot()
    fragment = board_fragment(snap)
    body = _new_task_form(board)
    if snap["live"]:
        body += f"<div data-live data-fragment-src='/fragment/board'>{fragment}</div>"
    else:
        body += fragment
    body += _archived_section(snap)
    return page("factory board", body, live=snap["live"])


def _new_task_form(board) -> str:
    if not board.repos:
        return (
            "<div class='newtask'><strong>New task</strong><p class='muted'>No repos configured — "
            "add a <code>repos:</code> section to <code>config.yaml</code> (name + path, "
            "optional per-repo <code>test_command</code>).</p></div>"
        )
    options = "".join(
        f"<option value='{esc(r['name'])}'>{esc(r['name'])}</option>" for r in board.repos
    )
    return (
        "<details class='newtask'><summary><strong>New task</strong></summary>"
        "<form method='post' action='/tasks'>"
        "<label>repo<br><select name='repo'>" + options + "</select></label><br>"
        "<label>title<br><input type='text' name='title' required></label><br>"
        "<label>what should be done<br><textarea name='prompt' rows='3'></textarea></label><br>"
        "<button type='submit'>add to backlog</button>"
        "</form></details>"
    )


def render_pause(board, pause_id: str) -> tuple[str, int]:
    """The owner's decision page for one parked card."""
    try:
        pid = int(pause_id)
    except ValueError:
        return page("unknown pause", "<p>Bad pause id.</p>"), 404
    conn = ui_connect(board.db_path)
    try:
        row = conn.execute("SELECT * FROM pauses WHERE id = ?", (pid,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return page("unknown pause", f"<p>No pause #{pid}.</p>"), 404

    try:
        req = json.loads(row["request"])
    except ValueError:
        req = {}
    run_link = (
        f" · run <a href='/run/{esc(row['run_id'])}'><code>{esc(row['run_id'])}</code></a>"
        if row["run_id"] else ""
    )
    head = f"<p>task <code>{esc(row['task_id'])}</code>{run_link} · opened {esc(row['created_at'])} UTC</p>"

    if row["status"] != "waiting":
        body = (
            head
            + f"<p>{badge(row['status'])}</p>"
            + (f"<pre>{esc(row['answer'])}</pre>" if row["answer"] else "")
        )
        return page(f"pause #{pid}", body), 200

    if row["type"] == "questions":
        questions = req.get("questions") or []
        qlist = "".join(f"<li>{esc(q)}</li>" for q in questions)
        body = (
            head
            + "<div class='summary'><p><strong>the planner asks:</strong></p>"
            + f"<ul>{qlist}</ul></div>"
            + f"<form method='post' action='/pause/{pid}'>"
            + "<label>your answers<br><textarea name='answer' rows='5'></textarea></label><br>"
            + "<button type='submit' name='action' value='send'>send answers</button> "
            + "<button type='submit' name='action' value='reject'>cancel the task</button>"
            + "</form>"
        )
    elif row["type"] == "plan_approval":
        plan_text = str(req.get("plan") or "")
        shown = (
            plan_text if len(plan_text) <= 6000
            else plan_text[:6000] + f"\n[+{len(plan_text) - 6000} chars omitted]"
        )
        body = (
            head
            + f"<div class='summary'><p><strong>plan</strong> "
            + f"<code>{esc(req.get('plan_path') or '-')}</code></p>"
            + f"<pre>{esc(shown)}</pre></div>"
            + f"<form method='post' action='/pause/{pid}'>"
            + "<button type='submit' name='action' value='approve'>approve — start building</button>"
            + "<br><br>"
            + "<label>or ask for changes<br><textarea name='answer' rows='4'></textarea></label><br>"
            + "<button type='submit' name='action' value='send'>send corrections</button> "
            + "<button type='submit' name='action' value='reject'>reject the plan</button>"
            + "</form>"
        )
    elif row["type"] == "cleanup_approval":
        body = (
            head
            + "<div class='summary'><p><strong>the failed run left these changes"
            + " uncommitted</strong></p>"
            + f"<pre>{esc(req.get('pending') or '-')}</pre></div>"
            + "<p class='muted'>Approving restores the tree (reset + clean, trace excluded);"
            + " rejecting keeps every edit for you to dispose of by hand.</p>"
            + f"<form method='post' action='/pause/{pid}'>"
            + ("<button type='submit' name='action' value='approve'>"
               "restore — discard the attempt's edits</button> ")
            + "<button type='submit' name='action' value='reject'>keep them</button>"
            + "</form>"
        )
    else:  # pr_approval
        body = (
            head
            + "<div class='summary'><p><strong>what would land</strong></p>"
            + f"<pre>{esc(req.get('pending') or '-')}</pre></div>"
            + "<p class='muted'>Approving commits the change on the task branch, pushes it,"
            + " and opens the PR.</p>"
            + f"<form method='post' action='/pause/{pid}'>"
            + ("<button type='submit' name='action' value='approve'>"
               "approve — push &amp; open PR</button> ")
            + "<button type='submit' name='action' value='reject'>reject</button>"
            + "</form>"
        )
    return page(f"pause #{pid}", body), 200
