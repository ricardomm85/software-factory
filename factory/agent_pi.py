"""Spawn the pi coding agent headless and capture its output.

pi is the v1 coding agent (see PLAN.md §6). Every invocation is a subprocess
with the factory's secrets injected into its environment — the key never
appears in config files or on the command line.
"""

import json
import os
import signal
import subprocess
import threading
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
        # -p keeps the run non-interactive; stdout carries the JSONL event stream.
        cmd = ["pi", "--mode", "json", "-p"]
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
    env_dir: Path | None = None,
) -> AgentResult:
    """Run one pi invocation with factory secrets injected. Never raises on agent failure.

    ``repo_dir`` is where pi works; secrets come from ``env_dir``'s ``.env``
    (the roster's home), falling back to ``repo_dir`` when ``env_dir`` is not
    given. No global fallback either way.
    """
    cmd = build_pi_command(prompt, model=model, session_id=session_id, mode=mode, thinking=thinking)
    if repo_dir is None:
        repo_dir = Path.cwd()
    env = os.environ.copy()
    env.update(load_env(env_dir if env_dir is not None else repo_dir))
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            check=False,
            cwd=str(repo_dir),
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        # TimeoutExpired carries bytes even with text=True — decode before returning.
        stdout, stderr = exc.stdout or "", exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        return AgentResult(
            exit_code=-1,
            stdout=stdout,
            stderr=stderr,
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


def _final_assistant_text(events: list[dict]) -> str:
    """Pull the final assistant message text out of pi's JSONL event stream."""
    for evt in reversed(events):
        if evt.get("type") != "message_end":
            continue
        msg = evt.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
    return ""


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill pi and any children holding its pipes — a bare kill() leaks grandchildren."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def run_pi_streaming(
    prompt: str,
    model: str,
    session_id: str | None = None,
    thinking: str | None = None,
    timeout_s: int = 300,
    repo_dir: Path | None = None,
    env_dir: Path | None = None,
    on_event=None,
) -> AgentResult:
    """Run pi in JSONL mode, forwarding each event to ``on_event`` as it lands.

    This is the live-trace feed: events reach the SQLite tracer while the agent
    is still working, not batched at the end. The returned ``stdout`` is the
    final assistant text extracted from the event stream. ``repo_dir`` is where
    pi works; secrets come from ``env_dir`` (the roster's home), falling back to
    ``repo_dir``.
    """
    cmd = build_pi_command(prompt, model=model, session_id=session_id, mode="json", thinking=thinking)

    if repo_dir is None:
        repo_dir = Path.cwd()
    env = os.environ.copy()
    env.update(load_env(env_dir if env_dir is not None else repo_dir))
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(repo_dir),
        start_new_session=True,  # lets the timeout kill the whole tree, not just pi
    )

    events: list[dict] = []
    stderr_lines: list[str] = []
    bad_lines = 0

    def _pump() -> None:
        nonlocal bad_lines
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            events.append(evt)
            if on_event is not None:
                try:
                    on_event(evt)
                except Exception:
                    pass  # tracing must never break the agent run

    def _pump_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)

    pump = threading.Thread(target=_pump, daemon=True)
    err_pump = threading.Thread(target=_pump_stderr, daemon=True)
    pump.start()
    err_pump.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(proc)
    pump.join(timeout=5)
    err_pump.join(timeout=5)
    stderr = "".join(stderr_lines)
    final = _final_assistant_text(events)
    return AgentResult(
        exit_code=-1 if timed_out else (proc.returncode or 0),
        stdout=final,
        stderr=stderr,
        duration_ms=int((time.monotonic() - started) * 1000),
        command=cmd,
        timed_out=timed_out,
    )
