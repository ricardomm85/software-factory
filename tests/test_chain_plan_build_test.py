"""Tests for the standard chain: plan → handoff → commit:plan → build/test → commit,
plus the Phase 5 human checkpoint before the final commit."""

import subprocess
import sys
from pathlib import Path

import pytest

from factory.agent_pi import AgentResult
from factory.chains import CheckpointDeclined, _stdin_checkpoint, run_plan_build_test
from factory.db import connect

PLANNER_CFG = {"name": "planner", "model": "zai/glm-5.3-flash", "thinking": "high"}
BUILDER_CFG = {"name": "builder", "model": "zai/glm-5.3-flash", "thinking": "medium"}

PLAN_JSON = (
    '{"status": "success", "summary": "plan ready: add feature.txt", '
    '"artifacts": ["specs/plan.md"], "plan_path": "specs/plan.md", '
    '"notes_for_next_agent": "just create feature.txt"}'
)
BUILD_JSON = (
    '{"status": "success", "summary": "implemented the plan", '
    '"artifacts": [], "changed_files": ["feature.txt"], '
    '"commit_message": "feat: add feature.txt per plan"}'
)


class StubAgent:
    """One stub plays both agents in order; applies side effects per call."""

    def __init__(self, outputs, side_effects=None):
        self.outputs = list(outputs)
        self.side_effects = list(side_effects or [])
        self.calls = []

    def __call__(self, prompt, model, session_id=None, on_event=None, repo_dir=None, **kwargs):
        self.calls.append({"prompt": prompt, "session_id": session_id})
        if self.side_effects:
            effect = self.side_effects.pop(0)
            if effect is not None and repo_dir is not None:
                effect(Path(repo_dir))
        if on_event is not None:
            on_event({"type": "agent_start"})
        return AgentResult(exit_code=0, stdout=self.outputs.pop(0), stderr="", duration_ms=1, command=[])


class StubTests:
    def __init__(self, exit_codes):
        self.exit_codes = list(exit_codes)
        self.runs = 0

    def __call__(self, command, repo_dir, timeout_s=600):
        self.runs += 1
        from factory.chains import TestResult

        return TestResult(exit_code=self.exit_codes.pop(0), output_tail="", duration_ms=5, command=command)


def write_plan(repo_dir: Path):
    (repo_dir / "specs").mkdir()
    (repo_dir / "specs" / "plan.md").write_text("# Plan\n- create feature.txt saying 'feature'\n")


def write_feature(repo_dir: Path):
    (repo_dir / "feature.txt").write_text("feature\n")


def _commits(repo):
    out = subprocess.run(
        ["git", "log", "--format=%s"], cwd=str(repo), capture_output=True, text=True, check=True
    )
    return out.stdout.strip().splitlines()


def test_green_chain_lands_plan_and_code_commits(repo, tmp_path):
    agent = StubAgent(
        [PLAN_JSON, BUILD_JSON],
        side_effects=[write_plan, write_feature],
    )
    tests = StubTests([0])
    conn = connect(tmp_path / "factory.db")
    accepted, reason = run_plan_build_test(
        conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
        repo_dir=repo, agent_fn=agent, test_fn=tests,
    )
    assert accepted, reason

    # Two author-flavored commits on top of init: plan first, then implementation.
    subjects = _commits(repo)
    assert subjects[0] == "feat: add feature.txt per plan"          # builder's own words
    assert subjects[1].startswith("factory(") and "plan ready" in subjects[1]
    assert subjects[-1] == "init"

    # The builder received the plan content in its prompt (the handoff).
    assert "# Plan" in agent.calls[1]["prompt"]
    assert agent.calls[0]["session_id"].endswith("-planner")
    assert agent.calls[1]["session_id"].endswith("-builder")

    # Trace shows the full chain in order.
    phases = [r["name"] for r in conn.execute(
        "SELECT name FROM phases ORDER BY seq"
    ).fetchall()]
    assert phases == ["plan", "plan_gates", "handoff", "commit:plan", "build:r1", "test:r1", "commit"]

    status = conn.execute("SELECT status FROM sessions").fetchone()[0]
    assert status == "accepted"


def test_planner_declined_stops_before_the_builder(repo, tmp_path):
    declined = (
        '{"status": "fail", "summary": "not actionable: request is vague", '
        '"artifacts": [], "plan_path": "", "notes_for_next_agent": ""}'
    )
    agent = StubAgent([declined])
    tests = StubTests([])
    conn = connect(tmp_path / "factory.db")
    accepted, reason = run_plan_build_test(
        conn, "do something vague", PLANNER_CFG, BUILDER_CFG, test_command="true",
        repo_dir=repo, agent_fn=agent, test_fn=tests,
    )
    assert not accepted
    assert "not actionable" in reason
    assert len(agent.calls) == 1          # builder never spawned
    assert tests.runs == 0
    assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "failed"


def test_fallback_commit_message_when_builder_stays_silent(repo, tmp_path):
    silent_build = (
        '{"status": "success", "summary": "implemented the plan", '
        '"artifacts": [], "changed_files": ["feature.txt"], "commit_message": ""}'
    )
    agent = StubAgent([PLAN_JSON, silent_build], side_effects=[write_plan, write_feature])
    tests = StubTests([0])
    conn = connect(tmp_path / "factory.db")
    accepted, reason = run_plan_build_test(
        conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
        repo_dir=repo, agent_fn=agent, test_fn=tests,
    )
    assert accepted, reason
    subject = _commits(repo)[0]
    assert subject.startswith("factory(")  # code-owned fallback wording
    assert "implemented the plan" in subject


def test_planner_debris_is_rolled_back_at_plan_gates(repo, tmp_path):
    def write_plan_and_rogue(repo_dir: Path):
        write_plan(repo_dir)
        (repo_dir / "rogue.txt").write_text("planner debris\n")

    agent = StubAgent([PLAN_JSON, BUILD_JSON], side_effects=[write_plan_and_rogue, write_feature])
    tests = StubTests([0])
    conn = connect(tmp_path / "factory.db")
    accepted, reason = run_plan_build_test(
        conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
        repo_dir=repo, agent_fn=agent, test_fn=tests,
    )
    assert accepted, reason
    assert not (repo / "rogue.txt").exists()  # plan_gates rolled the debris back
    assert all("rogue" not in s for s in _commits(repo))


def test_missing_plan_file_fails_at_plan_gates_without_builder(repo, tmp_path):
    no_file = (
        '{"status": "success", "summary": "plan ready", '
        '"artifacts": ["specs/plan.md"], "plan_path": "specs/plan.md", "notes_for_next_agent": ""}'
    )
    agent = StubAgent([no_file])  # declares the plan but never writes it
    tests = StubTests([])
    conn = connect(tmp_path / "factory.db")
    accepted, reason = run_plan_build_test(
        conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
        repo_dir=repo, agent_fn=agent, test_fn=tests,
    )
    assert not accepted
    assert "planner gates failed" in reason
    assert len(agent.calls) == 1  # builder never spawned


# --- Phase 5: the human checkpoint before the final commit --------------------


def test_checkpoint_approval_lets_the_commit_land(repo, tmp_path):
    agent = StubAgent([PLAN_JSON, BUILD_JSON], side_effects=[write_plan, write_feature])
    tests = StubTests([0])
    seen = []

    def approve(pending):
        seen.append(pending)
        return True

    conn = connect(tmp_path / "factory.db")
    accepted, reason = run_plan_build_test(
        conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
        repo_dir=repo, agent_fn=agent, test_fn=tests, checkpoint_fn=approve,
    )
    assert accepted, reason
    assert len(seen) == 1 and "feature.txt" in seen[0]   # the human saw the pending changes
    phases = [r["name"] for r in conn.execute("SELECT name FROM phases ORDER BY seq").fetchall()]
    assert phases == ["plan", "plan_gates", "handoff", "commit:plan", "build:r1", "test:r1", "checkpoint", "commit"]
    cp = conn.execute("SELECT status FROM phases WHERE name = 'checkpoint'").fetchone()[0]
    assert cp == "pass"


def test_checkpoint_decline_rejects_run_and_leaves_tree_dirty(repo, tmp_path):
    agent = StubAgent([PLAN_JSON, BUILD_JSON], side_effects=[write_plan, write_feature])
    tests = StubTests([0])
    conn = connect(tmp_path / "factory.db")
    with pytest.raises(CheckpointDeclined):
        run_plan_build_test(
            conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
            repo_dir=repo, agent_fn=agent, test_fn=tests, checkpoint_fn=lambda pending: False,
        )
    # The human owns the changes now: nothing was committed, the run is not accepted.
    assert (repo / "feature.txt").exists()
    assert "feat: add feature.txt per plan" not in _commits(repo)
    assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "failed"
    phase = conn.execute(
        "SELECT status, error FROM phases WHERE name = 'checkpoint'"
    ).fetchone()
    assert phase[0] == "fail" and "declined" in phase[1]


def test_no_checkpoint_fn_means_no_checkpoint_phase(repo, tmp_path):
    agent = StubAgent([PLAN_JSON, BUILD_JSON], side_effects=[write_plan, write_feature])
    tests = StubTests([0])
    conn = connect(tmp_path / "factory.db")
    accepted, _ = run_plan_build_test(
        conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
        repo_dir=repo, agent_fn=agent, test_fn=tests,
    )
    assert accepted
    names = [r["name"] for r in conn.execute("SELECT name FROM phases ORDER BY seq").fetchall()]
    assert "checkpoint" not in names   # opt-in: the default chain stays as it was


def test_stdin_checkpoint_refuses_without_a_tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(CheckpointDeclined, match="no human to ask"):
        _stdin_checkpoint("pending changes: whatever")


def test_stdin_checkpoint_reads_y_or_n(monkeypatch):
    import builtins

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "y")
    assert _stdin_checkpoint("pending changes") is True
    monkeypatch.setattr(builtins, "input", lambda prompt="": "nope")
    assert _stdin_checkpoint("pending changes") is False


# --- the trace survives the writes boundary (surfaced by a live Phase 5 smoke) ---


def test_trace_db_survives_gates_and_stays_out_of_commits(repo, tmp_path):
    """In a target repo without a .gitignore, data/factory.db is untracked — the
    plan/build gates' rollback and the commits' ``git add -A`` used to destroy it
    or land it in the user's history. It must survive both, intact and readable."""
    db_path = repo / "data" / "factory.db"           # the trace lives INSIDE the target repo
    conn = connect(db_path)
    from factory.__main__ import _trace_exemptions
    exempt = _trace_exemptions(repo, db_path)        # the real helper, not a hand copy
    assert exempt == {"data/factory.db", "data/factory.db-wal", "data/factory.db-shm"}

    def approve(pending):
        # the trace plumbing must never be shown as something the human approves
        assert "data/factory.db" not in pending
        return True

    accepted, reason = run_plan_build_test(
        conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
        repo_dir=repo, agent_fn=StubAgent([PLAN_JSON, BUILD_JSON], side_effects=[write_plan, write_feature]),
        test_fn=StubTests([0]), checkpoint_fn=approve, exempt_paths=exempt,
    )
    assert accepted, reason

    # A FRESH connection sees the full trace — the file was never unlinked.
    fresh = connect(db_path)
    phases = [r["name"] for r in fresh.execute("SELECT name FROM phases ORDER BY seq").fetchall()]
    assert phases == ["plan", "plan_gates", "handoff", "commit:plan", "build:r1", "test:r1", "checkpoint", "commit"]
    assert fresh.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 0

    # Neither commit carries trace plumbing.
    for sha in subprocess.run(
        ["git", "log", "--format=%H", "-2"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.split():
        names = subprocess.run(
            ["git", "show", "--name-only", "--format=", sha], cwd=str(repo),
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        assert all(not n.startswith("data/") for n in names), names
