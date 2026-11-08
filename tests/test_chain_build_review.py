"""Tests for the build-review chain: reviewers → scorer → gate → fix rounds."""

from pathlib import Path

from factory.agent_pi import AgentResult
from factory.chains import run_build_review
from factory.db import connect

REVIEWERS = [
    {"name": "review-conventions", "model": "zai/glm-5.3-flash", "thinking": "medium"},
    {"name": "review-bugs", "model": "zai/glm-5.3-flash", "thinking": "high"},
]
SCORER = {"name": "scorer", "model": "zai/glm-5.3-flash", "thinking": "medium"}
FIXER = {"name": "builder", "model": "zai/glm-5.3-flash", "thinking": "medium"}

CLEAN_FINDINGS = (
    '{"status": "success", "summary": "no violations", "findings": []}'
)
BLOCKING_FINDINGS = (
    '{"status": "success", "summary": "one violation", "findings": ['
    '{"file": "README.md", "line": 1, "description": "spanish text in code", "reason": "AGENTS.md language policy"}]}'
)
CLEAN_SCORED = '{"status": "success", "summary": "verified", "findings": []}'
BLOCKING_SCORED = (
    '{"status": "success", "summary": "verified", "findings": ['
    '{"file": "README.md", "line": 1, "description": "spanish text in code", '
    '"reason": "real", "score": 75}]}'
)
FIX_BUILD = (
    '{"status": "success", "summary": "fixed", "artifacts": [], '
    '"changed_files": ["README.md"], "commit_message": ""}'
)


class StubAgent:
    """Returns outputs in call order; a None output means 'keep the tree as is'."""

    def __init__(self, outputs, side_effects=None):
        self.outputs = list(outputs)
        self.side_effects = list(side_effects or [])
        self.calls = []

    def __call__(self, prompt, model, session_id=None, on_event=None, repo_dir=None, **kwargs):
        self.calls.append({"prompt": prompt, "session_id": session_id})
        if self.side_effects:
            effect = self.side_effects.pop(0)
            if effect is not None and repo_dir is not None:
                effect(Path(repo_dir))
        if on_event is not None:
            on_event({"type": "agent_start"})
        return AgentResult(exit_code=0, stdout=self.outputs.pop(0), stderr="", duration_ms=1, command=[])


def test_review_gate_passes_when_nothing_scores_50(repo, tmp_path):
    (repo / "wip.txt").write_text("work in progress\n")  # the diff under review
    agent = StubAgent([CLEAN_FINDINGS, CLEAN_FINDINGS, CLEAN_SCORED])
    conn = connect(tmp_path / "factory.db")
    accepted, reason = run_build_review(
        conn, repo_dir=repo, reviewer_cfgs=REVIEWERS, scorer_cfg=SCORER,
        fixer_cfg=FIXER, max_rounds=2, agent_fn=agent,
    )
    assert accepted, reason
    assert "no findings" in reason or "no changes" in reason
    # two reviewer lenses + scorer, one round, no fix phase
    assert len(agent.calls) == 3
    names = [r["name"] for r in conn.execute("SELECT name FROM phases ORDER BY seq").fetchall()]
    assert names == ["review:review-conventions:r1", "review:review-bugs:r1", "score:r1", "gate:r1"]


def test_blocking_findings_trigger_fix_then_pass(repo, tmp_path):
    (repo / "wip.txt").write_text("work in progress\n")
    def fix_the_file(repo_dir: Path):
        (repo_dir / "README.md").write_text("fixed, all english now\n")

    agent = StubAgent(
        [
            BLOCKING_FINDINGS, BLOCKING_FINDINGS, BLOCKING_SCORED,   # round 1: blocked
            FIX_BUILD,                                               # fixer's own envelope
            CLEAN_FINDINGS, CLEAN_FINDINGS, CLEAN_SCORED,            # round 2: clean
        ],
        side_effects=[None, None, None, fix_the_file, None, None, None],
    )
    conn = connect(tmp_path / "factory.db")
    accepted, reason = run_build_review(
        conn, repo_dir=repo, reviewer_cfgs=REVIEWERS, scorer_cfg=SCORER,
        fixer_cfg=FIXER, max_rounds=2, agent_fn=agent,
    )
    assert accepted, reason
    # round 1: 2 reviewers + scorer; fix; round 2: 2 reviewers + scorer
    assert len(agent.calls) == 7
    assert "scored >=50" in agent.calls[3]["prompt"]
    assert "spanish text" in agent.calls[3]["prompt"]  # the blocked findings reached the fixer


def test_exhausted_rounds_settle_rejected(repo, tmp_path):
    (repo / "wip.txt").write_text("work in progress\n")
    agent = StubAgent(
        [
            BLOCKING_FINDINGS, BLOCKING_FINDINGS, BLOCKING_SCORED,   # round 1: blocked
            FIX_BUILD,                                               # fixer
            BLOCKING_FINDINGS, BLOCKING_FINDINGS, BLOCKING_SCORED,   # round 2: still blocked
        ],
        side_effects=[None, None, None, None, None, None, None],
    )
    conn = connect(tmp_path / "factory.db")
    accepted, reason = run_build_review(
        conn, repo_dir=repo, reviewer_cfgs=REVIEWERS, scorer_cfg=SCORER,
        fixer_cfg=FIXER, max_rounds=2, agent_fn=agent,
    )
    assert not accepted
    assert "findings scored >=50" in reason and "spanish text" in reason
    assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "failed"


def test_user_wip_survives_the_fixer(repo, tmp_path):
    (repo / "user_wip.txt").write_text("my work in progress\n")  # dirty before the chain
    fix_build = (
        '{"status": "success", "summary": "fixed", "artifacts": [], '
        '"changed_files": ["fix.txt"], "commit_message": ""}'
    )
    agent = StubAgent(
        [
            BLOCKING_FINDINGS, BLOCKING_FINDINGS, BLOCKING_SCORED,
            fix_build,
            CLEAN_FINDINGS, CLEAN_FINDINGS, CLEAN_SCORED,
        ],
        side_effects=[None, None, None, lambda d: (d / "fix.txt").write_text("fix\n"), None, None, None],
    )
    conn = connect(tmp_path / "factory.db")
    accepted, _ = run_build_review(
        conn, repo_dir=repo, reviewer_cfgs=REVIEWERS, scorer_cfg=SCORER,
        fixer_cfg=FIXER, max_rounds=2, agent_fn=agent,
    )
    assert accepted
    assert (repo / "fix.txt").exists()                                    # the fixer's claimed work survives
    assert (repo / "user_wip.txt").read_text() == "my work in progress\n"  # survived the fixer's gate


def test_fixer_claimed_edit_to_a_baseline_file_is_sanctioned(repo, tmp_path):
    (repo / "user_wip.txt").write_text("wip with a bug: espanol\n")
    fix_build = (
        '{"status": "success", "summary": "fixed the wip file", "artifacts": [], '
        '"changed_files": ["user_wip.txt"], "commit_message": ""}'
    )

    def fix_baseline_file(repo_dir: Path):
        (repo_dir / "user_wip.txt").write_text("wip fixed: english now\n")  # claims it too

    agent = StubAgent(
        [
            BLOCKING_FINDINGS, BLOCKING_FINDINGS, BLOCKING_SCORED,
            fix_build,
            CLEAN_FINDINGS, CLEAN_FINDINGS, CLEAN_SCORED,
        ],
        side_effects=[None, None, None, fix_baseline_file, None, None, None],
    )
    conn = connect(tmp_path / "factory.db")
    accepted, _ = run_build_review(
        conn, repo_dir=repo, reviewer_cfgs=REVIEWERS, scorer_cfg=SCORER,
        fixer_cfg=FIXER, max_rounds=2, agent_fn=agent,
    )
    assert accepted, "a claimed edit to a baseline file is sanctioned, not rolled back"
    assert (repo / "user_wip.txt").read_text() == "wip fixed: english now\n"  # the fix survives


def test_fixer_gate_violations_are_corrected_in_session(repo, tmp_path):
    (repo / "wip.txt").write_text("work in progress\n")
    bad_fix = (
        '{"status": "success", "summary": "fixed", "artifacts": [], '
        '"changed_files": ["fix.txt"], "commit_message": ""}'
    )
    good_fix = bad_fix  # same claims; the rogue file was already rolled back

    def bad_then_good(repo_dir: Path):
        if not (repo_dir / "fix.txt").exists():
            (repo_dir / "fix.txt").write_text("fix\n")
            (repo_dir / "rogue.txt").write_text("unclaimed debris\n")  # first attempt over-claims

    agent = StubAgent(
        [
            BLOCKING_FINDINGS, BLOCKING_FINDINGS, BLOCKING_SCORED,
            bad_fix, good_fix,
            CLEAN_FINDINGS, CLEAN_FINDINGS, CLEAN_SCORED,
        ],
        side_effects=[None, None, None, bad_then_good, bad_then_good, None, None],
    )
    conn = connect(tmp_path / "factory.db")
    accepted, _ = run_build_review(
        conn, repo_dir=repo, reviewer_cfgs=REVIEWERS, scorer_cfg=SCORER,
        fixer_cfg=FIXER, max_rounds=2, agent_fn=agent,
    )
    assert accepted
    assert not (repo / "rogue.txt").exists()  # rolled back, then the correction landed
    violations = conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = 'fix_gate_violations'"
    ).fetchone()[0]
    assert violations == 1  # first attempt flagged, second attempt passed


def test_max_rounds_below_one_fails_fast(repo, tmp_path):
    # A review loop with no rounds could never accept; that is a caller bug —
    # raise instead of settling the run with a synthetic "exhausted" verdict.
    import pytest

    conn = connect(tmp_path / "factory.db")
    with pytest.raises(ValueError, match="max_rounds must be >= 1"):
        run_build_review(
            conn, repo_dir=repo, reviewer_cfgs=REVIEWERS, scorer_cfg=SCORER,
            fixer_cfg=FIXER, max_rounds=0, agent_fn=StubAgent([]),
        )
