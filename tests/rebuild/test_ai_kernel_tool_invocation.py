from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from core.ai_kernel.contracts import AIKernelContractError
from core.ai_kernel.ports import CapabilityDefinition
from core.ai_kernel.tool_invocation import (
    ToolAttemptFailure,
    ToolInvocationOutcome,
    attempt_failure_to_payload,
    build_intent,
    intent_from_payload,
    intent_to_payload,
    outcome_to_payload,
)
from core.ai_tooling import tool_from_legacy_capability


ROOT = Path(__file__).resolve().parents[2]


def test_invocation_intent_freezes_recoverable_legacy_tool_contract() -> None:
    tool = tool_from_legacy_capability(
        CapabilityDefinition(
            "memory.recall",
            1,
            "read",
            False,
            "read_only",
            "crp://input",
            "crp://output",
        )
    )
    intent = build_intent(
        invocation_id="tool-call-0123456789abcdef0123456789abcdef",
        turn_id="turn-0123456789abcdef0123456789abcdef",
        step_id="step-0123456789abcdef0123456789abcdef",
        operation_id="op-project-answer-0001",
        tool=tool,
        arguments={"query": "evidence"},
    )
    restored = intent_from_payload(intent_to_payload(intent))

    assert restored == intent
    assert restored.idempotency == "idempotent"
    assert restored.max_attempts == 2
    assert restored.retry_backoff_ms == 250
    assert restored.retryable_error_codes == ("timeout", "temporarily_unavailable")
    assert restored.idempotency_key.endswith(restored.invocation_id)


def test_invocation_intent_rejects_secret_shaped_arguments_before_persistence() -> None:
    tool = tool_from_legacy_capability(
        CapabilityDefinition(
            "memory.recall",
            1,
            "read",
            False,
            "read_only",
            "crp://input",
            "crp://output",
        )
    )
    with pytest.raises(AIKernelContractError, match="sensitive field"):
        build_intent(
            invocation_id="tool-call-0123456789abcdef0123456789abcdef",
            turn_id="turn-0123456789abcdef0123456789abcdef",
            step_id="step-0123456789abcdef0123456789abcdef",
            operation_id="op-project-answer-0001",
            tool=tool,
            arguments={"api_key": "forbidden"},
        )


def test_historical_intent_without_tool_contract_remains_readable_as_legacy() -> None:
    tool = tool_from_legacy_capability(CapabilityDefinition(
        "memory.recall", 1, "read", False, "read_only", "crp://input", "crp://output",
    ))
    intent = build_intent(
        invocation_id="tool-call-0123456789abcdef0123456789abcdef",
        turn_id="turn-0123456789abcdef0123456789abcdef",
        step_id="step-0123456789abcdef0123456789abcdef",
        operation_id="op-project-answer-0001",
        tool=tool,
        arguments={},
    )
    historical = intent_to_payload(intent)
    historical.pop("tool_contract")
    historical.pop("requires_approval")

    restored = intent_from_payload(historical)

    assert restored.tool_contract is None
    assert restored.requires_approval is None
    assert restored.authorization_facts_ref is None
    assert restored.authorization_facts_revision is None
    assert restored.approval_fact_ref is None


def test_invocation_intent_carries_optional_frozen_authorization_facts_in_v1_codec() -> None:
    tool = tool_from_legacy_capability(CapabilityDefinition(
        "document.draft", 1, "write", True, "receipt_required", "crp://input", "crp://output",
    ))
    intent = build_intent(
        invocation_id="tool-call-0123456789abcdef0123456789abcdef",
        turn_id="turn-0123456789abcdef0123456789abcdef",
        step_id="step-0123456789abcdef0123456789abcdef",
        operation_id="op-project-answer-0001",
        tool=tool,
        arguments={},
        requires_approval=True,
        authorization_facts_ref="crp://session/turn-0123456789abcdef/frozen-authorization-facts/facts-1",
        authorization_facts_revision="facts-r1",
        approval_fact_ref="crp://session/turn-0123456789abcdef/frozen-approval-fact/approval-1",
    )

    payload = intent_to_payload(intent)

    assert payload["schema_version"] == "1.0.0"
    assert payload["authorization_facts_ref"] == intent.authorization_facts_ref
    assert payload["authorization_facts_revision"] == intent.authorization_facts_revision
    assert payload["approval_fact_ref"] == intent.approval_fact_ref
    assert intent_from_payload(payload) == intent


@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        (
            {"authorization_facts_ref": "crp://session/turn-a/facts/facts-1"},
            "must be provided together",
        ),
        (
            {"authorization_facts_revision": "facts-r1"},
            "must be provided together",
        ),
        (
            {
                "authorization_facts_ref": "https://not-a-session-ref",
                "authorization_facts_revision": "facts-r1",
            },
            "safe session crp reference",
        ),
        (
            {
                "authorization_facts_ref": "crp://session/turn-a/facts/facts-1",
                "authorization_facts_revision": "contains whitespace",
            },
            "safe identifier",
        ),
        (
            {"approval_fact_ref": "crp://default/approval-fact/approval-1"},
            "safe session crp reference",
        ),
    ),
)
def test_invocation_intent_rejects_malformed_optional_frozen_authorization_fields(kwargs, message) -> None:
    tool = tool_from_legacy_capability(CapabilityDefinition(
        "memory.recall", 1, "read", False, "read_only", "crp://input", "crp://output",
    ))

    with pytest.raises(AIKernelContractError, match=message):
        build_intent(
            invocation_id="tool-call-0123456789abcdef0123456789abcdef",
            turn_id="turn-0123456789abcdef0123456789abcdef",
            step_id="step-0123456789abcdef0123456789abcdef",
            operation_id="op-project-answer-0001",
            tool=tool,
            arguments={},
            **kwargs,
        )


def test_invocation_payloads_satisfy_strict_schemas() -> None:
    tool = tool_from_legacy_capability(
        CapabilityDefinition(
            "document.draft",
            1,
            "write",
            True,
            "receipt_required",
            "crp://input",
            "crp://output",
        )
    )
    intent = build_intent(
        invocation_id="tool-call-0123456789abcdef0123456789abcdef",
        turn_id="turn-0123456789abcdef0123456789abcdef",
        step_id="step-0123456789abcdef0123456789abcdef",
        operation_id="op-project-answer-0001",
        tool=tool,
        arguments={},
    )
    outcome = ToolInvocationOutcome(
        invocation_id=intent.invocation_id,
        turn_id=intent.turn_id,
        capability_id=intent.capability_id,
        attempt=1,
        status="unknown_effect",
        effect_certainty="unknown",
        payload_ref=None,
        receipt_ref=None,
        evidence_refs=(),
        error_code="ai.tool_outcome_unknown",
        retryable=False,
    )
    attempt_failure = ToolAttemptFailure(
        invocation_id=intent.invocation_id,
        turn_id=intent.turn_id,
        capability_id=intent.capability_id,
        attempt=1,
        error_code="timeout",
        effect_certainty="confirmed_none",
        backoff_ms=250,
    )
    cases = (
        ("tool-invocation-intent.schema.json", intent_to_payload(intent)),
        ("tool-invocation-outcome.schema.json", outcome_to_payload(outcome)),
        ("tool-attempt-failure.schema.json", attempt_failure_to_payload(attempt_failure)),
    )
    for name, payload in cases:
        schema = json.loads((ROOT / "core-contracts" / "ai" / name).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        assert not tuple(Draft202012Validator(schema).iter_errors(payload))

    historical_outcome = outcome_to_payload(outcome)
    historical_outcome.pop("effect_certainty")
    outcome_schema = json.loads(
        (ROOT / "core-contracts" / "ai" / "tool-invocation-outcome.schema.json").read_text(encoding="utf-8")
    )
    assert not tuple(Draft202012Validator(outcome_schema).iter_errors(historical_outcome))
