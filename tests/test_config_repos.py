"""Tests for the roster's repos section and the global run cap (Phase 6)."""

import pytest

from factory.config import ConfigError, load_config, resolve_repos

BASE = """
defaults:
  coding_agent: pi
  model: pi/m
  test_command: echo ok
agents:
  - name: a
    purpose: p
"""


def load(tmp_path, text: str) -> dict:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return load_config(path)


def test_repos_section_is_optional(tmp_path):
    cfg = load(tmp_path, BASE)
    assert resolve_repos(cfg) == []


def test_valid_repos_resolve_paths_and_fallback_test_command(tmp_path):
    cfg = load(tmp_path, BASE + (
        "repos:\n"
        "  - name: main-app\n"
        "    path: ~/Projects/main-app\n"
        "  - name: edge-app\n"
        "    path: relative/app\n"
        "    test_command: npm test\n"
    ))
    repos = resolve_repos(cfg)
    assert repos[0]["name"] == "main-app"
    assert repos[0]["test_command"] == "echo ok"          # falls back to the default
    assert repos[1]["test_command"] == "npm test"         # per-repo override
    assert repos[1]["path"].name == "app"                 # relative paths resolve too
    assert repos[0]["path"].is_absolute()


def test_repos_validation_rejects_bad_entries(tmp_path):
    with pytest.raises(ConfigError, match="must be a mapping"):
        load(tmp_path, BASE + "repos:\n  - just-a-string\n")
    with pytest.raises(ConfigError, match="non-empty string 'name'"):
        load(tmp_path, BASE + "repos:\n  - path: /x\n")
    with pytest.raises(ConfigError, match="non-empty string 'path'"):
        load(tmp_path, BASE + "repos:\n  - name: x\n")
    with pytest.raises(ConfigError, match="test_command"):
        load(tmp_path, BASE + "repos:\n  - name: x\n    path: /x\n    test_command: '   '\n")
    with pytest.raises(ConfigError, match="duplicate repo name"):
        load(tmp_path, BASE + "repos:\n  - name: x\n    path: /x\n  - name: x\n    path: /y\n")


def test_max_concurrent_runs_defaults_to_one_and_validates(tmp_path):
    cfg = load(tmp_path, BASE)
    assert cfg["defaults"].get("max_concurrent_runs", 1) == 1
    with pytest.raises(ConfigError, match="integer >= 1"):
        load(tmp_path, BASE.replace("  test_command: echo ok", "  test_command: echo ok\n  max_concurrent_runs: 0"))
    with pytest.raises(ConfigError, match="integer >= 1"):
        load(tmp_path, BASE.replace("  test_command: echo ok", "  test_command: echo ok\n  max_concurrent_runs: true"))
    with pytest.raises(ConfigError, match="integer >= 1"):
        load(tmp_path, BASE.replace("  test_command: echo ok", "  test_command: echo ok\n  max_concurrent_runs: '2'"))
    ok = load(tmp_path, BASE.replace("  test_command: echo ok", "  test_command: echo ok\n  max_concurrent_runs: 3"))
    assert ok["defaults"]["max_concurrent_runs"] == 3


def test_agent_timeout_s_defaults_to_300_and_validates(tmp_path):
    cfg = load(tmp_path, BASE)
    assert cfg["defaults"].get("agent_timeout_s", 300) == 300
    with pytest.raises(ConfigError, match="integer >= 1"):
        load(tmp_path, BASE.replace("  test_command: echo ok", "  test_command: echo ok\n  agent_timeout_s: 0"))
    with pytest.raises(ConfigError, match="integer >= 1"):
        load(tmp_path, BASE.replace("  test_command: echo ok", "  test_command: echo ok\n  agent_timeout_s: 'soon'"))
    ok = load(tmp_path, BASE.replace("  test_command: echo ok", "  test_command: echo ok\n  agent_timeout_s: 600"))
    assert ok["defaults"]["agent_timeout_s"] == 600
