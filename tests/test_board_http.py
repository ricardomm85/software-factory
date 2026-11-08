"""Tests for the board's HTTP surface: kanban render, create/promote/archive/
rerun, pause pages and resolution, run detail. Real server on an ephemeral port."""

import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from factory.board import Board, make_board_server
from factory.config import load_config
from factory.db import connect
from factory.run import Run

ROSTER = """
defaults:
  coding_agent: pi
  model: pi/m
  test_command: echo tests-ok
repos:
  - name: alpha
    path: {alpha}
agents:
  - name: a
    purpose: p
  - name: planner
    purpose: plans
  - name: builder
    purpose: builds
"""


@pytest.fixture()
def server(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(ROSTER.format(alpha=tmp_path / "alpha"))
    cfg = load_config(cfg_path)
    Path(cfg["repos"][0]["path"]).mkdir(parents=True, exist_ok=True)
    db = tmp_path / "data" / "factory.db"
    conn = connect(db)
    board = Board(conn, cfg, config_dir=tmp_path, db_path=db, chain_fn=lambda *a, **k: (True, ""))
    srv = make_board_server(board, 0)  # port 0: the OS picks a free one
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    board.start()
    yield board, f"http://127.0.0.1:{port}"
    board.stop()
    srv.shutdown()
    srv.server_close()
    conn.close()


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as res:
            return res.status, res.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # tests want the 303 itself, not the page it points to


def post(url: str, fields: dict, headers: dict | None = None) -> tuple[int, str]:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers=headers or {})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=5) as res:
            return res.status, res.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_board_renders_empty_columns(server):
    board, base = server
    code, html = get(f"{base}/")
    assert code == 200
    for name in ("Backlog", "Working", "In PR", "Archived"):
        assert name in html
    assert "New task" in html
    assert "no repos configured" not in html


def test_create_task_via_form_shows_in_backlog(server):
    board, base = server
    board.stop()
    code, body = post(f"{base}/tasks", {"repo": "alpha", "title": "add tests", "prompt": "write them"})
    assert code == 303, body
    code, html = get(f"{base}/")
    assert "add tests" in html and "queue #1" in html
    row = board.conn.execute("SELECT * FROM tasks").fetchone()
    assert row["title"] == "add tests" and row["status"] == "backlog"


def test_create_task_refuses_unknown_repo_and_empty_title(server):
    board, base = server
    board.stop()
    code, body = post(f"{base}/tasks", {"repo": "nope", "title": "x", "prompt": ""})
    assert code == 400 and "unknown repo" in body
    code, body = post(f"{base}/tasks", {"repo": "alpha", "title": "  ", "prompt": ""})
    assert code == 400 and "title" in body


def test_archive_rerun_and_promote_endpoints(server):
    board, base = server
    board.stop()
    code, _ = post(f"{base}/tasks", {"repo": "alpha", "title": "card", "prompt": "p"})
    tid = board.conn.execute("SELECT id FROM tasks").fetchone()[0]
    code, _ = post(f"{base}/tasks/{tid}/archive", {})
    assert board._task(tid)["status"] == "archived"
    board._set_task(tid, status="backlog", runnable=0)
    code, _ = post(f"{base}/tasks/{tid}/promote", {})
    assert board._task(tid)["runnable"] == 1
    board._set_task(tid, status="working")
    code, _ = post(f"{base}/tasks/{tid}/rerun", {})
    assert board._task(tid)["status"] == "backlog"
    code, body = post(f"{base}/tasks/zz/rerun", {})
    assert code == 400


def test_board_escapes_hostile_task_input(server):
    board, base = server
    board.stop()
    board.create_task("<script>alert(1)</script>", "<img src=x>", str(Path(board.repos[0]["path"])))
    code, html = get(f"{base}/")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_working_card_shows_running_badge_with_run_link(server):
    board, base = server
    board.stop()
    tid = board.create_task("watch me", "p", str(Path(board.repos[0]["path"])))
    run = Run.create(board.conn, "plan-build-test: p", agent="a", model="m")
    board._set_task(tid, status="working", latest_run=run.id)
    code, html = get(f"{base}/")
    assert "watch run" in html and f"/run/{run.id}" in html


def test_working_card_without_a_run_still_offers_rerun(server):
    """A card that failed before its run was created (bad preflight, fetch or
    branch, missing test command) is 'working' with latest_run '' — it must
    show the error and a rerun button, not sit as a dead-end without controls."""
    board, base = server
    board.stop()
    tid = board.create_task("bricked", "p", str(Path(board.repos[0]["path"])))
    board._set_task(tid, status="working", note="no origin remote — PR delivery pushes to origin")
    code, html = get(f"{base}/")
    assert "no origin remote" in html
    assert f"/tasks/{tid}/rerun" in html

    # while a live worker owns the card (run not yet bound) the button is withheld
    board._workers[tid] = threading.Thread(name=f"task-{tid}")
    try:
        code, html = get(f"{base}/")
        assert f"/tasks/{tid}/rerun" not in html
    finally:
        board._workers.pop(tid, None)
    code, html = get(f"{base}/")
    assert f"/tasks/{tid}/rerun" in html


def test_cross_site_form_posts_are_refused(server):
    """CSRF: browsers attach the true origin to every form POST, so a foreign
    Origin/Referer is cross-site by definition — refused before any state
    changes. Non-browser clients (curl, tests) send neither header and pass,
    and the board's own same-origin forms keep working."""
    board, base = server
    board.stop()
    tid = board.create_task("csrf bait", "p", str(Path(board.repos[0]["path"])))
    board._set_task(tid, status="working")  # a claimed card, as a rerun would find it
    pid = board.open_pause(tid, "pr_approval", {"pending": "x"}, run_id="")

    code, _ = post(f"{base}/pause/{pid}", {"action": "approve"},
                   headers={"Origin": "http://evil.example"})
    assert code == 403
    assert board.pause_row(pid)["status"] == "waiting"  # nothing was approved

    code, _ = post(f"{base}/tasks/{tid}/rerun", {},
                   headers={"Referer": "https://evil.example/attack.html"})
    assert code == 403
    assert board._task(tid)["status"] == "working"  # nothing was rerun

    code, _ = post(f"{base}/pause/{pid}", {"action": "approve"}, headers={"Origin": base})
    assert code == 303  # same-origin — exactly what the board's own forms send
    assert board.pause_row(pid)["status"] == "approved"


def test_pause_page_renders_questions_and_resolves(server):
    board, base = server
    board.stop()
    tid = board.create_task("clarify", "p", str(Path(board.repos[0]["path"])))
    run = Run.create(board.conn, "plan-build-test: p", agent="a", model="m")
    board._set_task(tid, status="working", latest_run=run.id)
    pid = board.open_pause(tid, "questions", {"questions": ["json or yaml?"]}, run_id=run.id)

    code, html = get(f"{base}/pause/{pid}")
    assert code == 200
    assert "json or yaml?" in html and "send answers" in html
    # the board card links to the pause with a waiting badge
    code, html = get(f"{base}/")
    assert "waiting on you" in html and f"href='/pause/{pid}'" in html

    code, body = post(f"{base}/pause/{pid}", {"action": "send", "answer": "yaml"})
    assert code == 303, body
    row = board.pause_row(pid)
    assert row["status"] == "answered" and row["answer"] == "yaml"

    # an empty answer is refused; a resolved pause renders without a form
    pid2 = board.open_pause(tid, "questions", {"questions": ["more?"]}, run_id=run.id)
    code, body = post(f"{base}/pause/{pid2}", {"action": "send", "answer": "  "})
    assert code == 400 and "empty answer" in body
    board.resolve_pause(pid2, "rejected")
    code, html = get(f"{base}/pause/{pid2}")
    assert "rejected" in html and "<form" not in html


def test_pause_page_renders_plan_and_pr_approvals(server):
    board, base = server
    board.stop()
    tid = board.create_task("approve stuff", "p", str(Path(board.repos[0]["path"])))
    run = Run.create(board.conn, "plan-build-test: p", agent="a", model="m")
    board._set_task(tid, status="working", latest_run=run.id)
    plan_pid = board.open_pause(
        tid, "plan_approval", {"plan_path": "specs/plan.md", "plan": "# Plan\n- do it"},
        run_id=run.id,
    )
    code, html = get(f"{base}/pause/{plan_pid}")
    assert "specs/plan.md" in html and "approve — start building" in html
    code, body = post(f"{base}/pause/{plan_pid}", {"action": "approve", "answer": ""})
    assert board.pause_row(plan_pid)["status"] == "approved"

    pr_pid = board.open_pause(tid, "pr_approval", {"pending": "pending changes:\n M x.py"}, run_id=run.id)
    code, html = get(f"{base}/pause/{pr_pid}")
    assert "M x.py" in html and "push" in html
    code, body = post(f"{base}/pause/{pr_pid}", {"action": "reject", "answer": ""})
    assert board.pause_row(pr_pid)["status"] == "rejected"


def test_in_pr_and_archived_cards_link_to_their_run(server):
    """The trail of a finished task must stay reachable: In PR and Archived
    cards link to the run page (phases, events, pauses), not just the PR."""
    board, base = server
    board.stop()
    tid = board.create_task("shipped task", "p", str(Path(board.repos[0]["path"])))
    run = Run.create(board.conn, "plan-build-test: p", agent="a", model="m")
    board._set_task(tid, status="in_pr", latest_run=run.id, pr_url="http://x/pr/1")
    code, html = get(f"{base}/")
    assert f"<a href='/run/{run.id}'>run</a>" in html          # In PR card
    assert "http://x/pr/1" in html
    board.archive_task(tid)
    code, html = get(f"{base}/")
    assert f"<a href='/run/{run.id}'>run</a>" in html          # Archived entry
    assert "shipped task" in html


def test_pause_page_renders_cleanup_approval(server):
    board, base = server
    board.stop()
    tid = board.create_task("messy", "p", str(Path(board.repos[0]["path"])))
    run = Run.create(board.conn, "plan-build-test: p", agent="a", model="m")
    board._set_task(tid, status="working", latest_run=run.id)
    pid = board.open_pause(tid, "cleanup_approval",
                           {"pending": "pending changes:\n M src/x.php"}, run_id=run.id)
    code, html = get(f"{base}/pause/{pid}")
    assert code == 200
    assert "left these changes uncommitted" in html and "M src/x.php" in html
    assert "discard the attempt" in html and "keep them" in html
    code, body = post(f"{base}/pause/{pid}", {"action": "reject", "answer": ""})
    assert board.pause_row(pid)["status"] == "rejected"
    # the card's waiting badge names the cleanup pause
    pid2 = board.open_pause(tid, "cleanup_approval", {"pending": "x"}, run_id=run.id)
    code, html = get(f"{base}/")
    # the headline contains an apostrophe: match the escaped form's stable prefix
    assert "worktree has this run" in html


def test_unknown_pages(server):
    board, base = server
    code, html = get(f"{base}/pause/9999")
    assert code == 404
    code, html = get(f"{base}/pause/not-a-number")
    assert code == 404
    code, html = get(f"{base}/nowhere")
    assert code == 404


def test_run_detail_still_served_by_the_board(server):
    board, base = server
    run = Run.create(board.conn, "board run", agent="a", model="m")
    code, html = get(f"{base}/run/{run.id}")
    assert code == 200 and "board run" in html
    code, html = get(f"{base}/runs")
    assert code == 200 and "board run" in html
