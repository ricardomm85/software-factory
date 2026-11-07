"""CLI entry point for the factory.

Commands:
  factory run "<prompt>"      one agent phase inside a traced run (the spine)
  factory sessions            list traced runs
  factory phases <run_id>     list phases of one run
  factory prompt "<prompt>"   raw one-shot agent call, no trace (debug helper)
"""

import argparse
import sys
from pathlib import Path

from .agent_pi import run_pi
from .config import ConfigError, load_config, resolve_agent
from .db import connect
from .envelope import RunOutput
from .run import AgentPhaseError, Run, call_agent


def _load_config(path: Path) -> dict:
    try:
        return load_config(path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        sys.exit(2)


def _resolve_agent(cfg: dict, name: str | None) -> dict:
    try:
        return resolve_agent(cfg, name)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        sys.exit(2)


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
        duration = ""
        if p["started_at"] and p["ended_at"]:
            duration = p["ended_at"][-8:]
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
    p_run.set_defaults(func=cmd_run)

    p_sessions = sub.add_parser("sessions", help="list traced runs")
    p_sessions.add_argument("--limit", type=int, default=20)
    p_sessions.set_defaults(func=cmd_sessions)

    p_phases = sub.add_parser("phases", help="list phases of one run")
    p_phases.add_argument("run_id")
    p_phases.set_defaults(func=cmd_phases)

    p_prompt = sub.add_parser("prompt", help="raw one-shot agent call, no trace")
    p_prompt.add_argument("prompt")
    p_prompt.add_argument("--agent", default=None)
    p_prompt.add_argument("--mode", choices=["print", "json"], default="print")
    p_prompt.set_defaults(func=cmd_prompt)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
