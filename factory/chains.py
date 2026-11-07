"""Chains: multi-phase workflows on top of the spine.

Phase 2: ``run_build_test`` — builder + code(test) with a bounded fix loop.
Phase 3: ``run_plan_build_test`` — the standard chain: planner writes a plan,
code hands it off and commits it, the builder implements it inside the same
gated build→test loop, and code makes the final commit with the builder's
proposed message. The agent never runs git itself.
"""

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .agent_pi import run_pi_streaming
from .envelope import PROMPTS_DIR, BuildOutput, PlanOutput
from .gates import GateReport, artifacts_exist, diff_matches_claims, run_output_gate
from .run import Run, call_agent


@dataclass
class TestResult:
    exit_code: int
    output_tail: str
    duration_ms: int
    command: str


def _git(*args: str, repo_dir: Path):
    return subprocess.run(
        ["git", *args], cwd=str(repo_dir), capture_output=True, text=True, check=False
    )


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


def _build_test_rounds(
    run: Run,
    prompt: str,
    agent_cfg: dict,
    test_command: str,
    repo_dir: Path,
    max_rounds: int,
    agent_fn,
    test_fn,
) -> tuple[bool, str, BuildOutput | None]:
    """Bounded build→test rounds. Returns (accepted, reason, final envelope).

    The caller owns the run lifecycle (create and finish) — this helper never
    settles the run, so a crash propagates and the wrapper decides.
    """
    builder_prompt = prompt
    accepted = False
    reason = ""
    final_envelope: BuildOutput | None = None

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
            final_envelope = envelope
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

    return accepted, reason, final_envelope


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
    try:
        accepted, reason, _ = _build_test_rounds(
            run, prompt, agent_cfg, test_command, repo_dir, max_rounds, agent_fn, test_fn
        )
    except BaseException as exc:
        run.finish(accepted=False, reason=f"{type(exc).__name__}: {exc}")
        raise
    run.finish(accepted=accepted, reason=reason)
    return accepted


def run_plan_build_test(
    conn,
    request: str,
    planner_cfg: dict,
    builder_cfg: dict,
    test_command: str,
    repo_dir: Path,
    max_rounds: int = 3,
    agent_fn=run_pi_streaming,
    test_fn=run_test_command,
) -> tuple[bool, str]:
    """The standard chain: plan → handoff → commit:plan → build/test rounds → commit.

    Lands two commits when everything is green: the planner's plan and the
    builder's implementation, each with its own author-flavored message.
    Returns (accepted, reason).
    """
    run = Run.create(
        conn, request=f"plan-build-test: {request}", agent=planner_cfg["name"], model=planner_cfg["model"]
    )
    accepted = False
    reason = ""
    build_envelope: BuildOutput | None = None

    try:
        instruction = (PROMPTS_DIR / "plan_instruction.md").read_text(encoding="utf-8")
        planner_prompt = instruction.replace("{{run_id}}", run.id).replace("{{request}}", request)
        with run.phase("plan", kind="agent", owner=planner_cfg["name"]) as phase:
            plan_envelope = call_agent(
                run, phase, planner_prompt, planner_cfg, output_type=PlanOutput, agent_fn=agent_fn, repo_dir=repo_dir
            )
        if plan_envelope.status != "success":
            # The planner decided the request is not actionable — do not nag it.
            reason = f"planner reported failure: {plan_envelope.summary}"
            run.finish(accepted=False, reason=reason)
            return False, reason

        # Handoff: the plan crosses agents as a file plus its content in the
        # builder's prompt — context transfers in code, not in conversation.
        plan_rel = plan_envelope.plan_path or (plan_envelope.artifacts[0] if plan_envelope.artifacts else "")
        plan_text = ""
        if plan_rel and (repo_dir / plan_rel).is_file():
            plan_text = (repo_dir / plan_rel).read_text(encoding="utf-8")
        with run.phase("handoff", kind="code", owner="code") as phase:
            if not plan_text:
                plan_text = plan_envelope.summary
                phase.log("handoff_degraded", {"note": "plan file missing; handing off summary only"})
            phase.log("handoff", {"plan_path": plan_rel, "chars": len(plan_text)})

        # Commit:plan — the plan lands as its own commit before any code moves.
        if plan_rel:
            add_res = _git("add", "-A", repo_dir=repo_dir)
            plan_message = f"factory({run.id}): plan — {plan_envelope.summary[:60]}"
            commit_res = _git("commit", "-m", plan_message, repo_dir=repo_dir)
            if add_res.returncode != 0 or commit_res.returncode != 0:
                raise RuntimeError(
                    f"plan commit failed: {(commit_res.stderr or add_res.stderr).strip()[:300]}"
                )
            sha = _git("rev-parse", "--short", "HEAD", repo_dir=repo_dir).stdout.strip()
            with run.phase("commit:plan", kind="code", owner="git") as phase:
                phase.log("commit", {"sha": sha, "message": plan_message})

        builder_prompt = (
            f"{request}\n\n"
            f"## Implementation plan (from {planner_cfg['name']})\n\n{plan_text}\n\n"
            "Implement this plan. changed_files must declare EVERY file you change."
        )
        accepted, reason, build_envelope = _build_test_rounds(
            run, builder_prompt, builder_cfg, test_command, repo_dir, max_rounds, agent_fn, test_fn
        )

        if accepted:
            message = (build_envelope.commit_message if build_envelope else "").strip()
            if not message:
                message = f"factory({run.id}): {(build_envelope.summary if build_envelope else 'implementation')[:80]}"
            add_res = _git("add", "-A", repo_dir=repo_dir)
            commit_res = _git("commit", "-m", message, repo_dir=repo_dir)
            if add_res.returncode != 0 or commit_res.returncode != 0:
                raise RuntimeError(
                    f"final commit failed: {(commit_res.stderr or add_res.stderr).strip()[:300]}"
                )
            sha = _git("rev-parse", "--short", "HEAD", repo_dir=repo_dir).stdout.strip()
            with run.phase("commit", kind="code", owner="git") as phase:
                phase.log("commit", {"sha": sha, "message": message})
    except BaseException as exc:
        run.finish(accepted=False, reason=f"{type(exc).__name__}: {exc}")
        raise

    run.finish(accepted=accepted, reason=reason)
    return accepted, reason
