from __future__ import annotations

from dataclasses import replace

from backend.api.plugin_hands_outcome_projection import PluginHandsToolOutcomeProjector
from core.ai_kernel.tool_invocation import ToolInvocationIntent
from core.effect_log import (
    EffectClass,
    EffectIntent,
    EffectLog,
    EffectPurpose,
    EffectRunner,
)
from core.storage_provider import SQLiteStructuredRecordStore


def _intent() -> ToolInvocationIntent:
    return ToolInvocationIntent(
        invocation_id="call-hands-0001",
        turn_id="turn-hands-000001",
        step_id="step-hands-000001",
        capability_id="plugin.hand.example.summarize",
        capability_version=1,
        operation_id="operation-hands-0001",
        idempotency_key="idem-hands-000001",
        execution_mode="exclusive",
        resource_locks=(),
        idempotency="never_retry",
        max_attempts=1,
        retry_backoff_ms=0,
        retryable_error_codes=(),
        timeout_ms=1_000,
        tool_contract={
            "source": "plugin", "owner_id": "example",
            "tool_id": "plugin.hand.example.summarize", "version": 1,
        },
        requires_approval=True,
        arguments={"text": "hello"},
    )


def _facts(tmp_path):
    intent = _intent()
    parent_log = EffectLog(tmp_path / "ai.sqlite3")
    parent, _ = parent_log.plan(EffectIntent(
        session_id="session-hands-0001", turn_id=intent.turn_id,
        root_id=intent.operation_id, step_key="tool:step-hands-000001:call-hands-0001",
        kind="tool_call_at_most_once", effect_class=EffectClass.AT_MOST_ONCE,
        purpose=EffectPurpose.PRIMARY, intent_ref="intent-hands-0001",
        gate_decision_id="gate-hands-000001", rev_set={"capability_revision": "1"},
        payload={}, operation_id_override=intent.invocation_id,
    ), now=1)
    records = SQLiteStructuredRecordStore(tmp_path / "hands.sqlite3")
    child_log = EffectLog(records.database_path)
    child_intent = EffectIntent(
        session_id="project-hands-0001", turn_id=intent.turn_id,
        root_id=intent.invocation_id, parent_id=parent.operation_id,
        step_key="plugin-hands:example:summarize", kind="plugin_hands_execution",
        effect_class=EffectClass.AT_MOST_ONCE, purpose=EffectPurpose.PRIMARY,
        intent_ref=parent.intent_ref, gate_decision_id="gate-hands-000001",
        rev_set={"activation_revision": 3}, payload={},
        operation_id_override=f"plugin-hands-effect-{intent.invocation_id}",
    )
    EffectRunner(child_log, owner_id="hands-test").execute(
        child_intent,
        lambda _effect: f"plugin-hands-outcome:{intent.invocation_id}:r1",
        now=1,
    )
    with records.begin() as uow:
        uow.put("plugin_hands_result_payloads", intent.invocation_id, {
            "schema_version": "1.0.0", "invocation_id": intent.invocation_id,
            "result": {"summary": "done"}, "recorded_at": "2026-08-30T00:00:00Z",
        }, expected_revision=0)
        uow.put("plugin_hands_outcome_receipts", intent.invocation_id, {
            "schema_version": "1.0.0", "invocation_id": intent.invocation_id,
            "lease_id": "lease-hands-000001",
            "effect_operation_id": f"plugin-hands-effect-{intent.invocation_id}",
            "status": "success", "error_code": None,
            "result_payload_ref": f"plugin-hands-result:{intent.invocation_id}:r1",
            "binding": {
                "capability_id": intent.capability_id, "plugin_id": "example",
                "hand_id": "summarize", "artifact_opaque_ref": "artifact-hands-0001",
                "activation_revision": 3,
            },
            "recorded_at": "2026-08-30T00:00:00Z",
        }, expected_revision=0)
        uow.commit()
    return intent, parent, records


def test_projects_verified_hands_facts_without_provider_runtime(tmp_path) -> None:
    intent, parent, records = _facts(tmp_path)

    projected = PluginHandsToolOutcomeProjector(records).project(intent, parent)

    assert projected is not None
    assert projected.result == {"summary": "done"}
    assert projected.operation_receipt["source_receipt_ref"] == (
        "plugin-hands-outcome:call-hands-0001:r1"
    )
    assert projected.evidence_refs[-1] == "plugin-hands-effect-call-hands-0001"


def test_projection_fails_closed_on_parent_or_binding_drift(tmp_path) -> None:
    intent, parent, records = _facts(tmp_path)
    projector = PluginHandsToolOutcomeProjector(records)

    assert projector.project(replace(intent, capability_id="plugin.hand.other.tool"), parent) is None
    assert projector.project(intent, replace(parent, intent_ref="intent-drift-0001")) is None
