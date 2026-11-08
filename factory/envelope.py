"""Typed envelopes: the only way agent output crosses a seam.

An agent has exactly two output channels: files it writes into the repo, and a
final valid-JSON response parsed against the output type the call declared.
Context transfers in code, not in conversation.
"""

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

FENCE_RE = re.compile(r"```(?:json)?")

# Prompt templates are data (Markdown), per AGENTS.md — Python only fills placeholders.
PROMPTS_DIR = Path(__file__).parent / "prompts"


class EnvelopeBase(BaseModel):
    """Fields every agent envelope carries, regardless of role."""

    status: Literal["success", "fail"]
    summary: str = ""
    artifacts: list[str] = Field(default_factory=list)
    notes_for_next_agent: str = ""


class RunOutput(EnvelopeBase):
    """Output type for a Phase 1 one-shot agent run."""


class PlanOutput(EnvelopeBase):
    """Output type for planner phases: a plan file another agent can implement
    without asking questions. The plan's path is declared both in ``plan_path``
    and in ``artifacts`` — the artifacts gate verifies it exists and is non-empty.

    ``questions`` (Phase 7) is how the planner raises doubts instead of guessing:
    a non-empty list parks the run for the owner, whose answers re-enter the same
    session. A planner that questions nothing delivers ``[]``.
    """

    plan_path: str = ""
    questions: list[str] = Field(default_factory=list)


class DiscoveredItem(BaseModel):
    """One out-of-scope finding the agent wants on the owner's radar.

    These must NOT be fixed inside the current PR — the fixer and the gates may
    not absorb them. Deterministic code turns them into Backlog cards.
    """

    title: str = ""
    detail: str = ""


class BuildOutput(EnvelopeBase):
    """Output type for builder phases: the agent claims every file it changed.

    ``changed_files`` is what the diff gate verifies against; ``commit_message``
    is a proposal the code side may use — the agent never runs git itself.
    ``discovered`` lists findings outside the task's scope (Phase 8): they become
    system-created Backlog cards, never part of this change.
    """

    changed_files: list[str] = Field(default_factory=list)
    commit_message: str = ""
    discovered: list[DiscoveredItem] = Field(default_factory=list)


class Finding(BaseModel):
    """One reported issue: where it is, what it is, why it was flagged."""

    file: str = ""
    line: int = 0
    description: str = ""
    reason: str = ""


class FindingsOutput(EnvelopeBase):
    """Output type for reviewer phases: the issues found in the reviewed changes.

    ``discovered`` carries out-of-scope observations (Phase 8) — real issues the
    reviewer sees that must not enter the change under review; they become
    Backlog cards with provenance instead of scope creep.
    """

    findings: list[Finding] = Field(default_factory=list)
    discovered: list[DiscoveredItem] = Field(default_factory=list)


class ScoredFinding(Finding):
    """A finding after verification, with a 0-100 confidence score that it is real."""

    score: int = 0


class ScoredOutput(EnvelopeBase):
    """Output type for the scorer phase: findings with scores attached."""

    findings: list[ScoredFinding] = Field(default_factory=list)


def extract_json(text: str) -> dict:
    """Pull a JSON object out of agent text, tolerating fences and stray prose."""
    cleaned = FENCE_RE.sub("", text)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found in agent output")
    return json.loads(cleaned[start : end + 1])


def parse_envelope(text: str, output_type: type[BaseModel] = RunOutput) -> BaseModel:
    """Parse raw agent text against the declared output type. Raises on any mismatch."""
    data = extract_json(text)
    return output_type.model_validate(data)


def report_contract(output_type: type[BaseModel]) -> str:
    """Render the JSON contract appended to every agent prompt (the '## Report' block).

    The output contract lives in three places and they are one thing: the type here,
    the Markdown template in ``factory/prompts/``, and the output_type at the call
    site. Change one, change all three in the same edit.
    """
    template = (PROMPTS_DIR / "report_contract.md").read_text(encoding="utf-8")
    lines = []
    for name, field in output_type.model_fields.items():
        required = "required" if field.is_required() else "optional"
        annotation = str(field.annotation).replace("typing.", "")
        lines.append(f'- "{name}": {annotation} ({required})')
    return template.replace("{{fields}}", "\n".join(lines))
