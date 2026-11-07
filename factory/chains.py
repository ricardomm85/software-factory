"""Phase 2 chain: build → code(test) with a bounded fix loop.

The builder works inside one pi session for the whole chain. Gate violations
and red test suites are corrected in that same session — a correction costs one
message, a cold restart throws away everything the builder learned. Each round
is two phases (build + test) so the trace shows exactly where a run died.
"""

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .agent_pi import run_pi_streaming
from .envelope import PROMPTS_DIR, BuildOutput
from .gates import GateReport, artifacts_exist, diff_matches_claims, run_output_gate
from .run import Run, call_agent


@dataclass
class TestResult:
    exit_code: int
    output_tail: str
    duration_ms: int
    command: str


def run_test_command(command: str, repo_dir: Path, timeout_s: int = 600) -> TestResult:
    """Run the repo's real test command. Placeholders that exit 0 are theater."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            shlex.split(command),
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # A hung suite is a red result (exit 124), not a crash — the bounded
        # rounds decide what happens next.
        elapsed = int((time.monotonic() - started) * 1000)
        tail = exc.stdout or ""
        if isinstance(tail, bytes):
            tail = tail.decode(errors="replace")
        return TestResult(
            exit_code=124,
            output_tail=f"test command timed out after {timeout_s}s\n{str(tail)[-800:]}",
            duration_ms=elapsed,
            command=command,
        )
    tail = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-2000:]
    return TestResult(
        exit_code=proc.returncode,
        output_tail=tail,
        duration_ms=int((time.monotonic() - started) * 1000),
        command=command,
    )


def gate_reports(envelope: BuildOutput, repo_dir: Path) -> list[GateReport]:
    return [
        run_output_gate(envelope),
        artifacts_exist(envelope, repo_dir),
        diff_matches_claims(envelope, repo_dir, rollback=True),
    ]


def _violations(reports: list[GateReport]) -> list[str]:
    return [v for r in reports for v in r.violations()]


def _correction(name: str, **placeholders: str) -> str:
    template = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    for key, value in placeholders.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def run_build_test(
    conn,
    prompt: str,
    agent_cfg: dict,
    test_command: str,
    repo_dir: Path,
    max_rounds: int = 3,
    agent_fn=run_pi_streaming,
    test_fn=run_test_command,
) -> bool:
    """Builder + code(test) with bounded rounds. Returns whether the run was accepted.

    The run settles its own trace on any failure — no orphaned 'running' rows.
    """
    run = Run.create(conn, request=f"build-test: {prompt}", agent=agent_cfg["name"], model=agent_cfg["model"])
    builder_prompt = prompt
    accepted = False
    reason = ""

    try:
        for round_no in range(1, max_rounds + 1):
            agent_declined = False
            build_ok = False
            tests_ok = False
            with run.phase(f"build:r{round_no}", kind="agent", owner=agent_cfg["name"]) as phase:
                envelope = call_agent(
                    run,
                    phase,
                    builder_prompt,
                    agent_cfg,
                    output_type=BuildOutput,
                    agent_fn=agent_fn,
                    repo_dir=repo_dir,
                )
                if envelope.status != "success":
                    # The agent decided the task is impossible — do not nag it
                    # (Phase 1 contract: "the agent decided. Do not retry.").
                    agent_declined = True
                    reason = f"agent reported failure: {envelope.summary}"
                    phase.log("agent_declined", {"summary": envelope.summary})
                else:
                    reports = gate_reports(envelope, repo_dir)
                    phase.log("gate_results", {r.gate: r.to_payload() for r in reports})
                    build_ok = all(r.passed for r in reports)
                    if not build_ok:
                        violations = _violations(reports)
                        if round_no < max_rounds:
                            builder_prompt = f"{prompt}\n\n{_correction('gate_correction', violations='\\n'.join(violations))}"

            if agent_declined:
                break
            if not build_ok:
                reason = f"gate violations in round {round_no}"
                continue

            with run.phase(f"test:r{round_no}", kind="code", owner="code") as phase:
                test_result = test_fn(test_command, repo_dir)
                phase.log(
                    "test_run",
                    {
                        "command": test_result.command,
                        "exit_code": test_result.exit_code,
                        "duration_ms": test_result.duration_ms,
                        "tail": test_result.output_tail[-800:],
                    },
                )
                tests_ok = test_result.exit_code == 0
                if not tests_ok:
                    reason = f"tests failed in round {round_no} (exit {test_result.exit_code})"
                    if round_no < max_rounds:
                        builder_prompt = f"{prompt}\n\n{_correction('test_correction', exit_code=str(test_result.exit_code), tail=test_result.output_tail[-800:])}"

            if tests_ok:
                accepted = True
                break
    except BaseException as exc:
        run.finish(accepted=False, reason=f"{type(exc).__name__}: {exc}")
        raise

    run.finish(accepted=accepted, reason=reason)
    return accepted
