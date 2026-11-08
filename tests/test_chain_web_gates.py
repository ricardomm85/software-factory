"""Tests for the web human gates in the standard chain (Phase 7) and the
deliver phase (Phase 8): questions, plan approval, on_run, deliver_fn."""

import subprocess
from pathlib import Path

import pytest

from factory.agent_pi import AgentResult
from factory.chains import CheckpointDeclined, run_plan_build_test
from factory.db import connect

PLANNER_CFG = {"name": "planner", "model": "zai/glm-5.3-flash", "thinking": "high"}
BUILDER_CFG = {"name": "builder", "model": "zai/glm-5.3-flash", "thinking": "medium"}

QUESTIONS_JSON = (
    '{"status": "success", "summary": "need one clarification", '
    '"artifacts": [], "plan_path": "", '
    '"questions": ["Which format: json or yaml?"], "notes_for_next_agent": ""}'
)
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
    def __init__(self, outputs, side_effects=None):
        self.outputs = list(outputs)
        self.side_effects = list(side_effects or [])
        self.calls = []

    def __call__(self, prompt, model, session_id=None, on_event=None, repo_dir=None, **kwargs):
        self.calls.append({"prompt": prompt, "session_id": session_id})
        effect = self.side_effects.pop(0) if self.side_effects else None
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
    (repo_dir / "specs").mkdir(exist_ok=True)
    (repo_dir / "specs" / "plan.md").write_text("# Plan\n- create feature.txt saying 'feature'\n")


def write_feature(repo_dir: Path):
    (repo_dir / "feature.txt").write_text("feature\n")


def _commits(repo):
    out = subprocess.run(
        ["git", "log", "--format=%s"], cwd=str(repo), capture_output=True, text=True, check=True
    )
    return out.stdout.strip().splitlines()


def _phases(conn):
    return [r["name"] for r in conn.execute("SELECT name FROM phases ORDER BY seq").fetchall()]


def test_questions_pause_then_same_session_resume(repo, tmp_path):
    agent = StubAgent([QUESTIONS_JSON, PLAN_JSON, BUILD_JSON], side_effects=[None, write_plan, write_feature])
    tests = StubTests([0])
    asked = []

    def answer(questions):
        asked.extend(questions)
        return "json please"

    conn = connect(tmp_path / "factory.db")
    accepted, reason = run_plan_build_test(
        conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
        repo_dir=repo, agent_fn=agent, test_fn=tests, questions_fn=answer,
    )
    assert accepted, reason
    assert asked == ["Which format: json or yaml?"]
    # The planner resumed its own session — corrections never cold-restart.
    assert agent.calls[0]["session_id"] == agent.calls[1]["session_id"] != agent.calls[2]["session_id"]
    assert "json please" in agent.calls[1]["prompt"]
    events = [r["type"] for r in conn.execute("SELECT type FROM events ORDER BY id").fetchall()]
    assert "planner_questions" in events and "planner_answers" in events


def test_questions_with_nobody_attached_fail_the_plan_gates(repo, tmp_path):
    agent = StubAgent([QUESTIONS_JSON])
    tests = StubTests([])
    conn = connect(tmp_path / "factory.db")
    accepted, reason = run_plan_build_test(
        conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
        repo_dir=repo, agent_fn=agent, test_fn=tests,  # no questions_fn: the CLI
    )
    assert not accepted
    assert "no one is wired to answer" in reason
    assert len(agent.calls) == 1  # nobody to answer: one call, honest failure


def test_plan_approval_approved(repo, tmp_path):
    agent = StubAgent([PLAN_JSON, BUILD_JSON], side_effects=[write_plan, write_feature])
    tests = StubTests([0])
    seen = []

    def approve(plan_rel, plan_text):
        seen.append((plan_rel, plan_text))
        return None

    conn = connect(tmp_path / "factory.db")
    accepted, reason = run_plan_build_test(
        conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
        repo_dir=repo, agent_fn=agent, test_fn=tests, plan_approval_fn=approve,
    )
    assert accepted, reason
    assert seen == [("specs/plan.md", "# Plan\n- create feature.txt saying 'feature'\n")]
    assert "plan_approved" in [r["type"] for r in conn.execute("SELECT type FROM events").fetchall()]
    assert agent.calls[1]["session_id"].endswith("-builder")


def test_plan_correction_reenters_the_same_session(repo, tmp_path):
    agent = StubAgent(
        [PLAN_JSON, PLAN_JSON, BUILD_JSON],
        side_effects=[write_plan, write_plan, write_feature],
    )
    tests = StubTests([0])
    decisions = ["rewrite it shorter", None]

    def approve(plan_rel, plan_text):
        return decisions.pop(0)

    conn = connect(tmp_path / "factory.db")
    accepted, reason = run_plan_build_test(
        conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
        repo_dir=repo, agent_fn=agent, test_fn=tests, plan_approval_fn=approve,
    )
    assert accepted, reason
    assert "asked for changes" in agent.calls[1]["prompt"]
    assert "rewrite it shorter" in agent.calls[1]["prompt"]
    assert agent.calls[0]["session_id"] == agent.calls[1]["session_id"]  # same planner session
    events = [r["type"] for r in conn.execute("SELECT type FROM events ORDER BY id").fetchall()]
    assert events.count("plan_correction") == 1


def test_plan_approval_rejection_stops_the_chain(repo, tmp_path):
    agent = StubAgent([PLAN_JSON], side_effects=[write_plan])
    tests = StubTests([])
    conn = connect(tmp_path / "factory.db")

    def approve(plan_rel, plan_text):
        raise CheckpointDeclined("the owner rejected the plan")

    with pytest.raises(CheckpointDeclined):
        run_plan_build_test(
            conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
            repo_dir=repo, agent_fn=agent, test_fn=tests, plan_approval_fn=approve,
        )
    assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "failed"
    assert len(agent.calls) == 1  # the builder never spawned


def test_plan_approval_loop_is_bounded(repo, tmp_path):
    agent = StubAgent([PLAN_JSON] * 5, side_effects=[write_plan] * 5)
    tests = StubTests([])
    conn = connect(tmp_path / "factory.db")

    def approve(plan_rel, plan_text):
        return "still not right"  # never approves

    with pytest.raises(CheckpointDeclined, match="not approved after"):
        run_plan_build_test(
            conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
            repo_dir=repo, agent_fn=agent, test_fn=tests, plan_approval_fn=approve,
            human_rounds=2,
        )
    assert len(agent.calls) == 3  # initial + two corrections: bounded, no infinite loop


def test_questions_loop_is_bounded(repo, tmp_path):
    agent = StubAgent([QUESTIONS_JSON] * 4)
    tests = StubTests([])
    conn = connect(tmp_path / "factory.db")
    accepted, reason = run_plan_build_test(
        conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
        repo_dir=repo, agent_fn=agent, test_fn=tests,
        questions_fn=lambda qs: "answers", human_rounds=2,
    )
    assert not accepted
    assert len(agent.calls) == 3  # initial + two answered rounds, then honest failure
    # The bound was exhausted WITH someone wired — the honest cause is the bound,
    # not missing wiring.
    assert "still asking questions after 2 human rounds" in reason


def test_deliver_phase_runs_after_commit_and_logs_pr(repo, tmp_path):
    agent = StubAgent([PLAN_JSON, BUILD_JSON], side_effects=[write_plan, write_feature])
    tests = StubTests([0])
    delivered = []

    def deliver(run_id, message):
        delivered.append((run_id, message))
        return {"pr_url": "https://github.com/x/y/pull/1", "branch": "factory/abc"}

    conn = connect(tmp_path / "factory.db")
    accepted, reason = run_plan_build_test(
        conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
        repo_dir=repo, agent_fn=agent, test_fn=tests, deliver_fn=deliver,
    )
    assert accepted, reason
    assert len(delivered) == 1
    assert delivered[0][1] == "feat: add feature.txt per plan"
    run_id = conn.execute("SELECT id FROM sessions").fetchone()[0]
    assert delivered[0][0] == run_id
    assert _phases(conn)[-2:] == ["commit", "deliver"]  # deliver rides after the commit
    payload = conn.execute(
        "SELECT payload FROM events WHERE type = 'delivered'"
    ).fetchone()[0]
    assert "pull/1" in payload


def test_no_deliver_fn_means_no_deliver_phase(repo, tmp_path):
    agent = StubAgent([PLAN_JSON, BUILD_JSON], side_effects=[write_plan, write_feature])
    tests = StubTests([0])
    conn = connect(tmp_path / "factory.db")
    accepted, _ = run_plan_build_test(
        conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
        repo_dir=repo, agent_fn=agent, test_fn=tests,
    )
    assert accepted
    assert _phases(conn)[-1] == "commit"  # the CLI chain is unchanged


def test_on_run_fires_once_with_the_created_run(repo, tmp_path):
    agent = StubAgent([PLAN_JSON, BUILD_JSON], side_effects=[write_plan, write_feature])
    tests = StubTests([0])
    seen = []
    conn = connect(tmp_path / "factory.db")
    accepted, _ = run_plan_build_test(
        conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
        repo_dir=repo, agent_fn=agent, test_fn=tests, on_run=seen.append,
    )
    assert accepted
    assert len(seen) == 1
    assert seen[0].id == conn.execute("SELECT id FROM sessions").fetchone()[0]


def test_full_board_chain_questions_approval_and_pr(repo, tmp_path):
    """The whole Phase 7+8 flow in one chain: questions → plan → approval →
    build/test → PR approval → commit → deliver."""
    agent = StubAgent(
        [QUESTIONS_JSON, PLAN_JSON, BUILD_JSON],
        side_effects=[None, write_plan, write_feature],
    )
    tests = StubTests([0])
    answers = []

    def ask(questions):
        answers.extend(questions)
        return "json please"

    approvals = []

    def approve_plan(plan_rel, plan_text):
        approvals.append(plan_rel)
        return None  # the owner approved from the board

    def approve_pr(pending):
        return True  # the owner approved the push

    def deliver(run_id, message):
        return {"pr_url": "https://github.com/x/y/pull/7", "branch": "factory/deadbeef"}

    conn = connect(tmp_path / "factory.db")
    accepted, reason = run_plan_build_test(
        conn, "add a feature file", PLANNER_CFG, BUILDER_CFG, test_command="true",
        repo_dir=repo, agent_fn=agent, test_fn=tests,
        checkpoint_fn=approve_pr, questions_fn=ask, plan_approval_fn=approve_plan,
        deliver_fn=deliver,
    )
    assert accepted, reason
    assert answers == ["Which format: json or yaml?"]
    assert approvals == ["specs/plan.md"]
    phases = _phases(conn)
    assert phases == [
        "plan", "plan_gates", "handoff", "commit:plan", "build:r1", "test:r1",
        "checkpoint", "commit", "deliver",
    ]
    cp = conn.execute(
        "SELECT status FROM phases WHERE name = 'checkpoint'"
    ).fetchone()[0]
    assert cp == "pass"
