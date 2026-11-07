"""Tests for typed envelopes: extraction, parsing, and the prompt contract."""

import pytest
from pydantic import ValidationError as PydanticValidationError

from factory.envelope import EnvelopeBase, RunOutput, extract_json, parse_envelope, report_contract

VALID = '{"status": "success", "summary": "done", "artifacts": [], "notes_for_next_agent": ""}'


def test_extract_json_plain():
    assert extract_json(VALID)["status"] == "success"


def test_extract_json_inside_fences_and_prose():
    text = f"Sure, here you go:\n```json\n{VALID}\n```\nHope that helps!"
    assert extract_json(text)["summary"] == "done"


def test_extract_json_no_object_raises():
    with pytest.raises(ValueError, match="no JSON object"):
        extract_json("the agent just chatted, no JSON at all")


def test_parse_envelope_rejects_missing_required_field():
    with pytest.raises(PydanticValidationError):
        parse_envelope('{"summary": "no status field"}')


def test_parse_envelope_rejects_bad_status_literal():
    with pytest.raises(PydanticValidationError):
        parse_envelope('{"status": "maybe"}')


def test_report_contract_lists_fields():
    contract = report_contract(RunOutput)
    assert '"status"' in contract and '"artifacts"' in contract
    assert "FINAL message" in contract


def test_report_contract_renders_template_without_placeholders():
    contract = report_contract(RunOutput)
    assert contract.startswith("## Report")
    assert "{{" not in contract  # every placeholder was filled from the type


def test_envelope_base_defaults():
    env = EnvelopeBase(status="fail")
    assert env.artifacts == [] and env.summary == ""
