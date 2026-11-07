"""Tests for the repo-local secret loader."""

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


def test_load_env_reads_repo_env(tmp_path):
    (tmp_path / ".env").write_text("ZAI_API_KEY=abc123\n", encoding="utf-8")
    assert load_env(tmp_path) == {"ZAI_API_KEY": "abc123"}


def test_load_env_missing_repo_env_is_empty(tmp_path):
    assert load_env(tmp_path) == {}
