"""Tests for the standard chain: plan → handoff → commit:plan → build/test → commit."""

import subprocess
from pathlib import Path

from factory.agent_pi import AgentResult
from factory.chains import run_plan_build_test
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
    assert phases == ["plan", "handoff", "commit:plan", "build:r1", "test:r1", "commit"]

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
