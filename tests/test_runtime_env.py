"""Tests for the factory's runtime environment: defaults.path_prepend pin + serve preflight.

Two guards against the same class of failure: a daemon launched from a shell
whose PATH resolves an old node must not accept tasks every agent dies on.
"""

import os
from types import SimpleNamespace

import pytest

from factory.agent_pi import preflight_agent_runtime
from factory.config import ConfigError, apply_runtime_env, load_config, path_prepend_dirs

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


def with_knob(entry: str) -> str:
    return BASE.replace("  test_command: echo ok", f"  test_command: echo ok\n{entry}")


def test_path_prepend_accepts_list_and_single_string(tmp_path):
    as_list = load(tmp_path, with_knob("  path_prepend:\n    - /opt/tools/bin\n    - ~/bin"))
    assert path_prepend_dirs(as_list)[0] == "/opt/tools/bin"
    assert path_prepend_dirs(as_list)[1] == os.path.expanduser("~/bin")

    as_string = load(tmp_path, with_knob("  path_prepend: /opt/tools/bin"))
    assert path_prepend_dirs(as_string) == ["/opt/tools/bin"]


@pytest.mark.parametrize("entry", ["123", "''", "[' ', 'x']"])
def test_path_prepend_rejects_garbage(tmp_path, entry):
    with pytest.raises(ConfigError, match="path_prepend"):
        load(tmp_path, with_knob(f"  path_prepend: {entry}"))


def test_path_prepend_empty_list_means_off(tmp_path):
    cfg = load(tmp_path, with_knob("  path_prepend: []"))
    assert path_prepend_dirs(cfg) == []


def test_apply_runtime_env_prepends_to_path(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    cfg = {"defaults": {"path_prepend": ["/opt/a", "/opt/b"]}}
    apply_runtime_env(cfg)
    assert os.environ["PATH"] == "/opt/a:/opt/b:/usr/bin:/bin"


def test_apply_runtime_env_without_knob_is_noop(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    apply_runtime_env({"defaults": {}})
    assert os.environ["PATH"] == "/usr/bin:/bin"


def _patch_lookup(monkeypatch, which: dict, node_version: str = "v22.23.2"):
    monkeypatch.setattr("factory.agent_pi.shutil.which", lambda name: which.get(name))
    monkeypatch.setattr(
        "factory.agent_pi.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=node_version + "\n", stderr=""),
    )


def test_preflight_passes_with_pinned_node(monkeypatch):
    _patch_lookup(monkeypatch, {"pi": "/opt/nvm/bin/pi", "node": "/opt/nvm/bin/node"})
    ok, message = preflight_agent_runtime()
    assert ok
    assert "/opt/nvm/bin/pi" in message


def test_preflight_fails_without_pi(monkeypatch):
    _patch_lookup(monkeypatch, {"node": "/usr/bin/node"})
    ok, message = preflight_agent_runtime()
    assert not ok
    assert "'pi' not found" in message


def test_preflight_fails_without_node(monkeypatch):
    _patch_lookup(monkeypatch, {"pi": "/opt/nvm/bin/pi"})
    ok, message = preflight_agent_runtime()
    assert not ok
    assert "'node' not found" in message


def test_preflight_fails_on_old_node(monkeypatch):
    _patch_lookup(monkeypatch, {"pi": "/usr/bin/pi", "node": "/usr/bin/node"}, node_version="v20.19.4")
    ok, message = preflight_agent_runtime()
    assert not ok
    assert "too old" in message
    assert "path_prepend" in message
