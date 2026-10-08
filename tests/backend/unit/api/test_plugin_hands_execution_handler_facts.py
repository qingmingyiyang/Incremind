from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.plugin_hands_runtime import load_plugin_hands_tool_intent
from backend.api.app import create_app
from core.ai_kernel import SQLiteAITurnStore
from core.ai_kernel.tool_invocation import build_intent, intent_to_payload
from core.ai_tooling import ToolDefinition, ToolRetryPolicy
from core.effect_log import EffectClass, EffectIntent, EffectLog, EffectPurpose
from core.plugin_hands.durable_lifecycle import PluginHandsDurableLifecycleError


def _tool() -> ToolDefinition:
    return ToolDefinition(
        tool_id="plugin.hand.example", version=7, display_name="Example Hand",
        description="Contained test Hand", source="plugin",
        owner_id="plugin-example", effect="write", data_classes=("project_content",),
        destination="local", input_schema_uri="crp://schemas/hand-input-v1",
        output_schema_uri="crp://schemas/hand-output-v1",
        receipt_schema_uri="crp://schemas/plugin-hand-receipt-v1",
        operation_semantics="receipt_required", egress_class="none",
        network_scope=(), idempotency="never_retry",
        retry_policy=ToolRetryPolicy(
            max_attempts=1, backoff_ms=0, retryable_error_codes=(),
        ), timeout_ms=30_000,
        execution_mode="exclusive", resource_locks=("workspace:project-1",),
        verification_tool_id=None, compensation_tool_id=None,
        mutability="reversible", data_egress_scope=(), required_scopes=(),
        boundary_requirements=("approval",),
    )


def _effect(tmp_path: Path, *, intent_ref: str, turn_id: str = "turn-00000001"):
    intent = EffectIntent(
        session_id="project-0000001", turn_id=turn_id,
        root_id="tool-call-0000001", parent_id="tool-call-0000001",
        step_key="plugin-hands:plugin-example:hand-example",
        kind="plugin_hands_execution", effect_class=EffectClass.AT_MOST_ONCE,
        purpose=EffectPurpose.PRIMARY, intent_ref=intent_ref,
        gate_decision_id="boundary-decision-0001",
        rev_set={"capability_revision": 7},
        payload={"capability_id": "plugin.hand.example"},
        operation_id_override="plugin-hands-effect-tool-call-0000001",
    )
    return EffectLog(tmp_path / "jobs.sqlite3").plan(intent, now=1)[0]


def _persisted_intent(store: SQLiteAITurnStore, *, turn_id: str = "turn-00000001") -> str:
    store.claim_turn({
        "turn_id": turn_id, "session_id": "project-0000001",
        "operation_id": "operation-0000001",
        "idempotency_key": f"request:{turn_id}",
    })
    intent = build_intent(
        invocation_id="tool-call-0000001", turn_id=turn_id,
        step_id="step-00000001", operation_id="operation-0000001",
        tool=_tool(), arguments={"topic": "safe"}, requires_approval=True,
        authorization_facts_ref="crp://session/turn-00000001/authorization/facts",
        authorization_facts_revision="authorization-r7",
        approval_fact_ref="crp://session/turn-00000001/approval/fact",
    )
    return store.put(turn_id, "tool-invocation-intent", intent_to_payload(intent))


def test_restart_reader_loads_arguments_only_from_immutable_tool_intent(tmp_path: Path) -> None:
    store = SQLiteAITurnStore(tmp_path / "jobs.sqlite3")
    ref = _persisted_intent(store)
    loaded = load_plugin_hands_tool_intent(store, _effect(tmp_path, intent_ref=ref))

    assert loaded.arguments == {"topic": "safe"}
    assert loaded.authorization_facts_revision == "authorization-r7"


def test_restart_reader_rejects_effect_to_intent_identity_drift(tmp_path: Path) -> None:
    store = SQLiteAITurnStore(tmp_path / "jobs.sqlite3")
    ref = _persisted_intent(store, turn_id="turn-foreign01")

    with pytest.raises(PluginHandsDurableLifecycleError, match="drifted"):
        load_plugin_hands_tool_intent(store, _effect(tmp_path, intent_ref=ref))


def test_restart_reader_fails_closed_when_intent_fact_is_missing(tmp_path: Path) -> None:
    store = SQLiteAITurnStore(tmp_path / "jobs.sqlite3")

    with pytest.raises(PluginHandsDurableLifecycleError, match="unavailable"):
        load_plugin_hands_tool_intent(
            store, _effect(tmp_path, intent_ref="missing-intent-ref"),
        )


def test_app_registers_primary_hands_handler_without_eager_ai_runtime(tmp_path: Path) -> None:
    application = create_app(SimpleNamespace(root_dir=tmp_path))

    assert "plugin_hands_execution" in application.state.effect_runtime.handlers.kinds()
    assert not hasattr(application.state, "ai_runtime")
