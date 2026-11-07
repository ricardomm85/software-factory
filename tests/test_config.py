"""Tests for the roster config loader and validator."""

import textwrap

import pytest

from factory.config import ConfigError, load_config, resolve_agent

VALID = """
defaults:
  coding_agent: pi
  model: zai/glm-5.3-flash
  thinking: medium
agents:
  - name: scout
    purpose: recon
  - name: planner
    model: zai/glm-5.2
    thinking: high
    purpose: plan
"""


def write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_valid_config_loads_and_resolves(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    planner = resolve_agent(cfg, "planner")
    assert planner["model"] == "zai/glm-5.2"
    assert planner["thinking"] == "high"
    scout = resolve_agent(cfg, "scout")
    assert scout["model"] == "zai/glm-5.3-flash"  # inherited from defaults
    assert scout["thinking"] == "medium"


def test_resolve_defaults_with_none(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert resolve_agent(cfg, None)["model"] == "zai/glm-5.3-flash"


def test_bare_model_id_is_rejected(tmp_path):
    text = VALID.replace("zai/glm-5.3-flash", "glm-5.3-flash")
    with pytest.raises(ConfigError, match="provider/model-id"):
        load_config(write(tmp_path, text))


def test_unknown_coding_agent_is_rejected(tmp_path):
    text = VALID.replace("coding_agent: pi", "coding_agent: claude_code")
    with pytest.raises(ConfigError, match="'pi'"):
        load_config(write(tmp_path, text))


def test_duplicate_agent_names_are_rejected(tmp_path):
    text = VALID.replace("- name: planner", "- name: scout\n    purpose: dup\n  - name: planner")
    with pytest.raises(ConfigError, match="duplicate agent name"):
        load_config(write(tmp_path, text))


def test_unknown_agent_name_fails_with_known_list(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    with pytest.raises(ConfigError, match="scout, planner"):
        resolve_agent(cfg, "reviewer")


def test_bad_thinking_level_is_rejected(tmp_path):
    text = VALID.replace("thinking: medium", "thinking: turbo")
    with pytest.raises(ConfigError, match="thinking"):
        load_config(write(tmp_path, text))


def test_bare_off_is_rejected_with_quoting_hint(tmp_path):
    text = VALID.replace("thinking: medium", "thinking: off")
    with pytest.raises(ConfigError, match="quote the value"):
        load_config(write(tmp_path, text))


def test_quoted_off_is_accepted(tmp_path):
    text = VALID.replace("thinking: medium", "thinking: 'off'")
    cfg = load_config(write(tmp_path, text))
    assert resolve_agent(cfg, None)["thinking"] == "off"
