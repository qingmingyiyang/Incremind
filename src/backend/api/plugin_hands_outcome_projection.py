"""Read-only projection of immutable Plugin Hands facts into an AI Tool result."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from core.ai_kernel.tool_invocation import ToolInvocationIntent
from core.effect_log import Effect, EffectClass, EffectLog, EffectState
from core.plugin_hands.durable_lifecycle import (
    PluginHandsDurableLifecycleError,
    load_plugin_hands_outcome_fact,
)
from core.storage_provider import SQLiteStructuredRecordStore


@dataclass(frozen=True, slots=True)
class PluginHandsProjectedToolCompletion:
    result: Mapping[str, object]
    operation_receipt: Mapping[str, object]
    evidence_refs: tuple[str, ...]


class PluginHandsToolOutcomeProjector:
    """Verify receipt-first Hands completion without resolving a Provider."""

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        if not isinstance(records, SQLiteStructuredRecordStore):
            raise ValueError("Plugin Hands outcome store is invalid")
        self._records = records
        self._effects = EffectLog(records.database_path)

    def project(
        self, intent: ToolInvocationIntent, parent_effect: Effect,
    ) -> PluginHandsProjectedToolCompletion | None:
        contract = intent.tool_contract
        if (
            not isinstance(contract, Mapping)
            or contract.get("source") != "plugin"
            or contract.get("tool_id") != intent.capability_id
            or parent_effect.operation_id != intent.invocation_id
            or parent_effect.turn_id != intent.turn_id
            or parent_effect.intent_ref == ""
            or parent_effect.kind != f"tool_call_{parent_effect.effect_class.value.lower()}"
        ):
            return None
        invocation_id = intent.invocation_id
        receipt = self._records.read("plugin_hands_outcome_receipts", invocation_id)
        if receipt is None or receipt.revision != 1:
            return None
        payload = receipt.payload
        binding = payload.get("binding")
        receipt_ref = f"plugin-hands-outcome:{invocation_id}:r1"
        result_ref = f"plugin-hands-result:{invocation_id}:r1"
        try:
            child = self._effects.get(f"plugin-hands-effect-{invocation_id}")
        except KeyError:
            return None
        if (
            not isinstance(binding, Mapping)
            or payload.get("schema_version") != "1.0.0"
            or payload.get("invocation_id") != invocation_id
            or payload.get("effect_operation_id") != child.operation_id
            or payload.get("status") != "success"
            or payload.get("error_code") is not None
            or payload.get("result_payload_ref") != result_ref
            or binding.get("capability_id") != intent.capability_id
            or binding.get("plugin_id") != contract.get("owner_id")
            or not isinstance(binding.get("hand_id"), str)
            or not isinstance(binding.get("artifact_opaque_ref"), str)
            or not isinstance(binding.get("activation_revision"), int)
            or child.kind != "plugin_hands_execution"
            or child.effect_class is not EffectClass.AT_MOST_ONCE
            or child.state is not EffectState.SETTLED_OK
            or child.root_id != invocation_id
            or child.parent_id != parent_effect.operation_id
            or not child.session_id
            or child.turn_id != intent.turn_id
            or child.intent_ref != parent_effect.intent_ref
            or child.result_ref != receipt_ref
            or child.rev_set.get("activation_revision") != binding.get("activation_revision")
        ):
            return None
        try:
            outcome = load_plugin_hands_outcome_fact(self._records, invocation_id)
        except PluginHandsDurableLifecycleError:
            return None
        if outcome.status != "success" or outcome.output is None:
            return None
        operation_receipt = {
            "schema_version": "1.0.0",
            "operation": "plugin_hand",
            "status": "completed",
            "invocation_id": invocation_id,
            "capability_id": intent.capability_id,
            "plugin_id": binding["plugin_id"],
            "hand_id": binding["hand_id"],
            "artifact_ref": binding["artifact_opaque_ref"],
            "activation_revision": binding["activation_revision"],
            "source_receipt_ref": receipt_ref,
            "source_result_ref": result_ref,
            "child_effect_operation_id": child.operation_id,
        }
        return PluginHandsProjectedToolCompletion(
            result=dict(outcome.output),
            operation_receipt=operation_receipt,
            evidence_refs=(receipt_ref, result_ref, child.operation_id),
        )

    def project_for_runtime(
        self, intent: ToolInvocationIntent, parent_effect: Effect,
    ) -> Mapping[str, object] | None:
        projected = self.project(intent, parent_effect)
        if projected is None:
            return None
        return {
            "result": dict(projected.result),
            "operation_receipt": dict(projected.operation_receipt),
            "evidence_refs": projected.evidence_refs,
        }
