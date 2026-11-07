"""Gates: verify claims, never predict them.

A gate runs AFTER the agent finished, against the envelope's own declarations
and the actual repo state. One ``Check`` per thing examined, so a green gate
tells you exactly what it verified. Unauthorized changes are rolled back, not
argued with.
"""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .envelope import EnvelopeBase


@dataclass
class Check:
    item: str
    ok: bool
    note: str = ""


@dataclass
class GateReport:
    gate: str
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks)

    def violations(self) -> list[str]:
        return [f"{self.gate}: {c.item} — {c.note}" for c in self.checks if not c.ok]

    def to_payload(self) -> dict:
        return {
            "gate": self.gate,
            "passed": self.passed,
            "checks": [{"item": c.item, "ok": c.ok, "note": c.note} for c in self.checks],
        }


def _git(*args: str, repo_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo_dir), capture_output=True, text=True, check=False
    )


def changed_files(repo_dir: Path) -> list[str]:
    """Working-tree changes vs HEAD: modified, staged, untracked, and rename sides.

    Parsed from ``--porcelain -z``: NUL-delimited raw paths, so no quoting or
    escaping surprises. A staged rename lists BOTH sides — the new path and the
    original it replaced — so the rollback can undo a rename it didn't sanction.
    """
    out = _git("status", "--porcelain", "-z", repo_dir=repo_dir).stdout
    entries = [e for e in out.split("\0") if e]
    paths: list[str] = []
    i = 0
    while i < len(entries):
        status, path = entries[i][:2], entries[i][3:]
        i += 1
        if status and status[0] in "RC" and i < len(entries):
            paths.append(entries[i])  # the original path a rename/copy replaced
            i += 1
        if path:
            paths.append(path)
    return paths


def run_output_gate(envelope: EnvelopeBase) -> GateReport:
    """The envelope itself: success status and a summary a human can read."""
    checks = [
        Check("envelope.status == success", envelope.status == "success", f"status={envelope.status!r}"),
        Check("envelope.summary is non-empty", bool(envelope.summary.strip()), "empty summary"),
    ]
    return GateReport("run_output", checks)


def artifacts_exist(envelope: EnvelopeBase, repo_dir: Path) -> GateReport:
    """Every declared artifact exists, is non-empty, and stays inside the repo."""
    repo_root = repo_dir.resolve()
    checks = []
    for rel in envelope.artifacts:
        path = (repo_dir / rel).resolve()
        inside = path.is_relative_to(repo_root)
        ok = inside and path.is_file() and path.stat().st_size > 0
        if not inside:
            note = "outside the repo — not a valid artifact"
        elif not ok:
            note = "missing or empty"
        else:
            note = ""
        checks.append(Check(f"artifact exists, non-empty, in-repo: {rel}", ok, note))
    if not envelope.artifacts:
        checks.append(Check("artifacts declared", True, "none declared — nothing to verify"))
    return GateReport("artifacts_exist", checks)


def diff_matches_claims(envelope: EnvelopeBase, repo_dir: Path, rollback: bool = False) -> GateReport:
    """Every actually-changed file must be declared in ``changed_files``.

    Unclaimed changes are rolled back (tracked files restored, untracked files
    removed) when ``rollback=True`` — the check verifies the git operations
    actually succeeded instead of asserting they did. Declared-but-unchanged
    files are a warning note, not a failure — agents may reconsider a plan
    mid-flight.
    """
    claimed = set(envelope.changed_files)
    pre = set(changed_files(repo_dir))
    unclaimed = sorted(pre - claimed)

    rollback_errors: list[str] = []
    if unclaimed and rollback:
        def _in_index(path: str) -> bool:
            return _git("ls-files", "--error-unmatch", path, repo_dir=repo_dir).returncode == 0

        def _in_head(path: str) -> bool:
            return _git("cat-file", "-e", f"HEAD:{path}", repo_dir=repo_dir).returncode == 0

        def _rollback(args: list[str], label: str) -> None:
            res = _git(*args, repo_dir=repo_dir)
            if res.returncode != 0:
                rollback_errors.append(f"{label} failed: {res.stderr.strip()[:200]}")

        # Three disjoint fates, each with its own git op:
        #   in the index (modified, staged-new, rename's new side) -> restore
        #   gone from the index but known to HEAD (a rename's old side)    -> restore from HEAD
        #   known to neither (pure untracked)                              -> clean
        index_hits = [p for p in unclaimed if _in_index(p)]
        head_only = [p for p in unclaimed if p not in index_hits and _in_head(p)]
        pure_untracked = [p for p in unclaimed if p not in index_hits and p not in head_only]
        if index_hits:
            _rollback(["restore", "--worktree", "--staged", "--", *index_hits], "git restore")
        if head_only:
            _rollback(["restore", "--worktree", "--staged", "--source=HEAD", "--", *head_only], "git restore")
        if pure_untracked:
            _rollback(["clean", "-f", "--", *pure_untracked], "git clean")

    if unclaimed:
        note = (
            f"unclaimed changes rolled back: {unclaimed}"
            if rollback
            else f"unclaimed changes (rollback disabled): {unclaimed}"
        )
        if rollback_errors:
            note += " | " + "; ".join(rollback_errors)
    else:
        note = "all changes claimed"

    checks = [
        Check(
            "every changed file is declared in changed_files",
            not unclaimed and not rollback_errors,
            note,
        )
    ]
    missing = sorted(claimed - pre)
    checks.append(
        Check(
            "declared files were actually touched",
            True,
            f"declared but unchanged: {missing}" if missing else "all declarations materialized",
        )
    )
    return GateReport("diff_matches_claims", checks)
