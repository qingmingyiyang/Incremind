from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "core-contracts" / "ai"


def validator(name: str) -> Draft202012Validator:
    schema = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


REQUEST = {
    "input": {"kind": "text", "text": "xhs share text", "source_ref": None},
    "intent": "organize",
    "output_profile": {"profile_id": "default", "revision": "1"},
    "resource_budget": {"max_assets": 8, "max_bytes": 1000, "max_seconds": 20},
}
ADMITTED = {
    "status": "admitted", "reason": None,
    "manifest_ref": "crp://default/source-manifests/m-1", "manifest_revision": "m-r1",
    "job_id": "job-1", "job_ref": "crp://default/jobs/job-1", "job_revision": 1,
    "replayed": False,
}
TERMINAL = {
    "status": "terminal", "reason": "unsupported_content_kind",
    "manifest_ref": "crp://default/source-manifests/m-1", "manifest_revision": "m-r1",
    "job_id": None, "job_ref": None, "job_revision": None, "replayed": False,
}
RECEIPT = {
    "schema_version": "1.0.0", "receipt_id": "receipt-1", "turn_id": "turn-1",
    "tool_call_id": "call-1", "operation_id": "operation-1", "tool_name": "analyze_source",
    "tool_version": 1, "idempotency_key": "idempotency-key-0001", "outcome": ADMITTED,
    "evidence_refs": ["crp://default/evidence/adapter-1"],
}


@pytest.mark.parametrize(("name", "value"), [
    ("analyze-source-request.schema.json", REQUEST),
    ("analyze-source-result.schema.json", ADMITTED),
    ("analyze-source-result.schema.json", TERMINAL),
    ("analyze-source-receipt.schema.json", RECEIPT),
])
def test_valid_contract_examples_pass(name: str, value: dict) -> None:
    assert validator(name).is_valid(value)


@pytest.mark.parametrize(("name", "value"), [
    ("analyze-source-request.schema.json", REQUEST),
    ("analyze-source-result.schema.json", ADMITTED),
    ("analyze-source-receipt.schema.json", RECEIPT),
])
def test_contracts_fail_closed_for_unknown_fields(name: str, value: dict) -> None:
    invalid = deepcopy(value)
    invalid["network_url"] = "https://must-not-appear.example"
    assert not validator(name).is_valid(invalid)


def test_request_is_the_model_visible_tool_shape() -> None:
    assert set(REQUEST) == {"input", "intent", "output_profile", "resource_budget"}
    with_hint = deepcopy(REQUEST)
    with_hint["platform_hint"] = "bilibili"
    assert not validator("analyze-source-request.schema.json").is_valid(with_hint)
    invalid = deepcopy(REQUEST)
    invalid["input"] = {"kind": "text", "text": "content", "source_ref": "crp://default/sources/1"}
    assert not validator("analyze-source-request.schema.json").is_valid(invalid)
    invalid["input"] = {"kind": "source_ref", "text": None, "source_ref": "https://outside.example/source"}
    assert not validator("analyze-source-request.schema.json").is_valid(invalid)


def test_result_enforces_terminal_and_admitted_invariants() -> None:
    invalid = deepcopy(ADMITTED)
    invalid["job_ref"] = None
    assert not validator("analyze-source-result.schema.json").is_valid(invalid)
    invalid = deepcopy(TERMINAL)
    invalid["replayed"] = True
    assert not validator("analyze-source-result.schema.json").is_valid(invalid)


def test_receipt_binds_turn_tool_call_operation_and_idempotency() -> None:
    invalid = deepcopy(RECEIPT)
    invalid["idempotency_key"] = "short"
    assert not validator("analyze-source-receipt.schema.json").is_valid(invalid)
    invalid = deepcopy(RECEIPT)
    invalid["evidence_refs"].append(invalid["evidence_refs"][0])
    assert not validator("analyze-source-receipt.schema.json").is_valid(invalid)
    invalid = deepcopy(RECEIPT)
    invalid["outcome"] = deepcopy(TERMINAL)
    invalid["outcome"]["job_ref"] = "crp://default/jobs/must-not-exist"
    assert not validator("analyze-source-receipt.schema.json").is_valid(invalid)
