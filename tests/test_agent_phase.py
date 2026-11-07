"""Tests for the acceptance loop: envelope retries stay in the same session."""

import pytest

from factory.agent_pi import AgentResult
from factory.db import connect
from factory.envelope import RunOutput
from factory.run import AgentPhaseError, Run, call_agent

VALID = '{"status": "success", "summary": "done", "artifacts": [], "notes_for_next_agent": ""}'
AGENT_CFG = {"name": "builder", "model": "zai/glm-5.3-flash", "thinking": "medium"}


class StubAgent:
    """Stands in for run_pi_streaming; records every call."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, prompt, model, session_id=None, thinking=None, on_event=None, **kwargs):
        self.calls.append({"prompt": prompt, "session_id": session_id})
        if on_event is not None:
            on_event({"type": "agent_start"})  # mirror run_pi_streaming's live-feed contract
        return AgentResult(exit_code=0, stdout=self.outputs.pop(0), stderr="", duration_ms=1, command=[])


@pytest.fixture()
def run(tmp_path):
    conn = connect(tmp_path / "factory.db")
    return Run.create(conn, request="build it", agent="builder", model="zai/glm-5.3-flash")


def test_success_on_first_attempt(run):
    stub = StubAgent([VALID])
    with run.phase(name="run", owner="builder") as phase:
        envelope = call_agent(run, phase, "do it", AGENT_CFG, agent_fn=stub)
    assert envelope.status == "success"
    assert len(stub.calls) == 1
    assert stub.calls[0]["session_id"] == f"{run.id}-builder"
    assert "## Report" in stub.calls[0]["prompt"]  # contract rides in the prompt


def test_invalid_json_corrected_in_same_session(run):
    stub = StubAgent(["oops, no json here", VALID])
    with run.phase(name="run", owner="builder") as phase:
        envelope = call_agent(run, phase, "do it", AGENT_CFG, agent_fn=stub)
    assert envelope.status == "success"
    assert len(stub.calls) == 2
    first, second = stub.calls
    assert first["session_id"] == second["session_id"]  # never cold-restart
    assert "not a valid envelope" in second["prompt"] and "## Report" in second["prompt"]


def test_all_attempts_exhausted_raises(run):
    stub = StubAgent(["garbage", "still garbage", "nope"])
    with pytest.raises(AgentPhaseError, match="3 attempts"):
        with run.phase(name="run", owner="builder") as phase:
            call_agent(run, phase, "do it", AGENT_CFG, max_attempts=3, agent_fn=stub)
    assert len(stub.calls) == 3


def test_envelope_status_fail_is_returned_not_retried(run):
    fail = '{"status": "fail", "summary": "could not do it", "artifacts": [], "notes_for_next_agent": "db locked"}'
    stub = StubAgent([fail])
    with run.phase(name="run", owner="builder") as phase:
        envelope = call_agent(run, phase, "do it", AGENT_CFG, agent_fn=stub)
    assert envelope.status == "fail"
    assert envelope.notes_for_next_agent == "db locked"
    assert len(stub.calls) == 1  # the agent decided; do not nag it


def test_events_trace_attempts_and_corrections(run):
    stub = StubAgent(["bad", VALID])
    with run.phase(name="run", owner="builder") as phase:
        call_agent(run, phase, "do it", AGENT_CFG, agent_fn=stub)
    types = {r["type"] for r in run.conn.execute("SELECT type FROM events").fetchall()}
    assert {"attempt", "correction", "pi_event", "envelope_invalid", "envelope_ok"} <= types
