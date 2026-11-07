"""Tests for pi command construction (pure — no subprocess, no network)."""

import pytest

from factory.agent_pi import build_pi_command


def test_print_mode_defaults():
    cmd = build_pi_command("hello", model="zai/glm-5.3-flash")
    assert cmd == ["pi", "-p", "--model", "zai/glm-5.3-flash", "hello"]


def test_json_mode_with_session_and_thinking():
    cmd = build_pi_command(
        "hello",
        model="zai/glm-5.2",
        session_id="run-abc123",
        mode="json",
        thinking="high",
    )
    assert cmd[:4] == ["pi", "--mode", "json", "-p"]  # -p keeps json mode non-interactive
    assert "--session-id" in cmd and cmd[cmd.index("--session-id") + 1] == "run-abc123"
    assert "--thinking" in cmd and cmd[cmd.index("--thinking") + 1] == "high"
    assert cmd[cmd.index("--model") + 1] == "zai/glm-5.2"
    assert cmd[-1] == "hello"  # the prompt is always the last argv


def test_invalid_mode_raises():
    with pytest.raises(ValueError, match="mode"):
        build_pi_command("hello", model="zai/glm-5.3-flash", mode="rpc")
