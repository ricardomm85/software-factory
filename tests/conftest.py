"""Shared fixtures: a real git repo for chain/gate tests."""

import subprocess
from pathlib import Path

import pytest


@pytest.fixture()
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), capture_output=True, check=True)
    (repo / "README.md").write_text("base\n")
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "."],
        cwd=str(repo), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init"],
        cwd=str(repo), capture_output=True, check=True,
    )
    return repo
