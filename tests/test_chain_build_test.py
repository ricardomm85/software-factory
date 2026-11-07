"""Tests for the build-test chain: bounded rounds, gates, rollback, same-session fixes."""

from pathlib import Path

import pytest

from factory.agent_pi import AgentResult
from factory.chains import run_build_test
from factory.db import connect

AGENT_CFG = {"name": "builder", "model": "zai/glm-5.3-flash", "thinking": "medium"}
VALID = '{"status": "success", "summary": "done", "artifacts": [], "changed_files": ["feature.txt"], "commit_message": "add feature"}'


def envelope_json(changed_files='["feature.txt"]', status="success"):
    return (
        '{"status": "%s", "summary": "done", "artifacts": [], '
        '"changed_files": %s, "commit_message": "add feature"}' % (status, changed_files)
    )


class StubBuilder:
    """Stands in for run_pi_streaming; applies a side effect per call, in-session."""

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


def write_claimed_feature(repo_dir: Path):
    (repo_dir / "feature.txt").write_text("feature\n")


def test_green_run_one_round(repo, tmp_path):
    builder = StubBuilder([envelope_json()], side_effects=[write_claimed_feature])
    tests = StubTests([0])
    conn = connect(tmp_path / "factory.db")
    assert run_build_test(
        conn, "add a feature", AGENT_CFG, test_command="true",
        repo_dir=repo, agent_fn=builder, test_fn=tests,
    )
    assert tests.runs == 1
    assert builder.calls[0]["session_id"].endswith("-builder")
    assert "## Report" in builder.calls[0]["prompt"]
    run_id = conn.execute("SELECT id FROM sessions").fetchone()[0]
    assert conn.execute("SELECT status FROM sessions WHERE id = ?", (run_id,)).fetchone()[0] == "accepted"


def test_red_tests_trigger_same_session_fix_round(repo, tmp_path):
    builder = StubBuilder(
        [envelope_json(), envelope_json()],
        side_effects=[write_claimed_feature, write_claimed_feature],
    )
    tests = StubTests([1, 0])
    conn = connect(tmp_path / "factory.db")
    assert run_build_test(
        conn, "add a feature", AGENT_CFG, test_command="true",
        repo_dir=repo, max_rounds=3, agent_fn=builder, test_fn=tests,
    )
    assert len(builder.calls) == 2
    assert builder.calls[0]["session_id"] == builder.calls[1]["session_id"]  # same session
    assert "test suite failed" in builder.calls[1]["prompt"]                 # test_correction prompt
    assert tests.runs == 2


def test_gate_violation_rolls_back_then_fixes(repo, tmp_path):
    def write_rogue(repo_dir: Path):
        (repo_dir / "rogue.txt").write_text("unclaimed\n")  # will be rolled back by the gate
        (repo_dir / "feature.txt").write_text("feature\n")

    builder = StubBuilder(
        ['{"status": "success", "summary": "done", "artifacts": [], "changed_files": [], "commit_message": "x"}',
         envelope_json()],
        side_effects=[write_rogue, write_claimed_feature],
    )
    tests = StubTests([0])
    conn = connect(tmp_path / "factory.db")
    assert run_build_test(
        conn, "add a feature", AGENT_CFG, test_command="true",
        repo_dir=repo, agent_fn=builder, test_fn=tests,
    )
    assert len(builder.calls) == 2
    assert "violated acceptance gates" in builder.calls[1]["prompt"]
    assert not (repo / "rogue.txt").exists()          # rolled back in round 1
    assert (repo / "feature.txt").read_text() == "feature\n"  # round 2 work survives


def test_exhausted_rounds_settle_failed(repo, tmp_path):
    tests = StubTests([1, 1])
    builder = StubBuilder(
        [envelope_json(), envelope_json()],
        side_effects=[write_claimed_feature, write_claimed_feature],
    )
    conn = connect(tmp_path / "factory.db")
    assert not run_build_test(
        conn, "add a feature", AGENT_CFG, test_command="true",
        repo_dir=repo, max_rounds=2, agent_fn=builder, test_fn=tests,
    )
    status = conn.execute("SELECT status FROM sessions").fetchone()[0]
    assert status == "failed"
    rejection = conn.execute("SELECT payload FROM events WHERE type = 'run_rejected'").fetchone()
    assert rejection is not None and "round 2" in rejection[0]
    assert tests.runs == 2


def test_agent_declined_stops_the_chain_without_nagging(repo, tmp_path):
    fail_envelope = (
        '{"status": "fail", "summary": "impossible: the repo has no test suite", '
        '"artifacts": [], "changed_files": [], "commit_message": ""}'
    )
    builder = StubBuilder([fail_envelope])
    tests = StubTests([])
    conn = connect(tmp_path / "factory.db")
    assert not run_build_test(
        conn, "do the impossible", AGENT_CFG, test_command="true",
        repo_dir=repo, agent_fn=builder, test_fn=tests,
    )
    assert len(builder.calls) == 1  # the agent decided — never re-prompted
    assert tests.runs == 0          # nothing to test
    declined = conn.execute("SELECT payload FROM events WHERE type = 'agent_declined'").fetchone()
    assert declined is not None and "impossible" in declined[0]


def test_run_test_command_timeout_is_a_red_result_not_a_crash(tmp_path):
    from factory.chains import run_test_command

    result = run_test_command("sleep 30", tmp_path, timeout_s=1)
    assert result.exit_code == 124
    assert "timed out" in result.output_tail
