"""Board logic tests: queue, workers, pauses, delivery, reaper — no HTTP."""

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from factory.board import Board, collect_discovered
from factory.chains import CheckpointDeclined, run_plan_build_test
from factory.config import load_config
from factory.db import connect
from factory.run import Run

ROSTER = """
defaults:
  coding_agent: pi
  model: pi/m
  test_command: echo tests-ok
  max_concurrent_runs: {cap}
repos:
  - name: alpha
    path: {alpha}
  - name: beta
    path: {beta}
    test_command: echo beta-ok
agents:
  - name: a
    purpose: p
  - name: planner
    purpose: plans
  - name: builder
    purpose: builds
"""


def _git_init(repo: Path) -> None:
    """A repo plus a bare origin carrying main — the worker fetches and cuts
    each task branch from origin/<default>, so the origin must exist and have
    the branch, even offline."""
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), capture_output=True, check=True)
    (repo / "README.md").write_text("base\n")
    for args in (["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init"]):
        subprocess.run(["git", *args], cwd=str(repo), capture_output=True, check=True)
    bare = repo.parent / f"{repo.name}.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], capture_output=True, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=str(repo),
                   capture_output=True, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=str(repo),
                   capture_output=True, check=True)


def make_board(tmp_path, cap=2, chain_fn=None, skip_preflight=True):
    text = ROSTER.format(cap=cap, alpha=tmp_path / "alpha", beta=tmp_path / "beta")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(text)
    cfg = load_config(cfg_path)
    for r in cfg.get("repos", []):
        repo = Path(r["path"])
        repo.mkdir(parents=True, exist_ok=True)
        _git_init(repo)  # real repos: the worker checks out a task branch
    db = tmp_path / "data" / "factory.db"
    conn = connect(db)
    board = Board(conn, cfg, config_dir=tmp_path, db_path=db,
                  chain_fn=chain_fn or run_plan_build_test)
    if skip_preflight:
        # Unit tests exercise the queue/worker logic, not the git+gh preflight —
        # one dedicated test covers the real check.
        board._preflight = lambda repo_dir: ""
    return board


def repo_path(board, name: str) -> str:
    return str(board.repos_by_name[name]["path"])


def fake_chain(accepted=True, hold=None):
    """A chain body that looks like run_plan_build_test to the board."""
    def chain(conn, request, planner, builder, **kwargs):
        if hold is not None:
            hold.wait(timeout=10)  # hold the worker open so tests can observe scheduling
        kwargs["on_run"](Run.create(conn, request, agent="a", model="m"))
        return accepted, "" if accepted else "nope"
    return chain


def wait_until(fn, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def test_new_tables_exist_on_a_legacy_db(tmp_path):
    import sqlite3

    legacy = tmp_path / "factory.db"
    old = sqlite3.connect(legacy)
    old.executescript("CREATE TABLE sessions (id TEXT PRIMARY KEY, request TEXT);")
    old.commit()
    old.close()
    conn = connect(legacy)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tasks", "pauses"} <= names


def test_create_task_defaults_and_system_cards(tmp_path):
    board = make_board(tmp_path)
    board.stop()
    tid = board.create_task("ship it", "do it", repo_path(board, "alpha"))
    row = board._task(tid)
    assert row["origin"] == "owner" and row["runnable"] == 1 and row["status"] == "backlog"
    sys_id = board.create_task("found a bug", "details", repo_path(board, "alpha"), origin="system", runnable=False)
    srow = board._task(sys_id)
    assert srow["runnable"] == 0  # the factory proposes, the owner promotes
    with pytest.raises(ValueError):
        board.create_task("   ", "", repo_path(board, "alpha"))


def test_dispatch_respects_global_cap_and_per_repo_exclusivity(tmp_path):
    hold = threading.Event()
    board = make_board(tmp_path, cap=2, chain_fn=fake_chain(hold=hold))
    repo_a = repo_path(board, "alpha")
    repo_b = repo_path(board, "beta")
    t1 = board.create_task("one", "r1", repo_a)
    t2 = board.create_task("two", "r2", repo_a)    # same repo: queues behind t1
    t3 = board.create_task("three", "r3", repo_b)  # other repo: runs in parallel
    board.start()
    wait_until(lambda: len(board._workers) == 2)
    assert set(board._worker_repos.values()) == {repo_a, repo_b}
    assert board._task(t2)["status"] == "backlog"  # queued, never started beside t1
    hold.set()
    wait_until(lambda: board._task(t1)["status"] == "in_pr")
    wait_until(lambda: board._task(t3)["status"] == "in_pr")
    # FIFO: t2 runs once its repo frees (the transient 'working' window is too
    # short to poll — the settled state is the honest observation).
    wait_until(lambda: board._task(t2)["status"] == "in_pr")
    wait_until(lambda: not board._workers)
    board.stop()
    assert board._task(t2)["status"] == "in_pr"


def test_task_records_branch_latest_run_and_pr(tmp_path, monkeypatch):
    def chain(conn, request, planner, builder, **kwargs):
        run = Run.create(conn, request, agent="a", model="m")
        kwargs["on_run"](run)
        kwargs["deliver_fn"](run.id, "msg")
        return True, ""

    board = make_board(tmp_path, cap=1, chain_fn=chain)
    monkeypatch.setattr(board, "_deliver",
                        lambda repo_dir, branch, title, prompt, run_id:
                        {"pr_url": "http://x/pr/1", "branch": branch})
    tid = board.create_task("deliver", "r", repo_path(board, "alpha"))
    board.start()
    wait_until(lambda: board._task(tid)["status"] == "in_pr")
    board.stop()
    row = board._task(tid)
    assert row["latest_run"] and row["branch"] == f"factory/{tid}"
    assert row["pr_url"] == "http://x/pr/1"
    # the task branch really exists in the target repo
    branches = subprocess.run(
        ["git", "branch", "--list", row["branch"]], cwd=row["repo"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert f"factory/{tid}" in branches


def _committing_chain(filename: str):
    """A chain that commits one file on the task branch, like the real chain's
    plan commit does — so branch topology is exercised for real."""
    def chain(conn, request, planner, builder, repo_dir, **kwargs):
        run = Run.create(conn, request, agent="a", model="m")
        kwargs["on_run"](run)
        (Path(repo_dir) / filename).write_text("work\n")
        for args in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", request]):
            subprocess.run(["git", *args], cwd=str(repo_dir), capture_output=True, check=True)
        return True, ""
    return chain


def test_task_branches_start_from_the_default_branch(tmp_path):
    """Sequential tasks on one repo: the second branch is cut from the default
    branch, never off the previous task's branch (PLAN.md Phase 8)."""
    board = make_board(tmp_path, cap=1, chain_fn=_committing_chain("a.txt"))
    repo = Path(repo_path(board, "alpha"))
    t1 = board.create_task("first", "r1", str(repo))
    board.start()
    wait_until(lambda: board._task(t1)["status"] == "in_pr")
    board.chain_fn = _committing_chain("b.txt")
    t2 = board.create_task("second", "r2", str(repo))
    wait_until(lambda: board._task(t2)["status"] == "in_pr")
    board.stop()

    def subjects(branch: str) -> str:
        return subprocess.run(
            ["git", "log", "--format=%s", branch], cwd=str(repo),
            capture_output=True, text=True, check=True,
        ).stdout

    assert "r1" in subjects(f"factory/{t1}")      # t1's work landed on t1's branch
    assert "r1" not in subjects(f"factory/{t2}")  # and never leaked into t2's branch
    assert "r2" in subjects(f"factory/{t2}")


def test_rerun_replaces_a_stale_task_branch(tmp_path):
    """A card that already started owns a branch (never deleted on failure), so
    its rerun must replace that stale branch instead of failing at creation."""
    board = make_board(tmp_path, cap=1, chain_fn=fake_chain(accepted=False))
    tid = board.create_task("retry me", "r", repo_path(board, "alpha"))
    board.start()
    wait_until(lambda: "nope" in board._task(tid)["note"])
    board.stop()
    assert board._task(tid)["branch"] == f"factory/{tid}"
    board.rerun_task(tid)
    board.chain_fn = fake_chain(accepted=True)
    board._execute(tid)  # direct, no dispatcher running after stop()
    row = board._task(tid)
    assert row["status"] == "in_pr" and row["note"] == "" and row["branch"] == f"factory/{tid}"


def test_pause_round_trip_wakes_the_worker(tmp_path):
    board = make_board(tmp_path, cap=1)
    tid = board.create_task("parker", "r", repo_path(board, "alpha"))
    seen = {}

    def chain(conn, request, planner, builder, **kwargs):
        run = Run.create(conn, request, agent="a", model="m")
        kwargs["on_run"](run)
        pid = board.open_pause(tid, "questions", {"questions": ["which shape?"]}, run_id=run.id)
        seen["pause_id"] = pid
        seen["status"] = board.wait_pause(pid, timeout_s=5)
        seen["row"] = board.pause_row(pid)
        return True, ""

    board.chain_fn = chain
    board.start()
    wait_until(lambda: "pause_id" in seen)
    board.resolve_pause(seen["pause_id"], "answered", "a hexagon")
    wait_until(lambda: "row" in seen)
    board.stop()
    assert seen["status"] == "answered"
    assert seen["row"]["answer"] == "a hexagon"
    assert board._task(tid)["latest_run"] != ""  # on_run bound the card to its run


def test_resolve_pause_validates_state(tmp_path):
    board = make_board(tmp_path)
    board.stop()
    tid = board.create_task("t", "p", repo_path(board, "alpha"))
    pid = board.open_pause(tid, "pr_approval", {"pending": "x"}, run_id="")
    with pytest.raises(ValueError):
        board.resolve_pause(pid, "bogus")
    board.resolve_pause(pid, "approved")
    with pytest.raises(ValueError):
        board.resolve_pause(pid, "rejected")  # already resolved
    with pytest.raises(ValueError):
        board.resolve_pause(9999, "approved")


def test_promote_archive_rerun_rules(tmp_path):
    board = make_board(tmp_path)
    board.stop()
    tid = board.create_task("t", "p", repo_path(board, "alpha"))
    board.archive_task(tid)
    with pytest.raises(ValueError):
        board.promote_task(tid)  # only backlog cards can be promoted
    sys_id = board.create_task("s", "d", repo_path(board, "alpha"), origin="system", runnable=False)
    board.promote_task(sys_id)
    assert board._task(sys_id)["runnable"] == 1
    board.archive_task(sys_id)
    assert board._task(sys_id)["status"] == "archived"
    with pytest.raises(ValueError):
        board.rerun_task("nope")
    # in_pr cards are not rerunnable
    t2 = board.create_task("t2", "p", repo_path(board, "alpha"))
    board._set_task(t2, status="in_pr", pr_url="http://x")
    with pytest.raises(ValueError):
        board.rerun_task(t2)


def test_failed_run_leaves_a_working_card_with_note(tmp_path):
    board = make_board(tmp_path, cap=1, chain_fn=fake_chain(accepted=False))
    tid = board.create_task("doomed", "r", repo_path(board, "alpha"))
    board.start()
    wait_until(lambda: "nope" in board._task(tid)["note"])
    board.stop()
    assert board._task(tid)["status"] == "working"  # failed badge, rerun available


def test_failed_run_restores_the_worktree_for_reruns(tmp_path):
    """A failed run leaves its uncommitted edits in the target repo — the board
    put them there, so the board's own deterministic git path removes them: the
    rerun button meets a clean baseline, never a permanent 'working tree not
    clean' wall (nothing outside the board touches git)."""
    def dirty_failing_chain(conn, request, planner, builder, repo_dir, **kwargs):
        run = Run.create(conn, request, agent="a", model="m")
        kwargs["on_run"](run)
        (Path(repo_dir) / "feature.txt").write_text("claimed but tests failed\n")
        return False, "tests failed in round 3 (exit 1)"

    board = make_board(tmp_path, cap=1, chain_fn=dirty_failing_chain)
    repo = Path(repo_path(board, "alpha"))
    tid = board.create_task("doomed", "r", str(repo))
    board.start()
    wait_until(lambda: "tests failed" in board._task(tid)["note"])
    board.stop()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                            capture_output=True, text=True, check=True).stdout
    assert status.strip() == ""  # the failed run's debris is gone
    board.chain_fn = fake_chain(accepted=True)
    board.rerun_task(tid)
    board._execute(tid)  # direct, no dispatcher running after stop()
    assert board._task(tid)["status"] == "in_pr"  # the rerun actually recovered


def test_declined_run_restores_the_worktree_too(tmp_path):
    """A rejected checkpoint leaves the changes uncommitted by design — the
    decline decided against them, so the board removes the debris like any
    failed run and the owner's rejection never bricks the repo."""
    def declining_chain(conn, request, planner, builder, repo_dir, **kwargs):
        run = Run.create(conn, request, agent="a", model="m")
        kwargs["on_run"](run)
        (Path(repo_dir) / "impl.txt").write_text("uncommitted by design\n")
        raise CheckpointDeclined("declined at the pre-commit checkpoint — changes left uncommitted")

    board = make_board(tmp_path, cap=1, chain_fn=declining_chain)
    repo = Path(repo_path(board, "alpha"))
    tid = board.create_task("rejected", "r", str(repo))
    board.start()
    wait_until(lambda: "declined" in board._task(tid)["note"])
    board.stop()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                            capture_output=True, text=True, check=True).stdout
    assert status.strip() == ""
    assert board._task(tid)["status"] == "working"


def test_preflight_notes_are_recorded(tmp_path):
    board = make_board(tmp_path, cap=1, chain_fn=fake_chain(), skip_preflight=False)
    tid = board.create_task("ghost hunt", "r", str(tmp_path / "ghost"))
    board.start()
    wait_until(lambda: "does not exist" in board._task(tid)["note"])
    board.stop()
    assert board._task(tid)["status"] == "working" and board._task(tid)["latest_run"] == ""


def test_reaper_settles_orphaned_runs_and_expires_pauses(tmp_path):
    board = make_board(tmp_path, cap=1)
    board.stop()
    run = Run.create(board.conn, "plan-build-test: ghost", agent="a", model="m", launched_by="board")
    board.conn.execute(
        "INSERT INTO phases (run_id, seq, name, kind, owner, status, started_at)"
        " VALUES (?, 1, 'plan', 'agent', 'a', 'fail', '2026-11-07T00:00:00+00:00')",
        (run.id,),
    )
    board.conn.commit()  # an open phase: the daemon died mid-phase, ended_at NULL
    task = board.create_task("ghost card", "p", repo_path(board, "alpha"))
    board.open_pause(task, "questions", {"questions": ["?"]}, run_id=run.id)
    board._set_task(task, status="working", latest_run=run.id)

    board.start()  # start() reaps
    board.stop()

    sess = board.conn.execute(
        "SELECT status, ended_at FROM sessions WHERE id = ?", (run.id,)
    ).fetchone()
    assert sess["status"] == "failed" and sess["ended_at"]
    ph = board.conn.execute(
        "SELECT status, error FROM phases WHERE run_id = ?", (run.id,)
    ).fetchone()
    assert ph["status"] == "fail" and "restarted" in ph["error"]
    pz = board.conn.execute("SELECT status FROM pauses").fetchone()
    assert pz["status"] == "expired"
    ev = board.conn.execute(
        "SELECT type FROM events WHERE run_id = ?", (run.id,)
    ).fetchone()
    assert ev["type"] == "reaped"
    assert "rerun" in board._task(task)["note"]


def test_reaper_leaves_orphaned_worktrees_alone(tmp_path):
    """Post-crash dirt is unattributable — the dead run's debris and the
    owner's uncommitted edits look identical — so the reaper never restores;
    the card tells the owner to review the tree, and the rerun's preflight
    refuses until it is clean."""
    board = make_board(tmp_path, cap=1)
    board.stop()
    repo = Path(repo_path(board, "alpha"))
    (repo / "maybe-owner-wip.txt").write_text("unattributable\n")
    run = Run.create(board.conn, "plan-build-test: ghost", agent="a", model="m",
                     repo=str(repo), launched_by="board")
    board.conn.commit()
    task = board.create_task("ghost card", "p", str(repo))
    board._set_task(task, status="working", latest_run=run.id)

    board.start()  # start() reaps
    board.stop()
    assert "rerun" in board._task(task)["note"]
    status = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                            capture_output=True, text=True, check=True).stdout
    assert "maybe-owner-wip.txt" in status  # untouched: could be the owner's WIP


def test_reaper_leaves_foreign_running_sessions_alone(tmp_path):
    """The trace db is shared: CLI commands record their own 'running' sessions
    that are still live while the board starts — the board must not settle them
    and above all must not 'git reset --hard' a repo a live CLI run is writing
    to. Only runs the board itself launched are reaped."""
    board = make_board(tmp_path, cap=1)
    board.stop()
    repo = Path(repo_path(board, "alpha"))
    run = Run.create(board.conn, "plan-build-test: live cli run", agent="a", model="m",
                     repo=str(repo))  # launched_by '': CLI, not the board
    (repo / "cli-wip.txt").write_text("a live CLI run's uncommitted work\n")
    board.start()  # start() reaps — foreign sessions must survive untouched
    board.stop()
    sess = board.conn.execute("SELECT status FROM sessions WHERE id = ?", (run.id,)).fetchone()
    assert sess["status"] == "running"
    assert (repo / "cli-wip.txt").exists()  # the CLI run's work was not reset away


def test_reaper_flags_cards_claimed_but_never_bound(tmp_path):
    """The daemon died between _dispatch marking the card 'working' and on_run
    recording latest_run: no session matches the card, yet it must come back
    flagged for a rerun instead of an unexplained dead-end."""
    board = make_board(tmp_path, cap=1)
    board.stop()
    task = board.create_task("claimed", "p", repo_path(board, "alpha"))
    board._set_task(task, status="working")  # claimed; latest_run and note still ''
    board.start()
    board.stop()
    assert "rerun" in board._task(task)["note"]


def test_discovered_cards_spawn_when_the_chain_raises(tmp_path):
    """Out-of-scope findings become cards on every ending of a run — a chain
    that raises (failed push, declined pause) still recorded its envelopes in
    the trace; dropping them silently would break the Phase 8 contract."""
    runs = []

    def failing_chain(conn, request, planner, builder, **kwargs):
        run = Run.create(conn, request, agent="a", model="m")
        kwargs["on_run"](run)
        with run.phase("build:r1", kind="agent", owner="a") as phase:
            conn.execute(
                "INSERT INTO envelopes (phase_id, attempt, valid, data, created_at)"
                " VALUES (?, 1, 1, ?, 'now')",
                (phase.id, json.dumps({
                    "status": "success",
                    "discovered": [{"title": "out of scope bug", "detail": "fix later"}],
                })),
            )
        runs.append(run.id)
        raise RuntimeError("gh pr create failed: not a github origin")

    board = make_board(tmp_path, cap=1, chain_fn=failing_chain)
    tid = board.create_task("discoverer", "r", repo_path(board, "alpha"))
    board.start()
    wait_until(lambda: "RuntimeError" in board._task(tid)["note"])
    board.stop()
    cards = board.conn.execute("SELECT * FROM tasks WHERE origin = 'system'").fetchall()
    assert len(cards) == 1
    card = cards[0]
    assert card["title"] == "out of scope bug"
    assert card["origin_run"] == runs[0]
    assert card["runnable"] == 0 and card["status"] == "backlog"  # the owner promotes


def test_collect_discovered_parses_envelopes(tmp_path):
    board = make_board(tmp_path)
    board.stop()
    run = Run.create(board.conn, "r", agent="a", model="m")
    with run.phase("build:r1", kind="agent", owner="a") as phase:
        board.conn.execute(
            "INSERT INTO envelopes (phase_id, attempt, valid, data, created_at)"
            " VALUES (?, 1, 1, ?, ?)",
            (
                phase.id,
                json.dumps({
                    "status": "success",
                    "discovered": [
                        {"title": "legacy bug in auth", "detail": "expires tokens twice"},
                        {"title": ""},  # no title: skipped
                        "not a dict",   # skipped
                    ],
                }),
                "now",
            ),
        )
    items = collect_discovered(board.conn, run.id)
    assert len(items) == 1
    assert items[0]["title"] == "legacy bug in auth"


def test_discovered_cards_created_with_provenance_and_deduped(tmp_path, monkeypatch):
    runs = []

    def chain(conn, request, planner, builder, **kwargs):
        run = Run.create(conn, request, agent="a", model="m")
        kwargs["on_run"](run)
        with run.phase("build:r1", kind="agent", owner="a") as phase:
            conn.execute(
                "INSERT INTO envelopes (phase_id, attempt, valid, data, created_at)"
                " VALUES (?, 1, 1, ?, ?)",
                (
                    phase.id,
                    json.dumps({
                        "status": "success",
                        "discovered": [{"title": "clean up specs dir", "detail": "old plans"}],
                    }),
                    "now",
                ),
            )
        kwargs["deliver_fn"](run.id, "msg")
        runs.append(run.id)
        return True, ""

    board = make_board(tmp_path, cap=1, chain_fn=chain)
    monkeypatch.setattr(board, "_deliver",
                        lambda repo_dir, branch, title, prompt, run_id:
                        {"pr_url": "http://x/pr/2", "branch": branch})
    tid = board.create_task("main task", "r", repo_path(board, "alpha"))
    board.start()
    wait_until(lambda: board._task(tid)["status"] == "in_pr")
    # a second task on the same repo would rediscover the same finding
    tid2 = board.create_task("main task two", "r", repo_path(board, "alpha"))
    wait_until(lambda: board._task(tid2)["status"] == "in_pr")
    wait_until(lambda: len(runs) >= 2)
    wait_until(lambda: not board._workers)
    board.stop()
    cards = board.conn.execute("SELECT * FROM tasks WHERE origin = 'system'").fetchall()
    assert len(cards) == 1  # deduped across runs
    card = cards[0]
    assert card["origin_run"] == runs[0] and card["runnable"] == 0
    assert card["status"] == "backlog"


def test_stop_expires_open_pauses_and_workers_unwind(tmp_path):
    board = make_board(tmp_path, cap=1)
    tid = board.create_task("slow", "r", repo_path(board, "alpha"))
    started = threading.Event()
    seen = {}

    def chain(conn, request, planner, builder, **kwargs):
        run = Run.create(conn, request, agent="a", model="m")
        kwargs["on_run"](run)
        pid = board.open_pause(tid, "plan_approval", {"plan": "..."}, run_id=run.id)
        started.set()
        seen["status"] = board.wait_pause(pid, timeout_s=30)
        raise Exception("unwinding after shutdown")

    board.chain_fn = chain
    board.start()
    started.wait(timeout=5)
    board.stop()
    assert not board._workers  # stop() joins workers: no post-close db races
    assert seen["status"] == "expired"
    status = board.conn.execute("SELECT status FROM pauses").fetchone()["status"]
    assert status == "expired"
    assert "unwinding" in board._task(tid)["note"]  # the worker recorded its own exit


def test_pr_poller_archives_merged_cards(tmp_path, monkeypatch):
    board = make_board(tmp_path, cap=1)  # loops never started: no stop() here —
    # stop() latches the stop flag and _poll_prs_once would return early
    tid = board.create_task("shipped", "r", repo_path(board, "alpha"))
    board._set_task(tid, status="in_pr", pr_url="http://x/pr/9")
    monkeypatch.setattr(board, "_pr_state", lambda url, repo: "MERGED")
    board._poll_prs_once()
    assert board._task(tid)["status"] == "archived"
    assert board._task(tid)["note"] == "PR merged"
    # a non-merged state leaves the card alone
    monkeypatch.setattr(board, "_pr_state", lambda url, repo: "OPEN")
    board._set_task(tid, status="in_pr", note="")
    board._poll_prs_once()
    assert board._task(tid)["status"] == "in_pr"


def test_real_preflight_reports_the_first_problem(tmp_path):
    """skip_preflight=False: the genuine check — a clean repo with no origin."""
    board = make_board(tmp_path, cap=1, skip_preflight=False)
    board.stop()
    tid = board.create_task("real check", "r", repo_path(board, "alpha"))
    # _git_init gave every repo a bare origin — remove it to test the no-origin case
    subprocess.run(["git", "remote", "remove", "origin"],
                   cwd=repo_path(board, "alpha"), capture_output=True, check=True)
    board._execute(tid)  # direct, no thread
    assert "no origin remote" in board._task(tid)["note"]

    # with the origin back (the bare from _git_init already carries main),
    # preflight passes and the branch is cut from origin/<default>
    subprocess.run(["git", "remote", "add", "origin", str(tmp_path / "alpha.git")],
                   cwd=repo_path(board, "alpha"), capture_output=True, check=True)
    board._set_task(tid, note="")
    board.chain_fn = fake_chain()  # never the real chain in a test
    board._execute(tid)
    assert board._task(tid)["note"] == ""
    assert board._task(tid)["status"] == "in_pr"
    base = subprocess.run(
        ["git", "merge-base", f"factory/{tid}", "origin/main"],
        cwd=repo_path(board, "alpha"), capture_output=True, text=True, check=True,
    ).stdout.strip()
    tip = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=repo_path(board, "alpha"), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert base == tip  # the branch's base IS the remote default
    assert board._task(tid)["status"] == "in_pr"


def test_deliver_pushes_the_branch_and_parses_the_pr_url(tmp_path, monkeypatch):
    """The real delivery: git push to a local bare origin, gh faked."""
    board = make_board(tmp_path, cap=1)
    board.stop()
    repo = Path(repo_path(board, "alpha"))
    origin = tmp_path / "deliver-origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], capture_output=True, check=True)
    subprocess.run(["git", "remote", "remove", "origin"], cwd=str(repo),
                   capture_output=True, check=True)  # drop the _git_init origin first
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=str(repo),
                   capture_output=True, check=True)
    subprocess.run(["git", "branch", "factory/t1"], cwd=str(repo), capture_output=True, check=True)

    calls = []
    real_run = subprocess.run  # captured before patching: fake_gh delegates to it

    def fake_gh(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "gh":
            return subprocess.CompletedProcess(cmd, 0, stdout="https://github.com/x/y/pull/5\n", stderr="")
        return real_run(cmd, **kwargs)  # the git push stays real

    monkeypatch.setattr("factory.board.subprocess.run", fake_gh)
    info = board._deliver(repo, "factory/t1", "title", "prompt", "run1")
    assert info == {"pr_url": "https://github.com/x/y/pull/5", "branch": "factory/t1"}
    assert calls[-1][:4] == ["gh", "pr", "create", "--title"]
    assert "--head" in calls[-1] and "factory/t1" in calls[-1]
    # the branch really arrived at the origin
    remote_branches = subprocess.run(
        ["git", "branch", "--list", "factory/t1"], cwd=str(origin),
        capture_output=True, text=True, check=True,
    ).stdout
    assert "factory/t1" in remote_branches


def test_deliver_surfaces_push_failures(tmp_path):
    board = make_board(tmp_path, cap=1)
    board.stop()
    repo = Path(repo_path(board, "alpha"))
    with pytest.raises(RuntimeError, match="push failed"):
        board._deliver(repo, "factory/none", "title", "prompt", "run1")


def test_kill_live_agents_kills_the_registered_process():
    """The registry is the shutdown hook's raw material: registered = killable."""
    import factory.agent_pi as ap

    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    with ap._live_lock:
        ap._live_procs.add(proc)
    ap.kill_live_agents()
    assert proc.wait(timeout=10) != 0  # SIGKILLed, not left running for 30s


def test_stop_kills_inflight_agent_subprocesses(tmp_path):
    """Shutdown must not orphan agent children: stop() kills live pi processes,
    so every blocked chain unwinds and its run settles in the trace."""
    import threading

    import factory.agent_pi as ap

    board = make_board(tmp_path, cap=1)
    tid = board.create_task("slow agent", "r", repo_path(board, "alpha"))
    started = threading.Event()
    finished = {}

    def chain(conn, request, planner, builder, **kwargs):
        run = Run.create(conn, request, agent="a", model="m")
        kwargs["on_run"](run)
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        with ap._live_lock:
            ap._live_procs.add(proc)
        started.set()  # only now is the agent registered and killable
        try:
            proc.wait(timeout=60)
        finally:
            with ap._live_lock:
                ap._live_procs.discard(proc)
        finished["code"] = proc.returncode
        return True, ""

    board.chain_fn = chain
    board.start()
    started.wait(timeout=5)
    board.stop()  # expires nothing here, but must kill the registered subprocess
    wait_until(lambda: not board._workers, timeout_s=10)
    assert finished["code"] != 0          # killed by stop(), not waited out
    assert board._task(tid)["status"] == "in_pr"  # the chain unwound and settled
