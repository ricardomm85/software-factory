"""Tests for the two-layer secret loader (repo-local .env wins, global fills gaps)."""

import factory.env as env_module
from factory.env import load_env, parse_env


def test_parse_env_ignores_comments_and_blanks(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n\nZAI_API_KEY=abc123\nEMPTY=\nSPACED  =  value  \n",
        encoding="utf-8",
    )
    values = parse_env(path)
    assert values == {"ZAI_API_KEY": "abc123", "EMPTY": "", "SPACED": "value"}


def test_parse_env_missing_file_is_empty(tmp_path):
    assert parse_env(tmp_path / "nope.env") == {}


def test_repo_env_wins_and_global_fills_gaps(tmp_path, monkeypatch):
    global_path = tmp_path / "global.env"
    global_path.write_text("ZAI_API_KEY=global-key\nONLY_GLOBAL=1\n", encoding="utf-8")
    monkeypatch.setattr(env_module, "GLOBAL_ENV_PATH", global_path)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("ZAI_API_KEY=repo-key\n", encoding="utf-8")

    merged = load_env(repo_dir=repo)
    assert merged["ZAI_API_KEY"] == "repo-key"  # repo wins
    assert merged["ONLY_GLOBAL"] == "1"  # global fills gaps


def test_no_repo_env_falls_back_to_global(tmp_path, monkeypatch):
    global_path = tmp_path / "global.env"
    global_path.write_text("ZAI_API_KEY=global-key\n", encoding="utf-8")
    monkeypatch.setattr(env_module, "GLOBAL_ENV_PATH", global_path)

    merged = load_env(repo_dir=tmp_path / "does-not-exist")
    assert merged == {"ZAI_API_KEY": "global-key"}
