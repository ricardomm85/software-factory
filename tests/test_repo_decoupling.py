"""Tests for roster/target decoupling: one trace, many repos, secrets at home."""

import sqlite3
import subprocess
from pathlib import Path

from factory.agent_pi import AgentResult, run_pi
from factory.__main__ import _trace_exemptions
from factory.db import connect
from factory.run import Run, call_agent


def test_migrated_db_gains_the_repo_column(tmp_path):
    db = tmp_path / "factory.db"
    legacy = sqlite3.connect(db)
    legacy.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, request TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'running', agent TEXT, model TEXT,"
        " started_at TEXT NOT NULL, ended_at TEXT);"
        "INSERT INTO sessions (id, request, status, agent, model, started_at)"
        " VALUES ('old', 'r', 'failed', 'a', 'm', 'now');"
    )
    legacy.commit()
    legacy.close()

    fresh = connect(db)
    cols = [r[1] for r in fresh.execute("PRAGMA table_info(sessions)")]
    assert "repo" in cols
    row = fresh.execute("SELECT id, repo FROM sessions").fetchone()
    assert row["id"] == "old" and row["repo"] == ""   # pre-coupling runs ran at home


def test_run_create_records_the_target_repo(tmp_path):
    conn = connect(tmp_path / "factory.db")
    run = Run.create(conn, "ship it", agent="builder", model="m", repo="/tmp/target")
    assert conn.execute("SELECT repo FROM sessions WHERE id = ?", (run.id,)).fetchone()[0] == "/tmp/target"
    assert Run.create(conn, "at home", agent="b", model="m").id  # repo defaults to ''


def test_pi_works_in_the_target_repo_but_secrets_come_from_home(monkeypatch, tmp_path):
    seen_env_dir = {}
    captured = {}

    def fake_load_env(path):
        seen_env_dir["path"] = path
        return {"ZAI_API_KEY": "from-home"}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr("factory.agent_pi.load_env", fake_load_env)
    monkeypatch.setattr("factory.agent_pi.subprocess.run", fake_run)
    result = run_pi("p", model="m", repo_dir=tmp_path / "target", env_dir=tmp_path / "home")
    assert result.exit_code == 0
    assert seen_env_dir["path"] == tmp_path / "home"          # secrets from the roster's home
    assert captured["cwd"] == str(tmp_path / "target")        # work in the target repo
    assert captured["env"]["ZAI_API_KEY"] == "from-home"


def test_env_dir_defaults_to_repo_dir(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr("factory.agent_pi.load_env", lambda p: seen.__setitem__("path", p) or {})
    monkeypatch.setattr(
        "factory.agent_pi.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    run_pi("p", model="m", repo_dir=tmp_path / "solo")
    assert seen["path"] == tmp_path / "solo"                  # unchanged behavior without --repo


def test_call_agent_forwards_env_dir(tmp_path):
    captured = {}

    def stub(prompt, model, session_id=None, **kwargs):
        captured.update(kwargs)
        return AgentResult(
            exit_code=0,
            stdout='{"status": "success", "summary": "ok", "artifacts": [], "changed_files": [], "commit_message": ""}',
            stderr="",
            duration_ms=1,
            command=[],
        )

    conn = connect(tmp_path / "factory.db")
    run = Run.create(conn, "r", agent="a", model="m")
    with run.phase("p") as phase:
        call_agent(run, phase, "prompt", {"name": "a", "model": "m"}, agent_fn=stub,
                   repo_dir=tmp_path / "target", env_dir=tmp_path / "home")
    assert captured["repo_dir"] == tmp_path / "target"
    assert captured["env_dir"] == tmp_path / "home"


def test_prompt_honors_top_level_repo(monkeypatch, tmp_path):
    """`factory --repo X prompt ...` runs the agent in X with secrets at home —
    the lone raw command must honor the flag like every other agent caller."""
    import factory.__main__ as cli

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "defaults:\n  coding_agent: pi\n  model: pi/m\n"
        "agents:\n  - name: a\n    purpose: p\n"
    )
    captured = {}

    def fake_run_pi(prompt, **kwargs):
        captured.update(kwargs)
        return AgentResult(exit_code=0, stdout="ok", stderr="", duration_ms=1, command=[])

    monkeypatch.setattr(cli, "run_pi", fake_run_pi)
    target = tmp_path / "target"
    target.mkdir()
    rc = cli.main(["--repo", str(target), "prompt", "hi", "--config", str(cfg)])
    assert rc == 0
    assert captured["repo_dir"] == target.resolve()   # work happens in the target
    assert captured["env_dir"] == tmp_path            # secrets stay at the roster's home


def test_trace_at_home_is_not_exempt_in_the_target_repo(tmp_path):
    # With a central trace, the target repo's git clean can't touch it: nothing
    # to exempt — and the exemption math must say so, not guess.
    home = tmp_path / "factory-home"
    target = tmp_path / "target"
    target.mkdir()
    assert _trace_exemptions(target, home / "data" / "factory.db") == set()


def test_home_runs_record_a_resolved_path(tmp_path):
    # "." (the default --config dir) must not leak into the trace as the repo
    # value — home runs and --repo runs have to be comparable in one column.
    import types

    from factory.__main__ import _resolve_repo

    args = types.SimpleNamespace(config=Path("config.yaml"), repo=None)
    repo_dir, _ = _resolve_repo(args)
    assert repo_dir == Path("config.yaml").parent.resolve()


def test_build_review_refuses_a_non_git_repo(tmp_path, capsys):
    import factory.__main__ as cli

    plain = tmp_path / "plain"
    plain.mkdir()  # no .git anywhere
    cfg = tmp_path / "config.yaml"
    cfg.write_text("defaults:\n  coding_agent: pi\n  model: zai/m\nagents:\n  - name: a\n    purpose: p\n")
    rc = cli.main(["build-review", "--repo", str(plain), "--config", str(cfg)])
    assert rc == 2
    assert "not a git repository" in capsys.readouterr().err
