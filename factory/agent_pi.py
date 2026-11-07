"""Spawn the pi coding agent headless and capture its output.

pi is the v1 coding agent (see PLAN.md §6). Every invocation is a subprocess
with the factory's secrets injected into its environment — the key never
appears in config files or on the command line.
"""

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .env import load_env


@dataclass
class AgentResult:
    """What one agent invocation produced, plus how and how long it ran."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    command: list[str]
    timed_out: bool = False


def build_pi_command(
    prompt: str,
    model: str,
    session_id: str | None = None,
    mode: str = "print",
    thinking: str | None = None,
) -> list[str]:
    """Build the pi argv for one headless invocation.

    mode="print" writes the final assistant text to stdout; mode="json" writes
    typed JSONL events (the feed the Phase 1 tracer will consume).
    """
    if mode == "print":
        cmd = ["pi", "-p"]
    elif mode == "json":
        cmd = ["pi", "--mode", "json"]
    else:
        raise ValueError(f"mode must be 'print' or 'json', got {mode!r}")
    if session_id is not None:
        # create-or-continue: corrections reuse the session, never cold-restart.
        cmd += ["--session-id", session_id]
    if thinking:
        cmd += ["--thinking", thinking]
    cmd += ["--model", model, prompt]
    return cmd


def run_pi(
    prompt: str,
    model: str,
    session_id: str | None = None,
    mode: str = "print",
    thinking: str | None = None,
    timeout_s: int = 300,
    repo_dir: Path | None = None,
) -> AgentResult:
    """Run one pi invocation with factory secrets injected. Never raises on agent failure.

    Secrets come from the repo-local ``.env`` (repo_dir); there is no global fallback.
    """
    cmd = build_pi_command(prompt, model=model, session_id=session_id, mode=mode, thinking=thinking)
    env = os.environ.copy()
    env.update(load_env(repo_dir))
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s, env=env, check=False
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return AgentResult(
            exit_code=-1,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            duration_ms=elapsed,
            command=cmd,
            timed_out=True,
        )
    elapsed = int((time.monotonic() - started) * 1000)
    return AgentResult(
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        duration_ms=elapsed,
        command=cmd,
    )
