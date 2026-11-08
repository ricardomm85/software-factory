"""CLI entry point for the factory.

Commands:
  factory run "<prompt>"        one agent phase inside a traced run (the spine)
  factory build-test "<prompt>" builder + code(test) with a bounded fix loop (gates + rollback)
  factory build-review          multi-lens review of the working-tree changes (gates + fix rounds)
  factory ui                    read-only local web UI over the trace (runs, run timeline)
  factory serve                 the board: create tasks, watch runs, answer pauses (loopback)
  factory sessions              list traced runs
  factory phases <run_id>       list phases of one run
  factory prompt "<prompt>"     raw one-shot agent call, no trace (debug helper)
"""

import argparse
import subprocess
import sys
from pathlib import Path

from .agent_pi import run_pi
from .chains import run_build_review, run_build_test, run_plan_build_test, _stdin_checkpoint
from .config import ConfigError, load_config, resolve_agent
from .db import connect, trace_exemptions as _trace_exemptions
from .envelope import RunOutput
from .run import AgentPhaseError, Run, call_agent
from .web.runs import fmt_duration, make_trace_server


def _load_config(path: Path) -> dict:
    # ConfigError propagates; main() maps it to a clean exit code.
    return load_config(path)


def _resolve_agent(cfg: dict, name: str | None) -> dict:
    return resolve_agent(cfg, name)


def _db_path(cfg: dict, config_path: Path) -> Path:
    data_dir = cfg["defaults"].get("data_dir", "data")
    return config_path.parent / data_dir / "factory.db"


def _resolve_repo(args) -> tuple[Path, Path]:
    """(repo_dir, config_dir): the repo agents work on, and where roster+secrets live.

    They are the same place by default; ``--repo`` points the work at another
    repository while the roster, ``.env`` and the trace stay at home.
    """
    config_dir = args.config.parent
    repo_arg = getattr(args, "repo", None)
    # Always resolved: home runs and --repo runs must record comparable values
    # in the trace's repo column, never "." vs absolute paths.
    repo_dir = (Path(repo_arg) if repo_arg else config_dir).resolve()
    return repo_dir, config_dir


def cmd_run(args) -> int:
    cfg = _load_config(args.config)
    agent = _resolve_agent(cfg, args.agent)
    repo_dir, config_dir = _resolve_repo(args)
    conn = connect(_db_path(cfg, args.config))
    run = Run.create(conn, request=args.prompt, agent=agent["name"], model=agent["model"], repo=str(repo_dir))
    print(
        f"run {run.id} started (agent={agent['name']}, model={agent['model']}, "
        f"thinking={agent.get('thinking') or 'default'}, repo={repo_dir})"
    )
    accepted = False
    try:
        with run.phase(name="run", kind="agent", owner=agent["name"]) as phase:
            envelope = call_agent(
                run,
                phase,
                args.prompt,
                agent,
                output_type=RunOutput,
                max_attempts=args.max_attempts,
                repo_dir=repo_dir,
                env_dir=config_dir,
            )
            accepted = envelope.status == "success"
            reason = envelope.summary if envelope.status != "success" else ""
    except AgentPhaseError as exc:
        run.finish(accepted=False, reason=str(exc))
        print(f"run {run.id} FAILED: {exc}", file=sys.stderr)
        return 1
    except BaseException as exc:
        # A killed or crashed run settles its own trace to failed — no orphans.
        run.finish(accepted=False, reason=f"{type(exc).__name__}: {exc}")
        raise
    run.finish(accepted=accepted, reason=reason)
    verdict = "accepted" if accepted else "REJECTED"
    print(f"run {run.id} {verdict} ({agent['name']}: {envelope.summary})")
    return 0 if accepted else 1


def cmd_build_test(args) -> int:
    cfg = _load_config(args.config)
    agent = _resolve_agent(cfg, args.agent)
    repo_dir, config_dir = _resolve_repo(args)
    test_command = args.test_command or cfg["defaults"].get("test_command") or ""
    if not test_command.strip():
        print(
            "no test command: pass --test-command or set defaults.test_command in the roster "
            "(a placeholder suite is theater)",
            file=sys.stderr,
        )
        return 2
    rc = _require_clean_tree(repo_dir)
    if rc:
        return rc

    conn = connect(_db_path(cfg, args.config))
    accepted = False
    try:
        accepted = run_build_test(
            conn,
            args.prompt,
            agent,
            test_command=test_command,
            repo_dir=repo_dir,
            max_rounds=args.max_rounds,
            exempt_paths=_trace_exemptions(repo_dir, _db_path(cfg, args.config)),
            env_dir=config_dir,
        )
    except AgentPhaseError as exc:
        print(f"build-test FAILED: {exc}", file=sys.stderr)
        return 1
    return 0 if accepted else 1


def cmd_plan_build_test(args) -> int:
    cfg = _load_config(args.config)
    planner = _resolve_agent(cfg, args.planner)
    builder = _resolve_agent(cfg, args.builder)
    repo_dir, config_dir = _resolve_repo(args)
    test_command = args.test_command or cfg["defaults"].get("test_command") or ""
    if not test_command.strip():
        print(
            "no test command: pass --test-command or set defaults.test_command in the roster "
            "(a placeholder suite is theater)",
            file=sys.stderr,
        )
        return 2
    ask = args.checkpoint if args.checkpoint is not None else bool(cfg["defaults"].get("checkpoint", False))
    checkpoint_fn = _stdin_checkpoint if ask else None
    rc = _require_clean_tree(repo_dir)
    if rc:
        return rc

    conn = connect(_db_path(cfg, args.config))
    try:
        accepted, reason = run_plan_build_test(
            conn,
            args.prompt,
            planner,
            builder,
            test_command=test_command,
            repo_dir=repo_dir,
            max_rounds=args.max_rounds,
            checkpoint_fn=checkpoint_fn,
            timeout_s=int(
                getattr(args, "agent_timeout", None)
                or cfg["defaults"].get("agent_timeout_s", 300)
            ),
            exempt_paths=_trace_exemptions(repo_dir, _db_path(cfg, args.config)),
            env_dir=config_dir,
        )
    except (AgentPhaseError, RuntimeError) as exc:
        print(f"plan-build-test FAILED: {exc}", file=sys.stderr)
        return 1
    if not accepted and reason:
        print(f"plan-build-test rejected: {reason}", file=sys.stderr)
    return 0 if accepted else 1


def _require_clean_tree(repo_dir: Path) -> int:
    """The writes boundary rolls back against a clean baseline — refuse otherwise."""
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo_dir), capture_output=True, text=True
    )
    if dirty.returncode != 0 or dirty.stdout.strip():
        print(
            "working tree not clean — commit or stash first: the writes boundary "
            "rolls back unclaimed changes against a clean baseline",
            file=sys.stderr,
        )
        return 2
    return 0


def _require_git_repo(repo_dir: Path) -> int:
    """The chains read the repo through git — refuse a directory that is not one."""
    res = subprocess.run(
        ["git", "rev-parse", "--git-dir"], cwd=str(repo_dir), capture_output=True, text=True
    )
    if res.returncode != 0:
        print(f"not a git repository: {repo_dir}", file=sys.stderr)
        return 2
    return 0


def cmd_build_review(args) -> int:
    cfg = _load_config(args.config)
    repo_dir, config_dir = _resolve_repo(args)
    rc = _require_git_repo(repo_dir)
    if rc:
        return rc
    lens_names = [name.strip() for name in args.lenses.split(",") if name.strip()]
    reviewers = [_resolve_agent(cfg, name) for name in lens_names]
    scorer = _resolve_agent(cfg, args.scorer)
    fixer = _resolve_agent(cfg, args.fixer)
    conn = connect(_db_path(cfg, args.config))
    accepted = False
    try:
        accepted, reason = run_build_review(
            conn,
            repo_dir=repo_dir,
            reviewer_cfgs=reviewers,
            scorer_cfg=scorer,
            fixer_cfg=fixer,
            max_rounds=args.max_rounds,
            timeout_s=args.timeout,
            exempt_paths=_trace_exemptions(repo_dir, _db_path(cfg, args.config)),
            env_dir=config_dir,
        )
    except (AgentPhaseError, OSError) as exc:
        # OSError covers a lens whose prompt template file is missing, and
        # untracked-file read errors from the diff collector.
        print(f"build-review FAILED: {exc}", file=sys.stderr)
        return 1
    if reason:
        print(f"build-review: {reason}")
    return 0 if accepted else 1


def cmd_ui(args) -> int:
    cfg = _load_config(args.config)
    db_path = _db_path(cfg, args.config)
    if not 0 < args.port <= 65535:
        print(f"invalid port {args.port}: must be 1-65535", file=sys.stderr)
        return 2
    try:
        server = make_trace_server(db_path, args.port)
    except OSError as exc:
        print(f"cannot serve on port {args.port}: {exc}", file=sys.stderr)
        return 2
    # The banner only prints once the socket is actually bound.
    print(f"factory ui → http://127.0.0.1:{args.port}/  (db: {db_path}) — Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
    return 0


def cmd_serve(args) -> int:
    """The board: a long-lived daemon that owns execution (Phases 6-8)."""
    cfg = _load_config(args.config)
    db_path = _db_path(cfg, args.config)
    config_dir = args.config.parent
    if not 0 < args.port <= 65535:
        print(f"invalid port {args.port}: must be 1-65535", file=sys.stderr)
        return 2

    from .board import Board, make_board_server  # local import: heavy module, board users only

    conn = connect(db_path)
    board = Board(conn, cfg, config_dir=config_dir, db_path=db_path)
    try:
        server = make_board_server(board, args.port)
    except OSError as exc:
        print(f"cannot serve on port {args.port}: {exc}", file=sys.stderr)
        return 2
    board.start()
    repos = ", ".join(r["name"] for r in board.repos) or "(no repos configured — add a repos: section)"
    print(
        f"factory board → http://127.0.0.1:{args.port}/  (db: {db_path})\n"
        f"  repos: {repos}\n"
        f"  max concurrent runs: {board._cap} — Ctrl+C to stop"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping — open pauses expire, runs settle failed in the trace")
    finally:
        board.stop()
        server.server_close()
        conn.close()
    return 0


def cmd_sessions(args) -> int:
    cfg = _load_config(args.config)
    conn = connect(_db_path(cfg, args.config))
    rows = conn.execute(
        "SELECT id, status, agent, model, substr(request, 1, 48) AS request,"
        " started_at, ended_at FROM sessions ORDER BY started_at DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    if not rows:
        print("no runs traced yet")
        return 0
    for r in rows:
        print(f"{r['id']}  {r['status']:8s}  {r['agent'] or '-':8s}  {r['request']}  [{r['started_at']}]")
    return 0


def cmd_phases(args) -> int:
    cfg = _load_config(args.config)
    conn = connect(_db_path(cfg, args.config))
    run = Run.get(conn, args.run_id)
    print(f"run {run.id}")
    for p in run.phases():
        duration = fmt_duration(p["started_at"], p["ended_at"])
        line = f"  #{p['seq']} {p['name']:10s} kind={p['kind']:5s} owner={p['owner']:8s} {p['status']}"
        if p["error"]:
            line += f"  error={p['error']}"
        print(line + (f"  [{duration}]" if duration else ""))
    return 0


def cmd_prompt(args) -> int:
    cfg = _load_config(args.config)
    agent = _resolve_agent(cfg, args.agent)
    repo_dir, config_dir = _resolve_repo(args)
    result = run_pi(
        args.prompt,
        model=agent["model"],
        thinking=agent.get("thinking"),
        mode=args.mode,
        repo_dir=repo_dir,
        env_dir=config_dir,
    )
    if args.mode == "json":
        sys.stdout.write(result.stdout)
    else:
        sys.stdout.write(result.stdout if result.stdout.endswith("\n") else result.stdout + "\n")
    if result.timed_out:
        print(f"agent timed out after {result.duration_ms} ms", file=sys.stderr)
        return 124
    if result.exit_code != 0:
        sys.stderr.write(result.stderr)
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="factory", description="Personal software factory orchestrator."
    )
    parser.add_argument("--config", type=Path, default=Path("config.yaml"), help="Roster config path")
    parser.add_argument(
        "--repo", type=Path, default=None,
        help="Target repository the agents work on (default: the roster's own directory)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="one agent phase inside a traced run")
    p_run.add_argument("prompt", help="Task prompt for the agent")
    p_run.add_argument("--agent", default=None, help="Agent name from the roster")
    p_run.add_argument("--max-attempts", type=int, default=3, help="Envelope retries in the same session")
    p_run.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="Roster config path")
    p_run.add_argument("--repo", type=Path, default=argparse.SUPPRESS, help="Target repository")
    p_run.set_defaults(func=cmd_run)

    p_bt = sub.add_parser("build-test", help="builder + code(test) with a bounded fix loop")
    p_bt.add_argument("prompt", help="What the builder should implement")
    p_bt.add_argument("--agent", default="builder", help="Builder agent from the roster")
    p_bt.add_argument("--test-command", default=None, help="Override the roster's test_command")
    p_bt.add_argument("--max-rounds", type=int, default=3, help="Bounded fix rounds")
    p_bt.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="Roster config path")
    p_bt.add_argument("--repo", type=Path, default=argparse.SUPPRESS, help="Target repository")
    p_bt.set_defaults(func=cmd_build_test)

    p_pbt = sub.add_parser(
        "plan-build-test",
        help="planner → builder + code(test) → code commit (the standard chain)",
    )
    p_pbt.add_argument("prompt", help="What to plan and implement")
    p_pbt.add_argument("--planner", default="planner", help="Planner agent from the roster")
    p_pbt.add_argument("--builder", default="builder", help="Builder agent from the roster")
    p_pbt.add_argument("--test-command", default=None, help="Override the roster's test_command")
    p_pbt.add_argument("--max-rounds", type=int, default=3, help="Bounded fix rounds")
    p_pbt.add_argument(
        "--agent-timeout",
        type=int,
        default=None,
        help="Seconds each agent call may run (default: defaults.agent_timeout_s, else 300)",
    )
    p_pbt.add_argument(
        "--checkpoint",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Ask before the final commit lands (default: defaults.checkpoint in the roster)",
    )
    p_pbt.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="Roster config path")
    p_pbt.add_argument("--repo", type=Path, default=argparse.SUPPRESS, help="Target repository")
    p_pbt.set_defaults(func=cmd_plan_build_test)

    p_br = sub.add_parser(
        "build-review",
        help="multi-lens review of the working-tree changes: reviewers → scorer → gate (>=50 blocks) → fix rounds",
    )
    p_br.add_argument(
        "--lenses",
        default="review-conventions,review-bugs",
        help="Comma-separated reviewer agent names from the roster",
    )
    p_br.add_argument("--scorer", default="scorer", help="Scorer agent from the roster")
    p_br.add_argument("--fixer", default="builder", help="Agent that applies fixes")
    p_br.add_argument("--max-rounds", type=int, default=2, help="Bounded review→fix rounds")
    p_br.add_argument("--timeout", type=int, default=600, help="Per-invocation agent timeout in seconds")
    p_br.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="Roster config path")
    p_br.add_argument("--repo", type=Path, default=argparse.SUPPRESS, help="Target repository")
    p_br.set_defaults(func=cmd_build_review)

    p_ui = sub.add_parser(
        "ui",
        help="read-only local web UI over the trace (binds 127.0.0.1 only)",
    )
    p_ui.add_argument("--port", type=int, default=8666, help="Local port to serve on")
    p_ui.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="Roster config path")
    p_ui.set_defaults(func=cmd_ui)

    p_serve = sub.add_parser(
        "serve",
        help="the task board: create tasks, watch runs, answer pauses (binds 127.0.0.1 only)",
    )
    p_serve.add_argument("--port", type=int, default=8666, help="Local port to serve on")
    p_serve.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="Roster config path")
    p_serve.set_defaults(func=cmd_serve)

    p_sessions = sub.add_parser("sessions", help="list traced runs")
    p_sessions.add_argument("--limit", type=int, default=20)
    p_sessions.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="Roster config path")
    p_sessions.set_defaults(func=cmd_sessions)

    p_phases = sub.add_parser("phases", help="list phases of one run")
    p_phases.add_argument("run_id")
    p_phases.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="Roster config path")
    p_phases.set_defaults(func=cmd_phases)

    p_prompt = sub.add_parser("prompt", help="raw one-shot agent call, no trace")
    p_prompt.add_argument("prompt")
    p_prompt.add_argument("--agent", default=None)
    p_prompt.add_argument("--mode", choices=["print", "json"], default="print")
    p_prompt.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="Roster config path")
    p_prompt.add_argument("--repo", type=Path, default=argparse.SUPPRESS, help="Target repository")
    p_prompt.set_defaults(func=cmd_prompt)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:  # unknown run id and other bad lookups
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
