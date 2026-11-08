"""The task board: the factory's cockpit (Phases 6-8).

One long-lived ``factory serve`` process owns execution. Cards live in the
``tasks`` table; executing one creates a run exactly like the CLI does. The
dispatcher starts at most one run per repo (runs on the same repo would step on
each other's git state), FIFO within a repo, repos concurrent, all bounded by a
global run cap sized to the box's RAM. Human gates (Phase 7) are rows in the
``pauses`` table: the chain's callback blocks on an event bound to its pause
row, the owner answers from the browser, the same pi session resumes. Delivery
(Phase 8) is a branch per task, a push, and ``gh pr create`` — findings the
agents declare out-of-scope become new Backlog cards instead of scope creep,
on every ending of the run (a chain that raises still recorded its envelopes).
A run that fails or is declined leaves the repo as it started: the board
restores the worktree it dirtied (deterministic git again), so the advertised
rerun button always meets a clean baseline.

Threading model: one shared write connection (``check_same_thread=False``),
serialized at the connection itself — every execute (through full result
consumption), commit and rollback holds ``db.LockedConnection``'s lock, so
concurrent chain writes, pause SELECTs and board mutations cannot interleave
inside the connection's sqlite3 state. ``self._lock`` stays for what is more
than a statement: board mutations that also touch the in-memory maps (workers,
pause events) hold it across their whole unit. Render connections stay
``query_only`` (see ``ui.ui_connect``): the UI's read-only guarantee doesn't
relax, it stops being the whole story.
"""

import json
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .agent_pi import kill_live_agents
from .chains import (
    DEFAULT_REVIEW_LENSES,
    CheckpointDeclined,
    _git,
    _pending_changes_summary,
    run_plan_build_test,
)
from .config import resolve_agent, resolve_repos
from .db import trace_exemptions
from .envelope import extract_json
from .web import kanban, runs
from .web.scaffold import esc, page, phase_label, started_epoch, static_asset, ui_connect

FORM_CAP = 1_000_000  # max bytes of a POST body — a form, not an upload endpoint
ARCHIVED_LIMIT = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_task_id() -> str:
    return uuid.uuid4().hex[:8]


def collect_discovered(conn, run_id: str) -> list[dict]:
    """Out-of-scope findings the run's agents declared in their envelopes.

    These must NOT enter the change under review — the fixer and the gates may
    not absorb them (the envelope contract says so). Deterministic code turns
    each into a Backlog card with provenance; nothing here is an agent decision.
    """
    if not run_id:
        return []
    rows = conn.execute(
        "SELECT e.data FROM envelopes e JOIN phases p ON e.phase_id = p.id"
        " WHERE p.run_id = ? AND e.valid = 1 ORDER BY e.id",
        (run_id,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            data = extract_json(r["data"])
        except ValueError:
            continue
        for item in data.get("discovered") or []:
            if isinstance(item, dict) and item.get("title"):
                out.append({
                    "title": str(item["title"])[:200],
                    "detail": str(item.get("detail", ""))[:2000],
                })
    return out


# --- the board ----------------------------------------------------------------


class Board:
    """Owns the cards, the queue, the workers, and the human gates."""

    def __init__(self, conn, cfg: dict, config_dir: Path, db_path: Path,
                 chain_fn=run_plan_build_test):
        self.conn = conn
        self.cfg = cfg
        self.config_dir = Path(config_dir)
        self.db_path = Path(db_path)
        self.chain_fn = chain_fn  # injectable: tests swap the whole chain
        self.repos = resolve_repos(cfg)
        self.repos_by_name = {r["name"]: r for r in self.repos}
        self._cap = int(cfg["defaults"].get("max_concurrent_runs", 1))
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._events: dict[int, threading.Event] = {}    # pause id -> wake event
        self._workers: dict[str, threading.Thread] = {}  # task id -> live worker
        self._worker_repos: dict[str, str] = {}          # task id -> repo path
        self._threads: list[threading.Thread] = []

    # -- tasks ---------------------------------------------------------------

    def create_task(self, title: str, prompt: str, repo: str, origin: str = "owner",
                    runnable: bool | None = None, origin_run: str = "") -> str:
        """Insert a card. System cards default to unrunnable: the factory may
        propose work, only the owner promotes it."""
        title = title.strip()
        if not title:
            raise ValueError("task title must not be empty")
        if runnable is None:
            runnable = origin == "owner"
        repo = str(Path(repo).resolve())
        task_id = new_task_id()
        now = _now()
        with self._lock:
            self.conn.execute(
                "INSERT INTO tasks (id, title, prompt, repo, origin, runnable, status,"
                " created_at, updated_at, origin_run)"
                " VALUES (?, ?, ?, ?, ?, ?, 'backlog', ?, ?, ?)",
                (task_id, title, prompt.strip(), repo, origin, int(runnable), now, now, origin_run),
            )
            self.conn.commit()
        self._wake.set()
        return task_id

    def _task(self, task_id: str):
        return self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    def _set_task(self, task_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self.conn.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id))
            self.conn.commit()

    def promote_task(self, task_id: str, title: str = "", prompt: str = "") -> None:
        """The owner reviews a system-proposed card and makes it runnable."""
        row = self._task(task_id)
        if row is None:
            raise ValueError(f"unknown task {task_id}")
        if row["status"] != "backlog":
            raise ValueError("only backlog cards can be promoted")
        with self._lock:
            self.conn.execute(
                "UPDATE tasks SET runnable = 1, title = ?, prompt = ?, updated_at = ? WHERE id = ?",
                (title.strip() or row["title"], prompt.strip() or row["prompt"], _now(), task_id),
            )
            self.conn.commit()
        self._wake.set()

    def archive_task(self, task_id: str) -> None:
        row = self._task(task_id)
        if row is None:
            raise ValueError(f"unknown task {task_id}")
        self._set_task(task_id, status="archived")

    def rerun_task(self, task_id: str) -> None:
        """Re-queue a card whose last run settled (failed, or declined at a gate)."""
        with self._lock:
            if task_id in self._workers:
                raise ValueError("that task is running right now")
            row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown task {task_id}")
            if row["status"] == "in_pr":
                raise ValueError("a card with an open PR is not rerun — archive it or push again")
            self.conn.execute(
                "UPDATE tasks SET status = 'backlog', runnable = 1, note = '', updated_at = ?"
                " WHERE id = ?",
                (_now(), task_id),
            )
            self.conn.commit()
        self._wake.set()

    # -- the queue (one run per repo, FIFO within a repo, global cap) ----------

    def _dispatch(self) -> None:
        with self._lock:
            if len(self._workers) >= self._cap:
                return
            busy = set(self._worker_repos.values())
            rows = self.conn.execute(
                "SELECT * FROM tasks WHERE status = 'backlog' AND runnable = 1"
                " ORDER BY created_at, rowid"  # rowid = true insertion order: FIFO
            ).fetchall()
            for row in rows:
                if len(self._workers) >= self._cap:
                    break
                if row["repo"] in busy:
                    continue
                self.conn.execute(
                    "UPDATE tasks SET status = 'working', note = '', updated_at = ? WHERE id = ?",
                    (_now(), row["id"]),
                )
                self.conn.commit()
                busy.add(row["repo"])
                worker = threading.Thread(
                    target=self._worker, args=(row["id"],), daemon=True, name=f"task-{row['id']}"
                )
                self._workers[row["id"]] = worker
                self._worker_repos[row["id"]] = row["repo"]
                worker.start()

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=5)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self._dispatch()
            except Exception as exc:  # the queue must survive a bad card
                print(f"dispatch error: {type(exc).__name__}: {exc}", file=sys.stderr)

    # -- pauses (human gates as rows) ------------------------------------------

    def open_pause(self, task_id: str, ptype: str, request: dict, run_id: str = "") -> int:
        """Insert a waiting pause and register its wake event.

        ``run_id`` comes from the executing chain (published through the
        ``on_run`` hook); the pause attaches to the currently open phase so the
        trace shows which step parked.
        """
        phase_id = None
        if run_id:
            row = self.conn.execute(
                "SELECT id FROM phases WHERE run_id = ? ORDER BY seq DESC LIMIT 1", (run_id,)
            ).fetchone()
            phase_id = row["id"] if row else None
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO pauses (run_id, phase_id, task_id, type, request, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'waiting', ?)",
                (run_id or None, phase_id, task_id, ptype,
                 json.dumps(request, ensure_ascii=False), _now()),
            )
            self.conn.commit()
            pause_id = cur.lastrowid
            self._events[pause_id] = threading.Event()
        return pause_id

    def wait_pause(self, pause_id: int, timeout_s: float | None = None) -> str:
        evt = self._events.get(pause_id)
        if evt is None:
            return self.pause_status(pause_id)
        evt.wait(timeout_s)
        return self.pause_status(pause_id)

    def pause_status(self, pause_id: int) -> str:
        row = self.conn.execute("SELECT status FROM pauses WHERE id = ?", (pause_id,)).fetchone()
        return row["status"] if row else "expired"

    def pause_row(self, pause_id: int):
        return self.conn.execute("SELECT * FROM pauses WHERE id = ?", (pause_id,)).fetchone()

    def resolve_pause(self, pause_id: int, status: str, answer: str = "") -> None:
        """The owner's decision: flip the row, then wake the waiting worker."""
        if status not in ("answered", "approved", "rejected"):
            raise ValueError(f"bad pause resolution: {status}")
        with self._lock:
            row = self.conn.execute(
                "SELECT status FROM pauses WHERE id = ?", (pause_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown pause {pause_id}")
            if row["status"] != "waiting":
                raise ValueError(f"pause already resolved as {row['status']}")
            self.conn.execute(
                "UPDATE pauses SET status = ?, answer = ?, resolved_at = ? WHERE id = ?",
                (status, answer, _now(), pause_id),
            )
            self.conn.commit()
            evt = self._events.pop(pause_id, None)
        if evt is not None:
            evt.set()

    # -- workers ----------------------------------------------------------------

    def _back_to_backlog(self, task_id: str, note: str) -> None:
        """A card that cannot start goes back to Backlog, unrunnable: the owner
        sees the reason, fixes the cause, and presses review & run. Dispatch
        skips unrunnable cards, so this never hot-loops."""
        with self._lock:
            self.conn.execute(
                "UPDATE tasks SET status = 'backlog', runnable = 0, note = ?, updated_at = ?"
                " WHERE id = ?",
                (note, _now(), task_id),
            )
            self.conn.commit()

    def _worker(self, task_id: str) -> None:
        try:
            self._execute(task_id)
        except Exception as exc:
            self._set_task(task_id, note=f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._workers.pop(task_id, None)
                self._worker_repos.pop(task_id, None)
            self._wake.set()

    def _test_command(self, repo_dir: Path) -> str:
        for r in self.repos:
            if r["path"] == repo_dir:
                return r["test_command"]
        return self.cfg["defaults"].get("test_command") or ""

    def _preflight(self, repo_dir: Path) -> str:
        """'' when the repo is ready for a branch-per-task run; the problem otherwise."""
        if not repo_dir.is_dir():
            return f"repo path does not exist: {repo_dir}"
        if _git("rev-parse", "--git-dir", repo_dir=repo_dir).returncode != 0:
            return f"not a git repository: {repo_dir}"
        status = _git("status", "--porcelain", repo_dir=repo_dir)
        if status.returncode != 0:
            return f"git status failed: {(status.stderr or '').strip()[:200]}"
        if status.stdout.strip():
            return "working tree not clean — the writes boundary needs a clean baseline"
        if _git("remote", "get-url", "origin", repo_dir=repo_dir).returncode != 0:
            return "no origin remote — PR delivery pushes to origin"
        if shutil.which("gh") is None:
            return "gh CLI not found on PATH — PR delivery needs it"
        return ""

    def _restore_worktree(self, repo_dir: Path, exempt_paths: set[str]) -> str:
        """A failed or declined run leaves its uncommitted edits behind — the
        board put them there, so the board removes them (deterministic git, the
        same writes boundary the gates enforce): tracked files reset to the task
        branch tip, untracked debris cleaned. The trace db (exempt paths) never
        counts as debris. Called ONLY from _execute's failure paths, where the
        board dirtied the tree itself and a run never starts on a dirty tree
        (preflight) — never from reap_orphans, where post-crash dirt is
        unattributable and could be the owner's WIP. Returns '' or why the
        restore failed.
        """
        reset = _git("reset", "--hard", "-q", repo_dir=repo_dir)
        clean_args = ["clean", "-fd"]
        for path in sorted(exempt_paths):
            clean_args += ["-e", path]
        clean = _git(*clean_args, repo_dir=repo_dir)
        problems = []
        if reset.returncode != 0:
            problems.append(f"git reset --hard failed: {(reset.stderr or '').strip()[:200]}")
        if clean.returncode != 0:
            problems.append(f"git clean failed: {(clean.stderr or '').strip()[:200]}")
        return "; ".join(f"worktree restore failed: {p}" for p in problems)

    def _default_branch(self, repo_dir: Path) -> str:
        """The branch task branches start from: origin's HEAD when the clone
        knows it, else main/master. PLAN.md Phase 8: branch per task off the
        default branch."""
        res = _git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", repo_dir=repo_dir)
        if res.returncode == 0:
            return res.stdout.strip().removeprefix("origin/")
        for name in ("main", "master"):
            if _git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", repo_dir=repo_dir).returncode == 0:
                return name
        return "main"

    def _execute(self, task_id: str) -> None:
        row = self._task(task_id)
        if row is None:
            return
        repo_dir = Path(row["repo"])
        test_command = self._test_command(repo_dir)
        if not test_command.strip():
            self._back_to_backlog(
                task_id, "can't start: no test command — set repos.<name>.test_command or defaults.test_command",
            )
            return
        problem = self._preflight(repo_dir)
        if problem:
            self._back_to_backlog(task_id, f"can't start: {problem}")
            return

        branch = f"factory/{task_id}"
        # Branch per task off the REMOTE default branch (PLAN.md Phase 8) — never
        # off whatever HEAD is (the previous task's branch after a run) and never
        # off a stale local default: fetch first, then cut from origin/<default>.
        fetch = _git("fetch", "--quiet", "origin", repo_dir=repo_dir)
        if fetch.returncode != 0:
            self._set_task(task_id, note=f"git fetch failed: {(fetch.stderr or '').strip()[:200]}")
            return
        # Best effort: refresh origin/HEAD so the default branch name is the
        # remote's own answer, not a guess. A failure here leaves the previous
        # pointer (or the conventional fallback) in place.
        _git("remote", "set-head", "origin", "--auto", repo_dir=repo_dir)
        default = self._default_branch(repo_dir)
        # -B (not -b): a rerun's stale branch is reset in place — even when it is
        # the checked-out one, where plain deletion would refuse.
        res = _git("checkout", "-B", branch, f"origin/{default}", repo_dir=repo_dir)
        if res.returncode != 0:
            self._set_task(
                task_id,
                note=f"branch creation from origin/{default} failed: {(res.stderr or '').strip()[:200]}",
            )
            return
        self._set_task(task_id, branch=branch)

        # defaults.review: on|off (default on) — the code-review gate that runs
        # before the commit phase. Call sites name agents (roster rule): the
        # lenses and scorer resolve here, the builder doubles as fixer inside the
        # chain. A missing agent raises ConfigError, which the worker records on
        # the card — honest fail-fast, not a silent skip.
        review_kwargs: dict = {}
        if bool(self.cfg["defaults"].get("review", True)):
            review_kwargs = {
                "review": True,
                "reviewer_cfgs": [resolve_agent(self.cfg, n) for n in DEFAULT_REVIEW_LENSES],
                "scorer_cfg": resolve_agent(self.cfg, "scorer"),
                "review_base": f"origin/{default}",  # the task branch diff vs origin default
            }

        holder: dict = {}

        def on_run(run) -> None:
            holder["run_id"] = run.id
            self._set_task(task_id, latest_run=run.id)

        def ask_questions(questions: list[str]) -> str:
            pause_id = self.open_pause(task_id, "questions", {"questions": questions},
                                       run_id=holder.get("run_id", ""))
            status = self.wait_pause(pause_id)
            if status != "answered":
                raise CheckpointDeclined(f"questions pause {status} — the run stops here")
            return self.pause_row(pause_id)["answer"]

        def approve_plan(plan_rel: str, plan_text: str) -> str | None:
            pause_id = self.open_pause(
                task_id, "plan_approval", {"plan_path": plan_rel, "plan": plan_text},
                run_id=holder.get("run_id", ""),
            )
            status = self.wait_pause(pause_id)
            if status == "approved":
                return None
            if status == "answered":
                return self.pause_row(pause_id)["answer"]
            raise CheckpointDeclined(f"plan approval {status} — the run stops here")

        def approve_commit(pending: str) -> bool:
            pause_id = self.open_pause(task_id, "pr_approval", {"pending": pending},
                                       run_id=holder.get("run_id", ""))
            return self.wait_pause(pause_id) == "approved"

        def deliver(run_id: str, message: str) -> dict:
            info = self._deliver(repo_dir, branch, row["title"], row["prompt"], run_id)
            holder["pr_url"] = info.get("pr_url", "")
            return info

        exempt = trace_exemptions(repo_dir, self.db_path)
        try:
            accepted, reason = self.chain_fn(
                self.conn,
                row["prompt"] or row["title"],
                resolve_agent(self.cfg, "planner"),
                resolve_agent(self.cfg, "builder"),
                test_command=test_command,
                repo_dir=repo_dir,
                checkpoint_fn=approve_commit,
                questions_fn=ask_questions,
                plan_approval_fn=approve_plan,
                deliver_fn=deliver,
                on_run=on_run,
                launched_by="board",  # the startup reaper settles only runs the board launched
                exempt_paths=exempt,
                env_dir=self.config_dir,
                timeout_s=int(self.cfg["defaults"].get("agent_timeout_s", 300)),
                **review_kwargs,
            )
        except Exception as exc:
            # The run failed mid-chain; its uncommitted edits stay in the tree
            # until the owner decides below — never destroyed on the board's
            # own authority.
            accepted, reason = False, f"{type(exc).__name__}: {exc}"
        finally:
            # Discovered findings become cards on every ending: a chain that
            # raises (declined pause, failed push) still left its envelopes in
            # the trace, and the Phase 8 contract drops none of them silently.
            try:
                self._spawn_discovered_cards(holder.get("run_id", ""), repo_dir)
            except Exception as exc:  # cards must never mask the run's own failure
                print(f"task {task_id}: discovered-card spawn failed: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
        if accepted:
            self._set_task(task_id, status="in_pr", pr_url=holder.get("pr_url", ""), note="")
        else:
            self._set_task(task_id, note=self._settle_failed(
                task_id, repo_dir, exempt, holder.get("run_id", ""), reason
            ))

    def _settle_failed(self, task_id: str, repo_dir: Path, exempt_paths: set[str],
                       run_id: str, reason: str) -> str:
        """A failed or declined run may hold uncommitted edits. They are never
        destroyed on the board's own authority: with anything pending, the card
        parks on a cleanup pause showing the pending changes — approved restores
        the tree (reset + clean), rejected keeps every edit for the owner to
        dispose of. A clean tree settles without asking."""
        dirty = _git("status", "--porcelain", repo_dir=repo_dir).stdout.strip()
        status = "approved"
        if dirty:
            pending = _pending_changes_summary(repo_dir, exempt_paths=exempt_paths)
            pause_id = self.open_pause(task_id, "cleanup_approval", {"pending": pending},
                                       run_id=run_id)
            status = self.wait_pause(pause_id)
        if not dirty or status != "approved":
            kept = reason or "run failed"
            if dirty:
                return f"{kept}; worktree left untouched at your request — dispose by hand"
            return kept
        problem = self._restore_worktree(repo_dir, exempt_paths)
        note = reason or "run failed"
        if problem:
            note += f"; worktree restore failed: {problem}"
        return note

    def _deliver(self, repo_dir: Path, branch: str, title: str, prompt: str, run_id: str) -> dict:
        """Branch per task ends here: push it and open the PR. The deterministic
        delivery step (Phase 8) — like every git move in the factory, code owns it."""
        # --force-with-lease: a rerun resets the task branch (-B) after a partial
        # delivery (push OK, gh failed), so the remote copy of the previous
        # attempt carries divergent history and a plain push would fail forever.
        # The ref is board-owned by contract; the lease still refuses to clobber
        # a push nobody told us about.
        push = _git("push", "--force-with-lease", "-u", "origin", branch, repo_dir=repo_dir)
        if push.returncode != 0:
            raise RuntimeError(
                f"push failed: {((push.stderr or '') + (push.stdout or '')).strip()[:300]}"
            )
        body = (
            f"Task delivered by the factory board (run `{run_id}`).\n\n"
            f"## Request\n{title}\n\n## Prompt\n{prompt}\n"
        )
        res = subprocess.run(
            ["gh", "pr", "create", "--title", title, "--body", body, "--head", branch],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=120, check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"gh pr create failed: {((res.stderr or '') + (res.stdout or '')).strip()[:300]}"
            )
        pr_url = ""
        for line in reversed((res.stdout or "").splitlines()):
            if line.startswith("http"):
                pr_url = line.strip()
                break
        return {"pr_url": pr_url, "branch": branch}

    def _spawn_discovered_cards(self, run_id: str, repo_dir: Path) -> None:
        """Envelope ``discovered`` findings become Backlog cards with provenance."""
        for item in collect_discovered(self.conn, run_id):
            dup = self.conn.execute(
                "SELECT id FROM tasks WHERE repo = ? AND origin = 'system' AND title = ?"
                " AND status != 'archived'",
                (str(repo_dir), item["title"]),
            ).fetchone()
            if dup is not None:
                continue
            self.create_task(
                title=item["title"],
                prompt=item["detail"],
                repo=str(repo_dir),
                origin="system",
                runnable=False,
                origin_run=run_id,
            )

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        self.reap_orphans()
        self._stop.clear()
        dispatcher = threading.Thread(target=self._dispatch_loop, daemon=True, name="dispatch")
        poller = threading.Thread(target=self._poll_prs_loop, daemon=True, name="pr-poll")
        self._threads = [dispatcher, poller]
        dispatcher.start()
        poller.start()
        self._wake.set()

    def stop(self) -> None:
        """Expire open pauses (workers unwind, their runs settle failed), then stop loops."""
        self._stop.set()
        self._wake.set()
        with self._lock:
            waiting = self.conn.execute(
                "SELECT id FROM pauses WHERE status = 'waiting'"
            ).fetchall()
            wake = []
            for r in waiting:
                self.conn.execute(
                    "UPDATE pauses SET status = 'expired', resolved_at = ? WHERE id = ?",
                    (_now(), r["id"]),
                )
                evt = self._events.pop(r["id"], None)
                if evt is not None:
                    wake.append(evt)
            self.conn.commit()
        for evt in wake:
            evt.set()
        # Any worker still blocked inside an agent invocation dies here: no
        # orphaned pi children burning tokens after the board is gone, and each
        # chain unwinds to a settled (failed) run row.
        kill_live_agents()
        # Wait for the workers to finish unwinding BEFORE the caller closes the
        # shared connection — their settlement writes (run.finish, task notes,
        # worktree restore) must not race a closed db. Killed agents return
        # promptly; a wedged worker is left to the startup reaper.
        for t in list(self._workers.values()):
            t.join(timeout=15)
        for t in self._threads:
            t.join(timeout=5)

    def reap_orphans(self) -> None:
        """Settle traces a dead daemon left behind — the trace stays the source of truth.

        Only runs this board launched are ours to settle (``launched_by = 'board'``):
        the trace db is shared, and CLI commands (``factory run``, ``build-test``,
        ``plan-build-test``) record their own ``running`` sessions that are still live
        while the board starts — reaping those would fail a working CLI run and reset
        the repo it is writing to. Orphans settle failed, their open phases fail,
        their waiting pauses expire, and their cards get the rerun flag. Cards claimed
        but never bound to a run (the daemon died between ``_dispatch`` and ``on_run``)
        get the flag too — no session matches them. Called at startup, when by
        definition no worker of this daemon is alive yet.
        """
        with self._lock:
            orphans = [
                r["id"] for r in
                self.conn.execute(
                    "SELECT id FROM sessions WHERE status = 'running' AND launched_by = 'board'"
                ).fetchall()
            ]
            for run_id in orphans:
                now = _now()
                self.conn.execute(
                    "UPDATE sessions SET status = 'failed', ended_at = ? WHERE id = ?",
                    (now, run_id),
                )
                self.conn.execute(
                    "UPDATE phases SET status = 'fail', ended_at = ?,"
                    " error = 'daemon restarted mid-run' WHERE run_id = ? AND ended_at IS NULL",
                    (now, run_id),
                )
                self.conn.execute(
                    "INSERT INTO events (run_id, phase_id, type, payload, created_at)"
                    " VALUES (?, NULL, 'reaped', ?, ?)",
                    (run_id, json.dumps({"reason": "daemon restarted mid-run"}), now),
                )
                self.conn.execute(
                    "UPDATE pauses SET status = 'expired', resolved_at = ?"
                    " WHERE run_id = ? AND status = 'waiting'",
                    (now, run_id),
                )
            if orphans:
                marks = ",".join("?" for _ in orphans)
                self.conn.execute(
                    "UPDATE tasks SET note = 'daemon restarted mid-run — rerun it', updated_at = ?"
                    f" WHERE status = 'working' AND latest_run IN ({marks})",
                    (_now(), *orphans),
                )
            # Claimed but never bound to a run: no session matches, so the orphan
            # loop above cannot flag these cards. Only an empty note qualifies —
            # an honest failure reason recorded earlier is never overwritten.
            self.conn.execute(
                "UPDATE tasks SET note = 'daemon restarted mid-run — rerun it', updated_at = ?"
                " WHERE status = 'working' AND latest_run = '' AND note = ''",
                (_now(),),
            )
            self.conn.commit()
        # Deliberately NO worktree restore for orphaned runs: after a daemon
        # death the dirt in a roster repo is unattributable — the dead run's
        # debris and the owner's uncommitted edits look identical. Restoring
        # would silently destroy owner WIP; instead the cards tell the owner to
        # review the tree, and the rerun's preflight refuses until it is clean.
        # Only the board's OWN failed/declined runs auto-restore (in _execute),
        # because there the board dirtied the tree itself.

    # -- PR merge polling ---------------------------------------------------------

    def _pr_state(self, pr_url: str, repo_dir: Path) -> str:
        res = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "state", "-q", ".state"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=30, check=False,
        )
        return res.stdout.strip() if res.returncode == 0 else ""

    def _poll_prs_once(self) -> None:
        rows = self.conn.execute(
            "SELECT id, pr_url, repo FROM tasks WHERE status = 'in_pr' AND pr_url != ''"
        ).fetchall()
        for row in rows:
            if self._stop.is_set():
                return
            state = self._pr_state(row["pr_url"], Path(row["repo"]))
            if state == "MERGED":
                self._set_task(row["id"], status="archived", note="PR merged")

    def _poll_prs_loop(self) -> None:
        while not self._stop.wait(timeout=60):
            try:
                self._poll_prs_once()
            except Exception as exc:  # gh hiccups must not kill the poller
                print(f"pr poll error: {type(exc).__name__}: {exc}", file=sys.stderr)

    # -- board snapshot -------------------------------------------------------

    def board_snapshot(self) -> dict:
        """Everything the kanban page renders, computed once per request.

        The full page and the polled fragment (/fragment/board) both render
        from this dict, so the two views cannot disagree. ``phase`` carries a
        live run's open phase row (``ended_at IS NULL``) as a plain-English
        label plus a unix epoch, so the card can tick elapsed time client-side
        without asking the server again. A card with an open pause shows the
        pause instead of the phase: the chain is still streaming but parked on
        the owner, and that is what the card must say.
        """
        conn = ui_connect(self.db_path)
        try:
            backlog = conn.execute(
                "SELECT * FROM tasks WHERE status = 'backlog' ORDER BY created_at, rowid"
            ).fetchall()
            working = conn.execute(
                "SELECT * FROM tasks WHERE status = 'working' ORDER BY updated_at DESC"
            ).fetchall()
            in_pr = conn.execute(
                "SELECT * FROM tasks WHERE status = 'in_pr' ORDER BY updated_at DESC"
            ).fetchall()
            archived = conn.execute(
                "SELECT * FROM tasks WHERE status = 'archived' ORDER BY updated_at DESC LIMIT ?",
                (ARCHIVED_LIMIT,),
            ).fetchall()
            archived_total = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE status = 'archived'"
            ).fetchone()["n"]
            waiting = {
                r["task_id"]: r
                for r in conn.execute("SELECT * FROM pauses WHERE status = 'waiting'").fetchall()
            }
            run_ids = [r["latest_run"] for r in working if r["latest_run"]]
            settled: dict[str, str] = {}
            open_phases: dict[str, object] = {}
            if run_ids:
                marks = ",".join("?" for _ in run_ids)
                settled = {
                    r["id"]: r["status"]
                    for r in conn.execute(
                        f"SELECT id, status FROM sessions WHERE id IN ({marks})", run_ids
                    ).fetchall()
                }
                live_ids = [rid for rid in run_ids if settled.get(rid) == "running"]
                if live_ids:
                    marks = ",".join("?" for _ in live_ids)
                    for r in conn.execute(
                        "SELECT run_id, name, started_at FROM phases"
                        f" WHERE ended_at IS NULL AND run_id IN ({marks}) ORDER BY seq",
                        live_ids,
                    ).fetchall():
                        open_phases[r["run_id"]] = r  # highest seq wins: the current phase
        finally:
            conn.close()

        now = int(time.time())
        with self._lock:
            live_workers = set(self._workers)

        positions: dict[str, int] = {}
        per_repo: dict[str, int] = {}
        for r in backlog:
            if r["runnable"]:
                per_repo[r["repo"]] = per_repo.get(r["repo"], 0) + 1
                positions[r["id"]] = per_repo[r["repo"]]

        def card_dict(r) -> dict:
            phase = None
            run_status = ""
            if r["latest_run"]:
                status = settled.get(r["latest_run"])
                if status == "running":
                    run_status = "running"
                    row = open_phases.get(r["latest_run"])
                    if row is not None:
                        epoch = started_epoch(row["started_at"])
                        phase = {
                            "name": row["name"],
                            "label": phase_label(row["name"]),
                            "started_epoch": epoch,
                            "elapsed_s": max(0, now - epoch),
                        }
                elif status:
                    # settled: failed, or accepted-but-not-delivered (declined)
                    run_status = status
            pause = waiting.get(r["id"])
            return {
                "id": r["id"],
                "title": r["title"],
                "repo": r["repo"],
                "repo_name": Path(r["repo"]).name,
                "status": r["status"],
                "runnable": r["runnable"],
                "note": r["note"],
                "branch": r["branch"],
                "pr_url": r["pr_url"],
                "origin": r["origin"],
                "origin_run": r["origin_run"],
                "latest_run": r["latest_run"],
                "updated_at": r["updated_at"],
                "position": positions.get(r["id"], 0),
                # A claimed card with no bound run (pre-run failure, or a dead
                # daemon's claim) still offers the rerun escape — unless a
                # worker owns it now.
                "rerun": r["status"] == "working" and r["id"] not in live_workers,
                "run_status": run_status,
                "phase": phase,
                "pause": (
                    {"id": pause["id"], "type": pause["type"], "request": pause["request"]}
                    if pause is not None
                    else None
                ),
            }

        return {
            "backlog": [card_dict(r) for r in backlog],
            "working": [card_dict(r) for r in working],
            "in_pr": [card_dict(r) for r in in_pr],
            "archived": [card_dict(r) for r in archived],
            "archived_total": archived_total,
            "live": bool(working) or bool(waiting),
        }


# --- http server -------------------------------------------------------------


class BoardHTTPError(ValueError):
    """A POST the board refuses: the message becomes a 400 page."""


def make_board_handler(board: Board) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            try:
                if path == "/":
                    self._send(kanban.render_board(board), 200)
                elif path == "/runs":
                    self._send(self._trace(runs.render_index), 200)
                elif path.startswith("/static/"):
                    name = path.removeprefix("/static/").strip("/")
                    asset = static_asset(name)
                    if asset is None:
                        self._send(_not_found(path), 404)
                    else:
                        self._send_bytes(*asset)
                elif path == "/fragment/board":
                    self._send(kanban.board_fragment(board.board_snapshot()), 200)
                elif path == "/fragment/runs":
                    self._send(self._trace(runs.render_runs_fragment), 200)
                elif path.startswith("/fragment/run/"):
                    run_id = path.removeprefix("/fragment/run/").strip("/")
                    body, code = (
                        self._trace(lambda c: runs.render_run_fragment(c, run_id))
                        if run_id
                        else (_not_found(path), 404)
                    )
                    self._send(body, code)
                elif path.startswith("/run/"):
                    run_id = path.removeprefix("/run/").strip("/")
                    body, code = (
                        self._trace(lambda c: runs.render_run(c, run_id))
                        if run_id
                        else (_not_found(path), 404)
                    )
                    self._send(body, code)
                elif path.startswith("/pause/"):
                    body, code = kanban.render_pause(board, path.removeprefix("/pause/").strip("/"))
                    self._send(body, code)
                else:
                    self._send(_not_found(path), 404)
            except Exception as exc:
                # A rendering bug gets its own page, not a dropped connection.
                self._send(page("render error", f"<p>{esc(type(exc).__name__)}: {esc(exc)}</p>"), 500)

        def _trace(self, fn):
            """Run a renderer that wants a read-only trace connection."""
            conn = ui_connect(board.db_path)
            try:
                return fn(conn)
            finally:
                conn.close()

        def _same_origin(self) -> bool:
            """CSRF guard for every mutating endpoint.

            Browsers attach the true Origin to every form POST (and Referer when
            allowed), so a header whose host differs from the request's own Host
            is cross-site by definition and is refused before any state changes.
            The board has no cookies or tokens to forge and pause ids are small
            sequential integers — without this check any website the owner browses
            could approve a push the owner never saw. Non-browser clients (curl,
            scripts, tests) send neither header and pass.
            """
            host = self.headers.get("Host", "")
            for header in ("Origin", "Referer"):
                value = self.headers.get(header)
                if value and urlparse(value).netloc != host:
                    return False
            return True

        def do_POST(self):
            if not self._same_origin():
                refused = "<p>Cross-site POST refused: the origin does not match the board.</p>"
                self._send(page("refused", refused), 403)
                return
            path = unquote(urlparse(self.path).path)
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length > FORM_CAP:
                    raise BoardHTTPError("form too large")
                form = parse_qs(
                    self.rfile.read(length).decode("utf-8", errors="replace"),
                    keep_blank_values=True,
                )

                def field(name: str) -> str:
                    return form.get(name, [""])[0]

                if path == "/tasks":
                    self._create_task(field)
                elif path.startswith("/tasks/") and path.endswith("/promote"):
                    board.promote_task(
                        self._id(path, "/tasks/", "/promote"),
                        title=field("title"), prompt=field("prompt"),
                    )
                elif path.startswith("/tasks/") and path.endswith("/archive"):
                    board.archive_task(self._id(path, "/tasks/", "/archive"))
                elif path.startswith("/tasks/") and path.endswith("/rerun"):
                    board.rerun_task(self._id(path, "/tasks/", "/rerun"))
                elif path.startswith("/pause/"):
                    self._resolve_pause(path.removeprefix("/pause/").strip("/"), field)
                else:
                    self._send(_not_found(path), 404)
                    return
                self._redirect("/")
            except BoardHTTPError as exc:
                self._send(page("refused", f"<p>{esc(exc)}</p>"), 400)
            except ValueError as exc:  # unknown ids and other bad lookups
                self._send(page("refused", f"<p>{esc(exc)}</p>"), 400)
            except Exception as exc:
                self._send(page("action failed", f"<p>{esc(type(exc).__name__)}: {esc(exc)}</p>"), 500)

        def _create_task(self, field) -> None:
            title = field("title").strip()
            entry = board.repos_by_name.get(field("repo").strip())
            if entry is None:
                raise BoardHTTPError(f"unknown repo {field('repo')!r} — pick one from the list")
            if not title:
                raise BoardHTTPError("a task needs a title")
            board.create_task(title=title, prompt=field("prompt"), repo=str(entry["path"]))

        def _resolve_pause(self, pid_text: str, field) -> None:
            action = field("action")
            answer = field("answer").strip()
            if action == "approve":
                board.resolve_pause(int(pid_text), "approved")
            elif action == "reject":
                board.resolve_pause(int(pid_text), "rejected")
            elif action == "send":
                if not answer:
                    raise BoardHTTPError("an empty answer answers nothing — write it or reject")
                board.resolve_pause(int(pid_text), "answered", answer)
            else:
                raise BoardHTTPError(f"unknown action {action!r}")

        def _id(self, path: str, prefix: str, suffix: str) -> str:
            return path.removeprefix(prefix).removesuffix(suffix).strip("/")

        def _redirect(self, to: str) -> None:
            try:
                self.send_response(303)
                self.send_header("Location", to)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except OSError:
                pass

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


def _not_found(path: str) -> str:
    return page("not found", f"<p>Unknown page <code>{esc(path)}</code>.</p>")


def make_board_server(board: Board, port: int) -> ThreadingHTTPServer:
    """Bind loopback only: the board drives real runs, it never leaves the machine."""
    return ThreadingHTTPServer(("127.0.0.1", port), make_board_handler(board))
