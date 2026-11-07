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
    """Working-tree changes vs HEAD: modified, staged, and untracked paths."""
    out = _git("status", "--porcelain", repo_dir=repo_dir).stdout
    paths = []
    for line in out.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip().strip('"')
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
    """Every declared artifact exists and is non-empty."""
    checks = []
    for rel in envelope.artifacts:
        path = repo_dir / rel
        ok = path.is_file() and path.stat().st_size > 0
        checks.append(Check(f"artifact exists and non-empty: {rel}", ok, "" if ok else "missing or empty"))
    if not envelope.artifacts:
        checks.append(Check("artifacts declared", True, "none declared — nothing to verify"))
    return GateReport("artifacts_exist", checks)


def diff_matches_claims(envelope: EnvelopeBase, repo_dir: Path, rollback: bool = False) -> GateReport:
    """Every actually-changed file must be declared in ``changed_files``.

    Unclaimed changes are rolled back (tracked files restored, untracked files
    removed) before the report is built, so the violation stays visible in the
    report while the working tree is already clean of them. Declared-but-
    unchanged files are a warning note, not a failure — agents may reconsider
    a plan mid-flight.
    """
    claimed = set(envelope.changed_files)
    pre = set(changed_files(repo_dir))
    unclaimed = sorted(pre - claimed)

    if unclaimed and rollback:
        tracked = [p for p in unclaimed if _git("ls-files", "--error-unmatch", p, repo_dir=repo_dir).returncode == 0]
        if tracked:
            _git("restore", "--worktree", "--staged", "--", *tracked, repo_dir=repo_dir)
        untracked = [p for p in unclaimed if p not in tracked]
        if untracked:
            _git("clean", "-f", "--", *untracked, repo_dir=repo_dir)

    checks = [
        Check(
            "every changed file is declared in changed_files",
            not unclaimed,
            f"unclaimed changes rolled back: {unclaimed}" if unclaimed else "all changes claimed",
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
