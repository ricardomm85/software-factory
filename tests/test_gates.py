"""Tests for gates: verify claims against real repo state, roll back violations."""

import subprocess
from pathlib import Path

import pytest

from factory.envelope import BuildOutput
from factory.gates import (
    artifacts_exist,
    changed_files,
    diff_matches_claims,
    run_output_gate,
)


def _git(repo: Path, *args: str):
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("base\n")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init")
    return repo


def envelope(**overrides) -> BuildOutput:
    fields = {"status": "success", "summary": "did it", "changed_files": [], "artifacts": []}
    fields.update(overrides)
    return BuildOutput(**fields)


def test_gate_report_violations_and_payload():
    from factory.gates import Check, GateReport

    report = GateReport("g", [Check("a", True, ""), Check("b", False, "bad")])
    assert not report.passed
    assert report.violations() == ["g: b — bad"]
    assert report.to_payload()["passed"] is False


def test_run_output_gate():
    assert run_output_gate(envelope()).passed
    assert not run_output_gate(envelope(status="fail", summary="")).passed


def test_artifacts_exist(tmp_path):
    (tmp_path / "present.md").write_text("content")
    report = artifacts_exist(envelope(artifacts=["present.md", "missing.md", "empty.md"]), tmp_path)
    (tmp_path / "empty.md").write_text("")
    assert not report.passed
    notes = [c.note for c in report.checks if not c.ok]
    assert any("missing" in n for n in notes)


def test_changed_files_sees_all_kinds(git_repo):
    (git_repo / "README.md").write_text("changed\n")           # tracked modified
    (git_repo / "new_untracked.txt").write_text("hi\n")        # untracked
    _git(git_repo, "add", ".")
    (git_repo / "staged.txt").write_text("staged\n")           # staged new file
    _git(git_repo, "add", "staged.txt")
    paths = changed_files(git_repo)
    assert set(paths) == {"README.md", "new_untracked.txt", "staged.txt"}


def test_diff_matches_claims_ok(git_repo):
    (git_repo / "README.md").write_text("changed\n")
    report = diff_matches_claims(envelope(changed_files=["README.md"]), git_repo)
    assert report.passed


def test_diff_matches_claims_rolls_back_unclaimed(git_repo):
    (git_repo / "README.md").write_text("claimed change\n")
    (git_repo / "rogue.txt").write_text("unclaimed\n")
    report = diff_matches_claims(
        envelope(changed_files=["README.md"]), git_repo, rollback=True
    )
    assert not report.passed                      # the violation stays visible
    assert "rogue.txt" in report.checks[0].note   # ...and says it was rolled back
    assert not (git_repo / "rogue.txt").exists()  # untracked: removed
    assert (git_repo / "README.md").read_text() == "claimed change\n"  # claimed: untouched


def test_diff_matches_claims_restores_tracked_unclaimed(git_repo):
    (git_repo / "README.md").write_text("claimed change\n")
    (git_repo / "extra.txt").write_text("new content\n")
    _git(git_repo, "add", "extra.txt")            # staged by the agent: still rolled back
    report = diff_matches_claims(envelope(changed_files=["README.md"]), git_repo, rollback=True)
    assert not report.passed
    assert (git_repo / "README.md").read_text() == "claimed change\n"
    assert not (git_repo / "extra.txt").exists()


def test_declared_but_unchanged_is_note_not_failure(git_repo):
    report = diff_matches_claims(envelope(changed_files=["ghost.txt"]), git_repo)
    assert report.passed
    assert "ghost.txt" in report.checks[1].note
