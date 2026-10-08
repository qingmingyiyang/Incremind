from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from jsonschema import Draft202012Validator, FormatChecker

from core.ai_kernel import AIKernelContractError, build_execution_projection, validate_execution_projection


ROOT = Path(__file__).resolve().parents[2]
TURN_ID = "turn-0123456789abcdef0123456789abcdef"
STEP_ID = "step-0123456789abcdef0123456789abcdef"
TOOL_CALL_ID = "tool-call-0123456789abcdef0123456789abcdef"
MODEL_REQUEST_ID = "model-request-0123456789abcdef0123456789abcdef"
MODEL_RECEIPT_REF = "crp://default/model-receipt.json"


def test_completed_projection_uses_one_event_source_for_simple_and_developer() -> None:
    events, payloads = _completed_events()

    simple = build_execution_projection(events, view="simple", payload_loader=payloads.__getitem__)
    developer = build_execution_projection(events, view="developer", payload_loader=payloads.__getitem__)

    _validate(simple)
    _validate(developer)
    assert simple["status"] == developer["status"] == "completed"
    assert simple["current_sequence"] == developer["current_sequence"] == len(events)
    assert simple["current_stage"]["kind"] == "completed"
    assert "tool_steps" not in simple and "model_steps" not in simple
    assert developer["model_steps"][0]["metadata_status"] == "not_recorded"
    assert developer["model_steps"][0]["wire_attempts_status"] == "not_recorded"
    assert developer["model_steps"][0]["wire_attempts"] == []
    assert developer["model_steps"][0]["recorded"] == {
        "provider_id": False, "model_id": False, "usage": False,
        "input": False, "output": False,
    }
    tool = developer["tool_steps"][0]
    assert tool["capability_id"] == "memory.recall"
    assert tool["execution_mode"] == "parallel"
    assert tool["idempotency"] == "idempotent"
    assert tool["attempts"] == [{
        "attempt": 1, "status": "completed", "error_code": None,
        "retryable": False, "effect_certainty": "confirmed_none", "backoff_ms": None,
    }]


def test_expert_projection_exposes_only_user_safe_turn_facts() -> None:
    selection_ref = "crp://default/expert-selection.json"
    binding_ref = "crp://default/expert-binding.json"
    receipt_ref = "crp://default/expert-receipt.json"
    payloads = {
        selection_ref: {
            "selected": {
                "expert_id": "video-research-expert",
                "reason": "project_default_binding",
                "secret": "must-not-project",
            },
        },
        binding_ref: {"snapshot_id": "must-not-project", "expert_revision": 7},
        receipt_ref: {
            "status": "completed",
            "input_evidence_refs": ["crp://default/evidence/1", "crp://default/evidence/2"],
            "output_refs": ["crp://default/output/1"],
            "summary": "must-not-project",
        },
    }
    events = [
        _event(1, "turn.accepted", "accepted", tool=False, model=False),
        _event(2, "expert.selection.recorded", "running", payload_ref=selection_ref, tool=False, model=False),
        _event(3, "expert.binding.frozen", "running", payload_ref=binding_ref, tool=False, model=False),
        _event(4, "expert.execution.receipted", "running", payload_ref=receipt_ref, tool=False, model=False),
        _event(5, "turn.completed", "completed", tool=False, model=False),
    ]

    simple = build_execution_projection(events, view="simple", payload_loader=payloads.__getitem__)
    developer = build_execution_projection(events, view="developer", payload_loader=payloads.__getitem__)

    _validate(simple)
    _validate(developer)
    assert simple["expert"] == developer["expert"] == {
        "selected_expert": "video-research-expert",
        "selection_reason": "这是当前项目的默认专家",
        "current_phase": "completed",
        "evidence_source_count": 2,
        "receipt_status": "completed",
    }
    encoded = json.dumps(simple, ensure_ascii=False)
    assert "snapshot_id" not in encoded
    assert "expert_revision" not in encoded
    assert "must-not-project" not in encoded


def test_waiting_approval_projection_requests_user_action_without_payload_leak() -> None:
    events, payloads = _waiting_events()
    simple = build_execution_projection(events, view="simple", payload_loader=payloads.__getitem__)
    developer = build_execution_projection(events, view="developer", payload_loader=payloads.__getitem__)

    _validate(simple)
    _validate(developer)
    assert simple["status"] == "waiting_approval"
    assert simple["current_stage"]["kind"] == "waiting_approval"
    assert simple["next_action"] == "approve"
    encoded = json.dumps(simple, ensure_ascii=False)
    assert TOOL_CALL_ID not in encoded
    assert "memory.recall" not in encoded
    assert "payload" not in encoded
    assert "api_key" not in encoded
    boundary = developer["tool_steps"][0]["boundary"]
    assert boundary == {
        "status": "ask", "requires_receipt": False,
        "redaction_required": True, "reason_codes": ["soft_sensitive"],
    }


def test_retry_projection_preserves_each_attempt_and_safe_backoff() -> None:
    events, payloads = _retry_events()
    developer = build_execution_projection(events, view="developer", payload_loader=payloads.__getitem__)
    simple = build_execution_projection(events, view="simple", payload_loader=payloads.__getitem__)

    _validate(developer)
    assert [item["status"] for item in developer["tool_steps"][0]["attempts"]] == ["retry_scheduled", "completed"]
    assert developer["tool_steps"][0]["attempts"][0]["backoff_ms"] == 250
    assert developer["tool_steps"][0]["attempts"][0]["effect_certainty"] == "confirmed_none"
    assert "安全重试 1 次" in simple["current_stage"]["detail"]


def test_unknown_effect_forces_review_instead_of_safe_retry() -> None:
    events, payloads = _terminal_tool_events("unknown_effect", "unknown", "turn.failed", "failed")

    simple = build_execution_projection(events, view="simple", payload_loader=payloads.__getitem__)
    developer = build_execution_projection(events, view="developer", payload_loader=payloads.__getitem__)

    _validate(simple)
    assert simple["current_stage"]["kind"] == "review_required"
    assert simple["next_action"] == "review"
    assert developer["tool_steps"][0]["status"] == "unknown_effect"
    assert developer["tool_steps"][0]["attempts"][0]["effect_certainty"] == "unknown"


def test_confirmed_cancel_is_not_projected_as_unknown_effect() -> None:
    events, payloads = _terminal_tool_events("cancelled", "confirmed_none", "turn.cancelled", "cancelled")
    simple = build_execution_projection(events, view="simple", payload_loader=payloads.__getitem__)
    developer = build_execution_projection(events, view="developer", payload_loader=payloads.__getitem__)

    _validate(simple)
    assert simple["current_stage"]["kind"] == "cancelled"
    assert simple["next_action"] == "none"
    assert developer["tool_steps"][0]["status"] == "cancelled"


def test_mismatched_retry_payload_is_not_projected_as_confirmed_none() -> None:
    events, payloads = _tool_prefix()
    payloads["crp://default/failure.json"] = {
        "schema_version": "1.0.0", "invocation_id": "tool-call-wrong",
        "turn_id": TURN_ID, "capability_id": "memory.recall", "attempt": 1,
        "error_code": "temporarily_unavailable", "effect_certainty": "confirmed_none", "backoff_ms": 250,
    }
    events.append(_event(9, "tool.attempt.failed", "running", payload_ref="crp://default/failure.json", retryable=True))

    developer = build_execution_projection(events, view="developer", payload_loader=payloads.__getitem__)

    assert developer["tool_steps"][0]["status"] == "running"
    assert developer["tool_steps"][0]["attempts"][0]["effect_certainty"] == "not_recorded"


def test_timed_out_outcome_keeps_attempt_detail_and_maps_tool_to_failed() -> None:
    events, payloads = _tool_prefix()
    payloads["crp://default/outcome.json"] = _outcome("timed_out", "confirmed_none", attempt=1)
    events.extend([
        _event(9, "tool.outcome.recorded", "running", payload_ref="crp://default/outcome.json", error_code="ai.tool_deadline_exceeded"),
        _event(10, "turn.failed", "failed", error_code="ai.tool_deadline_exceeded"),
    ])

    developer = build_execution_projection(events, view="developer", payload_loader=payloads.__getitem__)

    _validate(developer)
    assert developer["tool_steps"][0]["status"] == "failed"
    assert developer["tool_steps"][0]["attempts"][0]["status"] == "timed_out"


def test_approval_resolved_moves_tool_out_of_waiting_state() -> None:
    events, payloads = _waiting_events()
    events.append(_event(7, "approval.resolved", "running", payload_ref="crp://default/action.json"))

    developer = build_execution_projection(events, view="developer", payload_loader=payloads.__getitem__)

    assert developer["tool_steps"][0]["status"] == "running"
    assert developer["current_stage"]["kind"] == "using_tool"


def test_historical_outcome_without_effect_certainty_is_explicitly_not_recorded() -> None:
    events, payloads = _tool_prefix()
    historical = _outcome("completed", "confirmed_none", attempt=1)
    historical.pop("effect_certainty")
    payloads["crp://default/outcome.json"] = historical
    events.extend([
        _event(9, "tool.outcome.recorded", "running", payload_ref="crp://default/outcome.json"),
        _event(10, "tool.completed", "running"),
        _event(11, "turn.completed", "completed"),
    ])

    developer = build_execution_projection(events, view="developer", payload_loader=payloads.__getitem__)

    assert developer["tool_steps"][0]["attempts"][0]["effect_certainty"] == "not_recorded"


def test_projection_rejects_datetime_without_timezone() -> None:
    events, payloads = _completed_events()
    invalid = copy.deepcopy(events)
    invalid[-1]["occurred_at"] = "2026-08-24T00:00:13"

    with pytest.raises(AIKernelContractError, match="timezone"):
        build_execution_projection(invalid, view="simple", payload_loader=payloads.__getitem__)


@pytest.mark.parametrize("event_type,status", [
    ("model.completed", "completed"),
    ("model.failed", "failed"),
    ("model.cancelled", "cancelled"),
    ("model.timed_out", "timed_out"),
])
def test_model_receipt_projects_only_identity_matched_safe_metadata(event_type, status) -> None:
    events = _base_events()[:3]
    payloads = {MODEL_RECEIPT_REF: _model_receipt(status)}
    events.append(_event(4, event_type, "running", tool=False, receipt_ref=MODEL_RECEIPT_REF))

    developer = build_execution_projection(events, view="developer", payload_loader=payloads.__getitem__)

    _validate(developer)
    step = developer["model_steps"][0]
    assert step["status"] == status
    assert step["receipt_status"] == step["metadata_status"] == "recorded"
    assert step["provider_id"] == "deepseek"
    assert step["model_id"] == "deepseek-chat"
    assert step["usage_status"] == "recorded"
    assert step["usage"] == {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
    assert step["recorded"] == {"provider_id": True, "model_id": True, "usage": True, "input": False, "output": False}
    assert '"prompt":' not in json.dumps(developer)


def test_mismatched_or_invalid_model_receipt_is_not_recorded() -> None:
    events = _base_events()[:3]
    receipt = _model_receipt("completed")
    receipt["model_request_id"] = "model-request-wrong"
    payloads = {MODEL_RECEIPT_REF: receipt}
    events.append(_event(4, "model.completed", "running", tool=False, receipt_ref=MODEL_RECEIPT_REF))

    step = build_execution_projection(events, view="developer", payload_loader=payloads.__getitem__)["model_steps"][0]

    assert step["receipt_status"] == step["metadata_status"] == "not_recorded"
    assert step["provider_id"] is None and step["usage"] is None


def test_model_routed_projects_only_the_frozen_selected_route_to_developer() -> None:
    events = _base_events()[:3]
    snapshot_ref = "crp://default/routing-snapshot.json"
    payloads = {snapshot_ref: _routing_snapshot()}
    events.append(_event(4, "model.routed", "running", tool=False, payload_ref=snapshot_ref))

    simple = build_execution_projection(events, view="simple", payload_loader=payloads.__getitem__)
    developer = build_execution_projection(events, view="developer", payload_loader=payloads.__getitem__)

    _validate(developer)
    assert "model_steps" not in simple
    step = developer["model_steps"][0]
    assert step["routing_status"] == "recorded"
    assert step["routing"] == {
        "tier": "standard", "provider_id": "deepseek", "model_id": "deepseek-chat",
        "adapter_kind": "openai-compatible", "route_revision": 4,
    }
    encoded = json.dumps(developer)
    assert "prompt_cache_scope_identity" not in encoded
    assert "must-not-project" not in encoded


def test_nested_model_projects_the_consistent_parent_tool_call_only_to_developer() -> None:
    events = _base_events()[:3]
    snapshot_ref = "crp://default/routing-snapshot.json"
    events[2]["correlation"]["tool_call_id"] = TOOL_CALL_ID
    events.extend([
        _event(4, "model.routed", "running", tool=True, payload_ref=snapshot_ref),
        _event(5, "model.completed", "running", tool=True),
    ])

    simple = build_execution_projection(events, view="simple", payload_loader={snapshot_ref: _routing_snapshot()}.__getitem__)
    developer = build_execution_projection(events, view="developer", payload_loader={snapshot_ref: _routing_snapshot()}.__getitem__)

    _validate(developer)
    assert "model_steps" not in simple
    assert developer["model_steps"][0]["parent_tool_call_id"] == TOOL_CALL_ID


def test_model_parent_tool_call_drift_is_not_projected() -> None:
    events = _base_events()[:3]
    events[2]["correlation"]["tool_call_id"] = TOOL_CALL_ID
    drifted = _event(4, "model.routed", "running", tool=True)
    drifted["correlation"]["tool_call_id"] = "tool-call-drift"
    events.append(drifted)

    developer = build_execution_projection(events, view="developer")

    assert developer["model_steps"][0]["parent_tool_call_id"] is None


def test_model_parent_tool_call_requires_a_nonempty_bounded_value() -> None:
    events = _base_events()[:3]
    developer = build_execution_projection(events, view="developer")
    developer["model_steps"][0]["parent_tool_call_id"] = ""

    with pytest.raises(AIKernelContractError, match="parent tool call"):
        validate_execution_projection(developer)


def test_model_routed_rejects_snapshot_turn_or_step_drift() -> None:
    events = _base_events()[:3]
    snapshot_ref = "crp://default/routing-snapshot.json"
    snapshot = _routing_snapshot()
    snapshot["turn"] = {"turn_id": "turn-wrong"}
    events.append(_event(4, "model.routed", "running", tool=False, payload_ref=snapshot_ref))

    step = build_execution_projection(events, view="developer", payload_loader={snapshot_ref: snapshot}.__getitem__)["model_steps"][0]

    assert step["routing_status"] == "not_recorded"
    assert step["routing"] is None


def test_prompt_cache_receipt_projects_only_cross_bound_provider_counters() -> None:
    events = _base_events()[:3]
    snapshot_ref = "crp://default/routing-snapshot.json"
    receipt_ref = MODEL_RECEIPT_REF
    cache_ref = "crp://default/prompt-cache-receipt.json"
    snapshot = _routing_snapshot()
    payloads = {
        snapshot_ref: snapshot,
        receipt_ref: _model_receipt("completed"),
        cache_ref: _prompt_cache_receipt(snapshot),
    }
    events.extend([
        _event(4, "model.routed", "running", tool=False, payload_ref=snapshot_ref),
        _event(5, "model.completed", "running", tool=False, receipt_ref=receipt_ref, evidence_refs=[cache_ref]),
    ])

    developer = build_execution_projection(events, view="developer", payload_loader=payloads.__getitem__)

    _validate(developer)
    cache = developer["model_steps"][0]["prompt_cache"]
    assert cache == {
        "status": "reported", "source": "provider_usage", "cache_read_input_tokens": 16,
        "cache_write_input_tokens": None, "uncached_input_tokens": 8,
    }
    encoded = json.dumps(developer)
    assert "prompt_cache_scope_identity" not in encoded
    assert "routing_snapshot_revision" not in encoded
    assert "prompt-cache-receipt" not in encoded


def test_prompt_cache_receipt_with_wrong_route_binding_is_not_projected() -> None:
    events = _base_events()[:3]
    snapshot_ref = "crp://default/routing-snapshot.json"
    receipt_ref = MODEL_RECEIPT_REF
    cache_ref = "crp://default/prompt-cache-receipt.json"
    snapshot = _routing_snapshot()
    cache = _prompt_cache_receipt(snapshot)
    cache["provider_id"] = "other-provider"
    payloads = {snapshot_ref: snapshot, receipt_ref: _model_receipt("completed"), cache_ref: cache}
    events.extend([
        _event(4, "model.routed", "running", tool=False, payload_ref=snapshot_ref),
        _event(5, "model.completed", "running", tool=False, receipt_ref=receipt_ref, evidence_refs=[cache_ref]),
    ])

    step = build_execution_projection(events, view="developer", payload_loader=payloads.__getitem__)["model_steps"][0]

    assert step["prompt_cache_status"] == "not_recorded"
    assert step["prompt_cache"] is None


def test_model_wire_attempts_project_only_paired_frozen_route_metadata() -> None:
    events = _base_events()[:3]
    snapshot_ref = "crp://default/routing-snapshot.json"
    dispatch_ref = "crp://default/model-wire-dispatch.json"
    receipt_ref = "crp://default/model-wire-attempt.json"
    snapshot = _routing_snapshot()
    payloads = {
        snapshot_ref: snapshot,
        dispatch_ref: _wire_dispatch(snapshot),
        receipt_ref: _wire_attempt_receipt(snapshot),
    }
    events.extend([
        _event(4, "model.routed", "running", tool=False, payload_ref=snapshot_ref),
        _event(5, "model.attempt.dispatched", "running", tool=False, payload_ref=dispatch_ref),
        _event(6, "model.attempt.terminal", "running", tool=False, receipt_ref=receipt_ref),
    ])

    simple = build_execution_projection(events, view="simple", payload_loader=payloads.__getitem__)
    developer = build_execution_projection(events, view="developer", payload_loader=payloads.__getitem__)

    _validate(developer)
    assert "wire_attempts" not in json.dumps(simple)
    assert "model-wire-attempt" not in json.dumps(simple)
    step = developer["model_steps"][0]
    assert step["wire_attempts_status"] == "recorded"
    assert step["wire_attempts"] == [{
        "attempt_number": 1, "status": "succeeded", "provider_id": "deepseek",
        "model_id": "deepseek-chat", "execution_location": "remote",
        "started_at": "2026-08-24T00:00:05+00:00",
        "completed_at": "2026-08-24T00:00:06+00:00", "duration_ms": 1000,
        "usage_status": "reported", "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        "cache_status": "reported",
        "cache_metadata": {"source_format": "provider_usage", "cache_read_input_tokens": 2, "cache_write_input_tokens": None, "uncached_input_tokens": 1},
        "error_code": None,
    }]
    encoded = json.dumps(developer)
    assert "attempt_id" not in encoded
    assert "routing_snapshot_revision" not in encoded

    historical = copy.deepcopy(developer)
    historical["model_steps"][0]["wire_attempts"][0].pop("execution_location")
    validate_execution_projection(historical)
    _validate(historical)


@pytest.mark.parametrize("mutate", ["orphan", "binding_drift", "location_drift", "duplicate_terminal", "unfinished"])
def test_model_wire_attempt_integrity_drift_is_not_projected(mutate) -> None:
    events = _base_events()[:3]
    snapshot_ref = "crp://default/routing-snapshot.json"
    dispatch_ref = "crp://default/model-wire-dispatch.json"
    receipt_ref = "crp://default/model-wire-attempt.json"
    snapshot = _routing_snapshot()
    dispatch = _wire_dispatch(snapshot)
    receipt = _wire_attempt_receipt(snapshot)
    payloads = {snapshot_ref: snapshot, dispatch_ref: dispatch, receipt_ref: receipt}
    events.append(_event(4, "model.routed", "running", tool=False, payload_ref=snapshot_ref))
    if mutate != "orphan":
        events.append(_event(5, "model.attempt.dispatched", "running", tool=False, payload_ref=dispatch_ref))
    if mutate == "binding_drift":
        receipt["provider_id"] = "other-provider"
    if mutate == "location_drift":
        receipt["execution_location"] = "local_loopback"
    if mutate != "unfinished":
        events.append(_event(6 if mutate != "orphan" else 5, "model.attempt.terminal", "running", tool=False, receipt_ref=receipt_ref))
    if mutate == "duplicate_terminal":
        events.append(_event(7, "model.attempt.terminal", "running", tool=False, receipt_ref=receipt_ref))

    step = build_execution_projection(events, view="developer", payload_loader=payloads.__getitem__)["model_steps"][0]

    assert step["wire_attempts_status"] == "not_recorded"
    assert step["wire_attempts"] == []


@pytest.mark.parametrize("order", ["before_route", "after_logical_terminal"])
def test_model_wire_attempt_event_order_drift_is_not_projected(order: str) -> None:
    events = _base_events()[:3]
    snapshot_ref = "crp://default/routing-snapshot.json"
    dispatch_ref = "crp://default/model-wire-dispatch.json"
    receipt_ref = "crp://default/model-wire-attempt.json"
    snapshot = _routing_snapshot()
    payloads = {
        snapshot_ref: snapshot,
        dispatch_ref: _wire_dispatch(snapshot),
        receipt_ref: _wire_attempt_receipt(snapshot),
    }
    if order == "before_route":
        events.extend([
            _event(4, "model.attempt.dispatched", "running", tool=False, payload_ref=dispatch_ref),
            _event(5, "model.attempt.terminal", "completed", tool=False, receipt_ref=receipt_ref),
            _event(6, "model.routed", "running", tool=False, payload_ref=snapshot_ref),
        ])
    else:
        events.extend([
            _event(4, "model.routed", "running", tool=False, payload_ref=snapshot_ref),
            _event(5, "model.completed", "running", tool=False),
            _event(6, "model.attempt.dispatched", "running", tool=False, payload_ref=dispatch_ref),
            _event(7, "model.attempt.terminal", "completed", tool=False, receipt_ref=receipt_ref),
        ])

    step = build_execution_projection(
        events, view="developer", payload_loader=payloads.__getitem__,
    )["model_steps"][0]

    assert step["wire_attempts_status"] == "not_recorded"
    assert step["wire_attempts"] == []


def _completed_events():
    events, payloads = _tool_prefix()
    payloads["crp://default/outcome.json"] = _outcome("completed", "confirmed_none", attempt=1)
    events.extend([
        _event(9, "tool.outcome.recorded", "running", payload_ref="crp://default/outcome.json"),
        _event(10, "tool.completed", "running", payload_ref="crp://default/result.json", evidence_refs=["crp://default/memory/atom-1"]),
        _event(11, "model.requested", "running", model_request_id="model-request-ffffffffffffffffffffffffffffffff"),
        _event(12, "model.completed", "running", model_request_id="model-request-ffffffffffffffffffffffffffffffff"),
        _event(13, "turn.completed", "completed"),
    ])
    return events, payloads


def _waiting_events():
    payloads = {"crp://default/boundary.json": {
        "schema_version": "1.0.0", "outcome": "ask",
        "reason_codes": ["soft_sensitive", "api_key=must-not-project"], "matched_grant_ids": [],
        "policy_revision": 1, "requires_receipt": False, "redaction_required": True,
    }}
    events = _base_events()
    events.extend([
        _event(5, "tool.requested", "running", payload_ref="crp://default/boundary.json"),
        _event(6, "approval.required", "waiting_approval", payload_ref="crp://default/private-approval.json"),
    ])
    return events, payloads


def _retry_events():
    events, payloads = _tool_prefix()
    payloads["crp://default/failure.json"] = {
        "schema_version": "1.0.0", "invocation_id": TOOL_CALL_ID,
        "turn_id": TURN_ID, "capability_id": "memory.recall", "attempt": 1,
        "error_code": "temporarily_unavailable", "effect_certainty": "confirmed_none", "backoff_ms": 250,
    }
    payloads["crp://default/outcome.json"] = _outcome("completed", "confirmed_none", attempt=2)
    events.extend([
        _event(9, "tool.attempt.failed", "running", payload_ref="crp://default/failure.json", error_code="temporarily_unavailable", retryable=True),
        _event(10, "tool.started", "running"),
        _event(11, "tool.outcome.recorded", "running", payload_ref="crp://default/outcome.json"),
        _event(12, "tool.completed", "running"),
        _event(13, "turn.completed", "completed"),
    ])
    return events, payloads


def _terminal_tool_events(outcome_status, certainty, turn_type, turn_status):
    events, payloads = _tool_prefix()
    payloads["crp://default/outcome.json"] = _outcome(outcome_status, certainty, attempt=1)
    events.extend([
        _event(9, "turn.cancel.requested", "running"),
        _event(10, "tool.outcome.recorded", "running", payload_ref="crp://default/outcome.json", error_code="ai.tool_cancel_unconfirmed" if outcome_status == "unknown_effect" else "ai.tool_cancelled"),
        _event(11, "tool.failed" if outcome_status == "unknown_effect" else "tool.cancelled", "running"),
        _event(12, turn_type, turn_status, error_code="ai.tool_cancel_unconfirmed" if outcome_status == "unknown_effect" else None),
    ])
    return events, payloads


def _tool_prefix():
    payloads = {
        "crp://default/intent.json": {
            "schema_version": "1.0.0", "invocation_id": TOOL_CALL_ID, "turn_id": TURN_ID,
            "step_id": STEP_ID, "capability_id": "memory.recall", "capability_version": 1,
            "operation_id": "op-projection-0001", "idempotency_key": "opaque",
            "execution_mode": "parallel", "resource_locks": [], "idempotency": "idempotent",
            "max_attempts": 2, "retry_backoff_ms": 250,
            "retryable_error_codes": ["temporarily_unavailable"], "timeout_ms": 10000,
            "arguments": {"private": "must-not-project"},
        },
    }
    events = _base_events()
    events.extend([
        _event(5, "tool.requested", "running"),
        _event(6, "tool.intent.recorded", "running", payload_ref="crp://default/intent.json"),
        _event(7, "tool.dispatch.claimed", "running"),
        _event(8, "tool.started", "running"),
    ])
    return events, payloads


def _base_events():
    return [
        _event(1, "turn.accepted", "accepted", tool=False, model=False),
        _event(2, "context.resolved", "running", tool=False, model=False),
        _event(3, "model.requested", "running", tool=False),
        _event(4, "model.completed", "running", tool=False),
    ]


def _event(
    sequence,
    event_type,
    status,
    *,
    payload_ref=None,
    evidence_refs=None,
    error_code=None,
    receipt_ref=None,
    retryable=False,
    tool=True,
    model=True,
    model_request_id=MODEL_REQUEST_ID,
):
    return {
        "schema_version": "1.0.0", "event_id": f"event-{sequence:032x}",
        "turn_id": TURN_ID, "session_id": "session-projection", "sequence": sequence,
        "type": event_type, "actor": "kernel",
        "correlation": {
            "step_id": STEP_ID if tool or model else None,
            "tool_call_id": TOOL_CALL_ID if tool else None,
            "model_request_id": model_request_id if model and event_type in {"model.requested", "model.routed", "model.attempt.dispatched", "model.attempt.terminal", "model.completed", "model.failed", "model.cancelled", "model.timed_out"} else None,
            "operation_id": "op-projection-0001",
        },
        "data": {
            "status": status, "summary": "api_key=must-not-project C:\\private.txt",
            "capability_id": "memory.recall" if tool else None,
            "payload_ref": payload_ref, "receipt_ref": receipt_ref,
            "evidence_refs": evidence_refs or [], "error_code": error_code,
            "retryable": retryable,
        },
        "occurred_at": f"2026-08-24T00:00:{sequence:02d}+00:00",
    }


def _outcome(status, certainty, *, attempt):
    return {
        "schema_version": "1.0.0", "invocation_id": TOOL_CALL_ID,
        "turn_id": TURN_ID, "capability_id": "memory.recall", "attempt": attempt,
        "status": status, "effect_certainty": certainty, "payload_ref": None,
        "receipt_ref": None, "evidence_refs": [],
        "error_code": "ai.tool_cancel_unconfirmed" if status == "unknown_effect" else None,
        "retryable": False,
    }


def _model_receipt(status):
    return {
        "schema_version": "1.0.0",
        "receipt_id": "model-receipt-0123456789abcdef",
        "turn_id": TURN_ID,
        "model_request_id": MODEL_REQUEST_ID,
        "status": status,
        "requested_at": "2026-08-24T00:00:03+00:00",
        "completed_at": "2026-08-24T00:00:04+00:00",
        "duration_ms": 1000,
        "provider_id": "deepseek",
        "model_id": "deepseek-chat",
        "usage_status": "recorded",
        "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        "input_recorded": False,
        "output_recorded": False,
        "error_code": None if status == "completed" else "ai.model_call_terminal",
    }


def _routing_snapshot():
    return {
        "schema_version": "1.0.0",
        "turn": {"turn_id": TURN_ID},
        "project": {"project_id": "project-default"},
        "profile": {"profile_id": "profile-default", "profile_revision": 2, "preferred_model_tier": "standard"},
        "boundary": {"profile_id": "boundary-default", "profile_revision": 3},
        "requirement": {"must-not-project": True},
        "routing": {"must-not-project": True},
        "registry": {"must-not-project": True},
        "runtime": {"must-not-project": True},
        "activation": {"must-not-project": True},
        "tiers": [],
        "selected": {
            "tier": "standard", "route_key": "route-deepseek", "route_revision": 4,
            "provider_id": "deepseek", "provider_revision": "provider-revision-4",
            "model_name": "deepseek-chat", "adapter_kind": "openai-compatible",
            "execution_location": "remote", "reason": "project_tier",
        },
        "catalog_revision": "a" * 64,
        "prompt_cache_scope": {"identity": "b" * 64},
    }


def _prompt_cache_receipt(snapshot):
    revision = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "1.0.0", "receipt_id": "prompt-cache-receipt-0123456789abcdef",
        "turn_id": TURN_ID, "model_request_id": MODEL_REQUEST_ID,
        "routing_snapshot_revision": revision, "prompt_cache_scope_identity": snapshot["prompt_cache_scope"]["identity"],
        "provider_id": "deepseek", "model_id": "deepseek-chat", "cache_status": "reported",
        "source_format": "provider_usage", "cache_read_input_tokens": 16,
        "cache_write_input_tokens": None, "uncached_input_tokens": 8,
        "input_recorded": False, "output_recorded": False,
    }


def _wire_dispatch(snapshot):
    revision = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "1.0.0", "attempt_id": "model-wire-attempt-0123456789abcdef",
        "turn_id": TURN_ID, "model_request_id": MODEL_REQUEST_ID, "attempt_number": 1,
        "routing_snapshot_revision": revision, "provider_id": "deepseek", "model_id": "deepseek-chat",
        "execution_location": "remote",
        "dispatched_at": "2026-08-24T00:00:05+00:00", "input_stored": False, "output_stored": False,
    }


def _wire_attempt_receipt(snapshot):
    dispatch = _wire_dispatch(snapshot)
    dispatch.pop("dispatched_at")
    return {
        **dispatch,
        "status": "succeeded", "started_at": "2026-08-24T00:00:05+00:00",
        "completed_at": "2026-08-24T00:00:06+00:00", "duration_ms": 1000,
        "usage_status": "reported", "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        "cache_status": "reported",
        "cache_metadata": {
            "source_format": "provider_usage", "cache_read_input_tokens": 2,
            "cache_write_input_tokens": None, "uncached_input_tokens": 1,
        },
        "input_stored": False, "output_stored": False, "error_code": None,
    }


def _validate(value):
    schema = json.loads((ROOT / "core-contracts" / "ai" / "execution-projection.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = tuple(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value))
    assert not errors, [error.message for error in errors]
