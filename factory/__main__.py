"""CLI entry point for the factory.

Commands:
  factory run "<prompt>"        one agent phase inside a traced run (the spine)
  factory build-test "<prompt>" builder + code(test) with a bounded fix loop (gates + rollback)
  factory sessions              list traced runs
  factory phases <run_id>       list phases of one run
  factory prompt "<prompt>"     raw one-shot agent call, no trace (debug helper)
"""

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .agent_pi import run_pi
from .chains import run_build_test
from .config import ConfigError, load_config, resolve_agent
from .db import connect
from .envelope import RunOutput
from .run import AgentPhaseError, Run, call_agent


def _load_config(path: Path) -> dict:
    # ConfigError propagates; main() maps it to a clean exit code.
    return load_config(path)


def _resolve_agent(cfg: dict, name: str | None) -> dict:
    return resolve_agent(cfg, name)


def _fmt_duration(started_at, ended_at) -> str:
    """Human duration between two ISO timestamps; '' when either is missing."""
    try:
        t0, t1 = datetime.fromisoformat(started_at), datetime.fromisoformat(ended_at)
        return f"{(t1 - t0).total_seconds():.0f}s"
    except (TypeError, ValueError):
        return ""


def _db_path(cfg: dict, config_path: Path) -> Path:
    data_dir = cfg["defaults"].get("data_dir", "data")
    return config_path.parent / data_dir / "factory.db"


def cmd_run(args) -> int:
    cfg = _load_config(args.config)
    agent = _resolve_agent(cfg, args.agent)
    conn = connect(_db_path(cfg, args.config))
    run = Run.create(conn, request=args.prompt, agent=agent["name"], model=agent["model"])
    print(
        f"run {run.id} started (agent={agent['name']}, model={agent['model']}, "
        f"thinking={agent.get('thinking') or 'default'})"
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
                repo_dir=args.config.parent,
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
    repo_dir = args.config.parent
    test_command = args.test_command or cfg["defaults"].get("test_command") or ""
    if not test_command.strip():
        print(
            "no test command: pass --test-command or set defaults.test_command in the roster "
            "(a placeholder suite is theater)",
            file=sys.stderr,
        )
        return 2
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
        )
    except AgentPhaseError as exc:
        print(f"build-test FAILED: {exc}", file=sys.stderr)
        return 1
    return 0 if accepted else 1


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
        duration = _fmt_duration(p["started_at"], p["ended_at"])
        line = f"  #{p['seq']} {p['name']:10s} kind={p['kind']:5s} owner={p['owner']:8s} {p['status']}"
        if p["error"]:
            line += f"  error={p['error']}"
        print(line + (f"  [{duration}]" if duration else ""))
    return 0


def cmd_prompt(args) -> int:
    cfg = _load_config(args.config)
    agent = _resolve_agent(cfg, args.agent)
    result = run_pi(
        args.prompt,
        model=agent["model"],
        thinking=agent.get("thinking"),
        mode=args.mode,
        repo_dir=args.config.parent,
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
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="one agent phase inside a traced run")
    p_run.add_argument("prompt", help="Task prompt for the agent")
    p_run.add_argument("--agent", default=None, help="Agent name from the roster")
    p_run.add_argument("--max-attempts", type=int, default=3, help="Envelope retries in the same session")
    p_run.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="Roster config path")
    p_run.set_defaults(func=cmd_run)

    p_bt = sub.add_parser("build-test", help="builder + code(test) with a bounded fix loop")
    p_bt.add_argument("prompt", help="What the builder should implement")
    p_bt.add_argument("--agent", default="builder", help="Builder agent from the roster")
    p_bt.add_argument("--test-command", default=None, help="Override the roster's test_command")
    p_bt.add_argument("--max-rounds", type=int, default=3, help="Bounded fix rounds")
    p_bt.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="Roster config path")
    p_bt.set_defaults(func=cmd_build_test)

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
