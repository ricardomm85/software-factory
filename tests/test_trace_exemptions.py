"""Tests for _trace_exemptions: the trace must never count as agent debris."""

from pathlib import Path

from factory.__main__ import _trace_exemptions


def test_relative_paths_and_wal_sidecars(tmp_path):
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    db = repo / "data" / "factory.db"
    assert _trace_exemptions(repo, db) == {
        "data/factory.db",
        "data/factory.db-wal",
        "data/factory.db-shm",
    }


def test_nested_data_dir(tmp_path):
    repo = tmp_path / "repo"
    (repo / "var" / "trace").mkdir(parents=True)
    db = repo / "var" / "trace" / "factory.db"
    assert _trace_exemptions(repo, db) == {
        "var/trace/factory.db",
        "var/trace/factory.db-wal",
        "var/trace/factory.db-shm",
    }


def test_trace_outside_the_repo_is_not_exempt(tmp_path):
    # A db outside repo_dir can't be touched by the repo's git clean/add — the
    # empty set is "not applicable", not a fallback failure.
    assert _trace_exemptions(tmp_path / "repo", tmp_path / "elsewhere" / "factory.db") == set()


def test_unresolved_symlink_like_path_still_yields_posix_relative(tmp_path):
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    # resolve() on both sides keeps the comparison honest for absolute inputs.
    db = Path(repo) / "data" / "factory.db"
    result = _trace_exemptions(Path(repo.resolve()), db)
    assert "data/factory.db" in result
