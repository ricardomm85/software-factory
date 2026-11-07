"""CLI entry point: ``factory "prompt"`` spawns the default agent headless.

Phase 0 scope: one agent, one prompt, output captured. Phases, envelopes, and
the trace land in Phase 1 (see PLAN.md §5).
"""

import argparse
import sys
from pathlib import Path

from .agent_pi import run_pi
from .config import ConfigError, load_config, resolve_agent

DEFAULT_CONFIG = Path("config.yaml")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="factory",
        description="Personal software factory orchestrator (Phase 0: spawn one agent).",
    )
    parser.add_argument("prompt", help="Task prompt for the agent")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Roster config path")
    parser.add_argument("--agent", default=None, help="Agent name from the roster (default: roster defaults)")
    parser.add_argument(
        "--mode",
        choices=["print", "json"],
        default="print",
        help="print = final text; json = JSONL event stream",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
        agent = resolve_agent(cfg, args.agent)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

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


if __name__ == "__main__":
    sys.exit(main())
