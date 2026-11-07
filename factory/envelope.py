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


class BuildOutput(EnvelopeBase):
    """Output type for builder phases: the agent claims every file it changed.

    ``changed_files`` is what the diff gate verifies against; ``commit_message``
    is a proposal the code side may use — the agent never runs git itself.
    """

    changed_files: list[str] = Field(default_factory=list)
    commit_message: str = ""


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
