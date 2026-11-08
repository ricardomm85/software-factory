"""Chains: multi-phase workflows on top of the spine.

Phase 2: ``run_build_test`` — builder + code(test) with a bounded fix loop.
Phase 3: ``run_plan_build_test`` — the standard chain: planner writes a plan,
code hands it off and commits it, the builder implements it inside the same
gated build→test loop, and code makes the final commit with the builder's
proposed message. The agent never runs git itself.
Phase 5: ``run_plan_build_test(checkpoint_fn=...)`` — an optional human
checkpoint before the final commit: the pending changes are shown, and only
an explicit approval lands them. No human attached means the checkpoint fails.
"""

import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .agent_pi import run_pi_streaming
from .envelope import (
    PROMPTS_DIR,
    BuildOutput,
    FindingsOutput,
    PlanOutput,
    ScoredOutput,
)
from .gates import GateReport, artifacts_exist, changed_files, diff_matches_claims, run_output_gate
from .run import Run, call_agent, json_dumps


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


class CheckpointDeclined(RuntimeError):
    """The human said no at a pre-commit checkpoint (or none was there to ask)."""


def _pending_changes_summary(
    repo_dir: Path, max_chars: int = 4000, exempt_paths: set[str] | None = None
) -> str:
    """What the human is about to approve: status lines plus a diffstat, minus
    factory plumbing (exempt paths never land in the commit)."""
    status = _git("status", "--porcelain", repo_dir=repo_dir).stdout
    if exempt_paths:
        status = "\n".join(l for l in status.splitlines() if l[3:] not in exempt_paths)
    diffstat = _git("diff", "--stat", "HEAD", repo_dir=repo_dir).stdout
    body = f"pending changes:\n{status}\n{diffstat}".strip()
    if len(body) > max_chars:
        return f"{body[:max_chars]}\n[... truncated, {len(body) - max_chars} chars omitted ...]"
    return body


def _stdin_checkpoint(pending: str) -> bool:
    """Ask the human at stdin. Refuses when no human is attached — a checkpoint
    that silently approves itself would be theater."""
    if not sys.stdin.isatty():
        raise CheckpointDeclined(
            "checkpoint requested but stdin is not a TTY — no human to ask"
        )
    print(f"\n{pending}")
    try:
        answer = input("commit these changes? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt) as exc:
        # No answer is no approval: Ctrl-D / Ctrl-C at the prompt declines.
        raise CheckpointDeclined(f"no answer at the checkpoint prompt ({type(exc).__name__})") from exc
    return answer in ("y", "yes")


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


def gate_reports(
    envelope: BuildOutput, repo_dir: Path, allow: set[str] | None = None
) -> list[GateReport]:
    return [
        run_output_gate(envelope),
        artifacts_exist(envelope, repo_dir),
        diff_matches_claims(envelope, repo_dir, rollback=True, allow=allow),
    ]


def unstage_paths(paths: set[str] | None, repo_dir: Path) -> None:
    """Keep factory infrastructure (the trace db) out of the user's commits.

    A failed unstage is fatal, not best-effort: silently skipping it would land
    the trace db in the user's history — the exact failure this exists to prevent.
    """
    if paths:
        res = _git("reset", "-q", "--", *sorted(paths), repo_dir=repo_dir)
        if res.returncode != 0:
            raise RuntimeError(f"unstage failed: {(res.stderr or '').strip()[:300]}")


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
    exempt_paths: set[str] | None = None,
    env_dir: Path | None = None,
) -> tuple[bool, str, BuildOutput | None]:
    """Bounded build→test rounds. Returns (accepted, reason, final envelope).

    The caller owns the run lifecycle (create and finish) — this helper never
    settles the run, so a crash propagates and the wrapper decides.
    ``exempt_paths`` (factory infrastructure like the trace db) never counts as
    agent debris and is never rolled back. ``env_dir`` is the roster's home for
    secrets when the target ``repo_dir`` is elsewhere (``--repo``).
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
                env_dir=env_dir,
            )
            final_envelope = envelope
            if envelope.status != "success":
                # The agent decided the task is impossible — do not nag it
                # (Phase 1 contract: "the agent decided. Do not retry.").
                agent_declined = True
                reason = f"agent reported failure: {envelope.summary}"
                phase.log("agent_declined", {"summary": envelope.summary})
            else:
                reports = gate_reports(envelope, repo_dir, allow=exempt_paths)
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
    exempt_paths: set[str] | None = None,
    env_dir: Path | None = None,
) -> bool:
    """Builder + code(test) with bounded rounds. Returns whether the run was accepted.

    The run settles its own trace on any failure — no orphaned 'running' rows.
    ``exempt_paths`` never counts as agent debris and is never rolled back.
    """
    run = Run.create(
        conn, request=f"build-test: {prompt}", agent=agent_cfg["name"], model=agent_cfg["model"],
        repo=str(repo_dir),
    )
    try:
        accepted, reason, _ = _build_test_rounds(
            run, prompt, agent_cfg, test_command, repo_dir, max_rounds, agent_fn, test_fn,
            exempt_paths=exempt_paths, env_dir=env_dir,
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
    checkpoint_fn=None,
    questions_fn=None,
    plan_approval_fn=None,
    deliver_fn=None,
    on_run=None,
    launched_by: str = "",
    human_rounds: int = 4,
    exempt_paths: set[str] | None = None,
    env_dir: Path | None = None,
) -> tuple[bool, str]:
    """The standard chain: plan → plan gates → handoff → commit:plan → build/test rounds
    → checkpoint → commit → deliver.

    Lands the planner's plan commit and the builder's implementation commit (carrying the
    builder's proposed message) when the planner ships a plan file; degrades to a single
    implementation commit when it doesn't. ``checkpoint_fn`` (Phase 5) gates the final
    commit behind a human: it receives the pending-changes summary and returns approval;
    declining raises :class:`CheckpointDeclined` with the working tree left uncommitted
    for the human to dispose of.

    The web gates (Phase 7) are optional and only fire when wired:
    ``questions_fn`` receives the planner's ``questions`` list and returns the owner's
    answers — the planner resumes the SAME session with them until it ships a plan
    (bounded by ``human_rounds``). ``plan_approval_fn`` receives (plan_path, plan_text)
    and returns None for approval or a correction string that re-enters the same
    session. Both default to None: the CLI runs exactly as before, and a planner that
    asks questions with nobody attached fails its gates honestly. ``deliver_fn``
    (Phase 8) runs after the final commit inside a ``deliver`` phase; whatever it
    returns is logged (e.g. ``{"pr_url": ...}``) — a raise fails the run. ``on_run``
    fires the moment the run exists (the board uses it to bind the card to the run).
    ``launched_by`` stamps who created the run ('' = the CLI, ``'board'`` = the serve
    daemon) — the board's startup reaper settles only its own runs, never the CLI's
    sharing the same trace db. Agents never run git themselves. ``exempt_paths`` (the trace db and its WAL
    sidecars) is factory infrastructure: never agent debris, never rolled back,
    never committed. Returns (accepted, reason).
    """
    run = Run.create(
        conn, request=f"plan-build-test: {request}", agent=planner_cfg["name"], model=planner_cfg["model"],
        repo=str(repo_dir), launched_by=launched_by,
    )
    if on_run is not None:
        on_run(run)
    accepted = False
    reason = ""
    build_envelope: BuildOutput | None = None

    try:
        instruction = (PROMPTS_DIR / "plan_instruction.md").read_text(encoding="utf-8")
        planner_prompt = instruction.replace("{{run_id}}", run.id).replace("{{request}}", request)
        with run.phase("plan", kind="agent", owner=planner_cfg["name"]) as phase:
            plan_envelope = _plan_with_human_gates(
                run, phase, planner_prompt, planner_cfg, repo_dir,
                agent_fn=agent_fn, env_dir=env_dir,
                questions_fn=questions_fn, plan_approval_fn=plan_approval_fn,
                max_human_rounds=human_rounds,
            )
        if plan_envelope.status != "success":
            # The planner decided the request is not actionable — do not nag it.
            reason = f"planner reported failure: {plan_envelope.summary}"
            run.finish(accepted=False, reason=reason)
            return False, reason

        # Gate the planner's work exactly like the builder's — on every path.
        # The plan file must exist and be claimed, and nothing unclaimed
        # survives (rolled back): the no-plan early exit below runs the same
        # gates so the Phase 2 rollback guarantee holds even when the planner
        # wrote debris and then reported questions instead.
        plan_rel = plan_envelope.plan_path or (plan_envelope.artifacts[0] if plan_envelope.artifacts else "")
        if plan_envelope.status == "success" and not plan_rel:
            # "Success" without a plan file is a planner that either still asks
            # questions or reports an empty success — either way the builder has
            # nothing honest to implement. The gates still run (empty claims =
            # everything the planner wrote is debris and gets rolled back), so
            # the CLI path — which has no board worktree-restore net — keeps the
            # writes boundary. The two question cases get honest causes: nobody
            # wired to answer, or the human-rounds bound exhausted.
            with run.phase("plan_gates", kind="code", owner="code") as phase:
                empty_claim = BuildOutput(status="success", summary=plan_envelope.summary)
                reports = [
                    run_output_gate(empty_claim),
                    artifacts_exist(empty_claim, repo_dir),
                    diff_matches_claims(empty_claim, repo_dir, rollback=True, allow=exempt_paths),
                ]
                phase.log("gate_results", {r.gate: r.to_payload() for r in reports})
            if plan_envelope.questions:
                reason = (
                    "planner returned questions but no one is wired to answer them"
                    if questions_fn is None
                    else f"planner is still asking questions after {human_rounds} human rounds"
                )
            else:
                reason = "planner reported success without a plan path"
            run.finish(accepted=False, reason=reason)
            return False, reason
        plan_claim = BuildOutput(
            status=plan_envelope.status,
            summary=plan_envelope.summary,
            artifacts=plan_envelope.artifacts,
            changed_files=[plan_rel] if plan_rel else [],
        )
        with run.phase("plan_gates", kind="code", owner="code") as phase:
            reports = [
                run_output_gate(plan_claim),
                artifacts_exist(plan_claim, repo_dir),
                diff_matches_claims(plan_claim, repo_dir, rollback=True, allow=exempt_paths),
            ]
            phase.log("gate_results", {r.gate: r.to_payload() for r in reports})
        # A missing/empty plan kills the chain (the builder has nothing to implement).
        # A rolled-back debris violation is already remediated — it stays in the trace
        # and the chain continues.
        artifacts_report = next(r for r in reports if r.gate == "artifacts_exist")
        violations = _violations(reports)
        if not artifacts_report.passed:
            reason = "planner gates failed: " + "; ".join(violations)
            run.finish(accepted=False, reason=reason)
            return False, reason
        if violations:
            run.log_event(None, "plan_gate_violations", {"violations": violations})

        # Handoff: the plan crosses agents as a file plus its content in the
        # builder's prompt — context transfers in code, not in conversation.
        plan_text = ""
        if plan_rel and (repo_dir / plan_rel).is_file():
            plan_text = (repo_dir / plan_rel).read_text(encoding="utf-8")
        with run.phase("handoff", kind="code", owner="code") as phase:
            if not plan_text:
                plan_text = plan_envelope.summary
                phase.log("handoff_degraded", {"note": "plan file missing; handing off summary only"})
            phase.log("handoff", {"plan_path": plan_rel, "chars": len(plan_text)})

        # Commit:plan — the plan lands as its own commit before any code moves.
        # The trace db stays out of the user's history (unstaged after add -A).
        if plan_rel:
            add_res = _git("add", "-A", repo_dir=repo_dir)
            unstage_paths(exempt_paths, repo_dir)
            plan_message = f"factory({run.id}): plan — {plan_envelope.summary[:60]}"
            commit_res = _git("commit", "-m", plan_message, repo_dir=repo_dir)
            if add_res.returncode != 0 or commit_res.returncode != 0:
                raise RuntimeError(
                    f"plan commit failed: {(commit_res.stderr or add_res.stderr).strip()[:300]}"
                )
            sha = _git("rev-parse", "--short", "HEAD", repo_dir=repo_dir).stdout.strip()
            with run.phase("commit:plan", kind="code", owner="git") as phase:
                phase.log("commit", {"sha": sha, "message": plan_message})

        builder_prompt = _correction(
            "builder_handoff", request=request, planner=planner_cfg["name"], plan_text=plan_text
        )
        accepted, reason, build_envelope = _build_test_rounds(
            run, builder_prompt, builder_cfg, test_command, repo_dir, max_rounds, agent_fn, test_fn,
            exempt_paths=exempt_paths, env_dir=env_dir,
        )

        if accepted:
            message = (build_envelope.commit_message if build_envelope else "").strip()
            if not message:
                message = f"factory({run.id}): {(build_envelope.summary if build_envelope else 'implementation')[:80]}"
            if checkpoint_fn is not None:
                # The human owns the last word: what would land is shown, and only
                # an explicit approval commits. A decline leaves the tree as-is.
                with run.phase("checkpoint", kind="code", owner="human") as phase:
                    pending = _pending_changes_summary(repo_dir, exempt_paths=exempt_paths)
                    phase.log("checkpoint_request", {"pending": pending})
                    if not checkpoint_fn(pending):
                        phase.log("checkpoint_declined", {})
                        raise CheckpointDeclined(
                            "declined at the pre-commit checkpoint — changes left uncommitted"
                        )
                    phase.log("checkpoint_decision", {"approved": True})
            add_res = _git("add", "-A", repo_dir=repo_dir)
            unstage_paths(exempt_paths, repo_dir)
            commit_res = _git("commit", "-m", message, repo_dir=repo_dir)
            if add_res.returncode != 0 or commit_res.returncode != 0:
                raise RuntimeError(
                    f"final commit failed: {(commit_res.stderr or add_res.stderr).strip()[:300]}"
                )
            sha = _git("rev-parse", "--short", "HEAD", repo_dir=repo_dir).stdout.strip()
            with run.phase("commit", kind="code", owner="git") as phase:
                phase.log("commit", {"sha": sha, "message": message})
            if deliver_fn is not None:
                with run.phase("deliver", kind="code", owner="git") as phase:
                    info = deliver_fn(run.id, message)
                    phase.log("delivered", info)
    except BaseException as exc:
        run.finish(accepted=False, reason=f"{type(exc).__name__}: {exc}")
        raise

    run.finish(accepted=accepted, reason=reason)
    return accepted, reason


def _plan_with_human_gates(
    run: Run,
    phase,
    prompt: str,
    planner_cfg: dict,
    repo_dir: Path,
    *,
    agent_fn,
    env_dir: Path | None,
    questions_fn,
    plan_approval_fn,
    max_human_rounds: int,
) -> PlanOutput:
    """The planner loop behind the web gates (Phase 7).

    Questions: while the planner answers with ``questions`` and someone is wired
    to ask, the run parks, the owner's answers re-enter the SAME pi session, and
    the planner tries again — bounded by ``max_human_rounds``. Plan approval: the
    finished plan is shown; None means approved, a string is a correction that
    also re-enters the session. With no gates wired the planner is called exactly
    once and the loop bodies never run — the CLI behavior is unchanged, byte for
    byte.
    """
    envelope = call_agent(
        run, phase, prompt, planner_cfg, output_type=PlanOutput,
        agent_fn=agent_fn, repo_dir=repo_dir, env_dir=env_dir,
    )

    rounds = 0
    while (
        questions_fn is not None
        and envelope.status == "success"
        and envelope.questions
        and rounds < max_human_rounds
    ):
        rounds += 1
        phase.log("planner_questions", {"round": rounds, "count": len(envelope.questions)})
        answers = questions_fn(list(envelope.questions))
        phase.log("planner_answers", {"round": rounds, "chars": len(answers)})
        prompt = f"{prompt}\n\n{_correction('planner_answers', round=str(rounds), answers=answers)}"
        envelope = call_agent(
            run, phase, prompt, planner_cfg, output_type=PlanOutput,
            agent_fn=agent_fn, repo_dir=repo_dir, env_dir=env_dir,
        )

    if plan_approval_fn is None or envelope.status != "success" or envelope.questions:
        # Still-questioning after the bound (or failing) falls through: the plan
        # gates decide honestly — no plan file, no pass.
        return envelope

    for round_no in range(1, max_human_rounds + 1):
        plan_rel = envelope.plan_path or (envelope.artifacts[0] if envelope.artifacts else "")
        plan_text = ""
        if plan_rel and (repo_dir / plan_rel).is_file():
            plan_text = (repo_dir / plan_rel).read_text(encoding="utf-8")
        decision = plan_approval_fn(plan_rel, plan_text)
        if decision is None:
            phase.log("plan_approved", {"round": round_no})
            return envelope
        phase.log("plan_correction", {"round": round_no, "chars": len(decision)})
        prompt = f"{prompt}\n\n{_correction('plan_correction', decision=decision)}"
        envelope = call_agent(
            run, phase, prompt, planner_cfg, output_type=PlanOutput,
            agent_fn=agent_fn, repo_dir=repo_dir, env_dir=env_dir,
        )
    raise CheckpointDeclined(f"plan not approved after {max_human_rounds} rounds")


def _working_tree_diff(repo_dir: Path, max_chars: int = 80000, exempt_paths: set[str] | None = None) -> str:
    """The full working-tree diff vs HEAD — the material every reviewer reads.

    ``exempt_paths`` (factory infrastructure like the trace db) never enters a
    reviewer's context: it is untracked plumbing, not work under review.
    """
    res = _git("diff", "HEAD", repo_dir=repo_dir)
    untracked = _git("ls-files", "--others", "--exclude-standard", repo_dir=repo_dir)
    body = res.stdout
    skip = exempt_paths or set()
    for path in untracked.stdout.splitlines():
        if path in skip:
            continue
        p = repo_dir / path
        if p.is_file():
            body += f"\n--- NEW FILE: {path} ---\n{p.read_text(encoding='utf-8', errors='replace')}\n"
    if len(body) > max_chars:
        original_len = len(body)
        head = body[: max_chars // 2]
        tail = body[-max_chars // 4 :]
        omitted = original_len - len(head) - len(tail)
        body = f"{head}\n\n[... diff truncated, {omitted} chars omitted ...]\n\n{tail}"
    return body


def _review_prompt(agent_name: str, diff: str) -> str:
    # roster name review-conventions -> template review_conventions.md
    lens = agent_name.replace("review-", "", 1).replace("-", "_")
    template = (PROMPTS_DIR / f"review_{lens}.md").read_text(encoding="utf-8")
    return template.replace("{{diff}}", diff)


def run_build_review(
    conn,
    repo_dir: Path,
    reviewer_cfgs: list[dict],
    scorer_cfg: dict,
    fixer_cfg: dict,
    max_rounds: int = 2,
    agent_fn=run_pi_streaming,
    timeout_s: int = 600,
    exempt_paths: set[str] | None = None,
    env_dir: Path | None = None,
) -> tuple[bool, str]:
    """Multi-lens review of the working-tree changes: reviewers → scorer → gate (>=50
    blocks) → bounded fix rounds with the fixer, re-reviewed each round.

    The user's own WIP (dirty at chain start) is exempt from the fixer's rollback —
    reviewers see it, but only verified findings get fixed, and only by the fixer's
    claimed changes. ``exempt_paths`` (the trace db) is invisible to reviewers and
    survives the fixer's rollback. ``env_dir`` is the roster's home for secrets.
    Returns (accepted, reason).
    """
    run = Run.create(
        conn, request="build-review: working-tree changes", agent=reviewer_cfgs[0]["name"],
        model=reviewer_cfgs[0]["model"], repo=str(repo_dir),
    )
    baseline = set(changed_files(repo_dir))  # the user's WIP — exempt from rollback
    fixer_allow = baseline | (exempt_paths or set())
    accepted = False
    reason = ""
    try:
        for round_no in range(1, max_rounds + 1):
            diff = _working_tree_diff(repo_dir, exempt_paths=exempt_paths)
            if not diff.strip():
                run.finish(accepted=True, reason="no changes to review")
                return True, "no changes to review"

            findings: list[dict] = []
            for rcfg in reviewer_cfgs:
                with run.phase(f"review:{rcfg['name']}:r{round_no}", kind="agent", owner=rcfg["name"]) as phase:
                    env = call_agent(
                        run, phase, _review_prompt(rcfg["name"], diff), rcfg,
                        output_type=FindingsOutput, agent_fn=agent_fn, repo_dir=repo_dir,
                        timeout_s=timeout_s, env_dir=env_dir,
                    )
                    phase.log("findings", {"count": len(env.findings)})
                    findings.extend(f.model_dump() for f in env.findings)

            with run.phase(f"score:r{round_no}", kind="agent", owner=scorer_cfg["name"]) as phase:
                score_prompt = _correction(
                    "score_findings",
                    findings=json_dumps({"issues": findings}),
                    diff=diff,
                )
                scored = call_agent(
                    run, phase, score_prompt, scorer_cfg,
                    output_type=ScoredOutput, agent_fn=agent_fn, repo_dir=repo_dir,
                    timeout_s=timeout_s, env_dir=env_dir,
                )
                phase.log("scored", {"count": len(scored.findings)})

            blocked = [f for f in scored.findings if f.score >= 50]
            with run.phase(f"gate:r{round_no}", kind="code", owner="code") as phase:
                phase.log(
                    "review_gate",
                    {"total": len(scored.findings), "blocked": len(blocked), "threshold": 50},
                )
                gate_ok = not blocked

            if gate_ok:
                accepted = True
                reason = "no findings scored >=50"
                break
            if round_no == max_rounds:
                reason = f"{len(blocked)} findings scored >=50 after final round: " + "; ".join(
                    f"{f.file}:{f.line} {f.description[:80]} (score {f.score})" for f in blocked
                )
                break

            findings_json = json_dumps({"issues": [f.model_dump() for f in blocked]})
            with run.phase(f"fix:r{round_no}", kind="agent", owner=fixer_cfg["name"]) as phase:
                # The fixer claims everything it touched — including edits to the
                # user's baseline files; unclaimed changes are rolled back. Gate
                # violations are corrected in-session (never cold-restart).
                fix_prompt = _correction("fix_findings", findings=findings_json, diff=diff[:40000])
                for attempt in range(1, 3):
                    fix_envelope = call_agent(
                        run, phase, fix_prompt, fixer_cfg,
                        output_type=BuildOutput, agent_fn=agent_fn, repo_dir=repo_dir,
                        timeout_s=timeout_s, env_dir=env_dir,
                    )
                    reports = gate_reports(fix_envelope, repo_dir, allow=fixer_allow)
                    phase.log("gate_results", {r.gate: r.to_payload() for r in reports})
                    if all(r.passed for r in reports):
                        break
                    violations = _violations(reports)
                    run.log_event(phase.id, "fix_gate_violations", {"violations": violations})
                    fix_prompt = f"{fix_prompt}\n\n{_correction('gate_correction', violations='\\n'.join(violations))}"
    except BaseException as exc:
        run.finish(accepted=False, reason=f"{type(exc).__name__}: {exc}")
        raise

    run.finish(accepted=accepted, reason=reason)
    return accepted, reason
