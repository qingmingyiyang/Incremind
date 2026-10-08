from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from core.ai_kernel import (
    AIKernelContractError,
    validate_capability_manifest,
    validate_event_transition,
    validate_model_wire_attempt_receipt,
    validate_turn_action,
    validate_turn_request,
)
from core.ai_kernel.contracts import (
    validate_model_call_receipt,
    validate_model_wire_attempt_dispatch,
    validate_prompt_cache_receipt,
)


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "ai"


@pytest.mark.parametrize(
    ("contract", "fixture_dir"),
    [
        ("turn-request.schema.json", "turn-request"),
        ("turn-event.schema.json", "turn-event"),
        ("turn-action.schema.json", "turn-action"),
        ("capability-manifest.schema.json", "capability-manifest"),
    ],
)
def test_ai_kernel_contracts_accept_valid_and_reject_invalid_fixtures(
    contract: str,
    fixture_dir: str,
) -> None:
    schema = _json(CONTRACT_ROOT / contract)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    fixtures = CONTRACT_ROOT / "fixtures" / fixture_dir
    valid = sorted(fixtures.glob("valid-*.json"))
    invalid = sorted(fixtures.glob("invalid-*.json"))
    assert valid and invalid
    for path in valid:
        assert list(validator.iter_errors(_json(path))) == [], path.name
    for path in invalid:
        assert validator.is_valid(_json(path)) is False, path.name


def test_turn_request_requires_consented_remote_pii_and_consistent_capability_policy() -> None:
    request = _json(CONTRACT_ROOT / "fixtures" / "turn-request" / "valid-project-answer.json")
    assert validate_turn_request(request)["turn_id"] == request["turn_id"]

    remote = copy.deepcopy(request)
    remote["privacy"].update(mode="remote_allowed", allow_remote=True)
    with pytest.raises(AIKernelContractError, match="consent"):
        validate_turn_request(remote)

    conflict = copy.deepcopy(request)
    conflict["capability_policy"]["denied"] = ["memory.recall"]
    with pytest.raises(AIKernelContractError, match="both allowed and denied"):
        validate_turn_request(conflict)

    unknown = copy.deepcopy(request)
    unknown["provider"] = "bypass"
    with pytest.raises(AIKernelContractError, match="unknown fields"):
        validate_turn_request(unknown)

    nested_unknown = copy.deepcopy(request)
    nested_unknown["privacy"]["bypass_consent"] = True
    with pytest.raises(AIKernelContractError, match="unknown fields"):
        validate_turn_request(nested_unknown)


def test_turn_request_exact_capability_is_schema_checked_and_policy_bounded() -> None:
    request = _json(CONTRACT_ROOT / "fixtures" / "turn-request" / "valid-project-answer.json")
    request["capability_request"] = {
        "mode": "execute_exact_v1",
        "capability_id": "memory.recall",
        "arguments": {"query": "only this capability"},
    }
    schema = _json(CONTRACT_ROOT / "turn-request.schema.json")
    validator = Draft202012Validator(schema)
    assert validator.is_valid(request)
    assert validate_turn_request(request)["capability_request"] == request["capability_request"]

    unallowed = copy.deepcopy(request)
    unallowed["capability_request"]["capability_id"] = "document.draft"
    with pytest.raises(AIKernelContractError, match="allowed capability"):
        validate_turn_request(unallowed)
    assert validator.is_valid(unallowed)

    malformed = copy.deepcopy(request)
    malformed["capability_request"]["provider"] = "bypass"
    assert validator.is_valid(malformed) is False


def test_turn_request_expert_skill_ids_are_optional_and_unique() -> None:
    request = _json(CONTRACT_ROOT / "fixtures" / "turn-request" / "valid-project-answer.json")
    request["expert_request"] = {
        "expert_id": "video-research-expert",
        "task_intents": ["research"],
        "budget": "research-2k",
        "skill_ids": ["media-comprehension"],
    }
    schema = _json(CONTRACT_ROOT / "turn-request.schema.json")
    validator = Draft202012Validator(schema)

    assert validator.is_valid(request)
    assert validate_turn_request(request)["expert_request"] == request["expert_request"]

    duplicate = copy.deepcopy(request)
    duplicate["expert_request"]["skill_ids"] *= 2
    assert validator.is_valid(duplicate) is False
    with pytest.raises(AIKernelContractError, match="skill ids"):
        validate_turn_request(duplicate)


def test_turn_request_accepts_only_a_strict_internal_subagent_binding() -> None:
    request = _json(CONTRACT_ROOT / "fixtures" / "turn-request" / "valid-project-answer.json")
    binding = {
        "schema_version": "1.0.0",
        "kind": "internal_agent_run_v1",
        "run_id": "agent-run-child-001",
        "role": "subagent",
        "profile_id": "subagent.researcher",
        "profile_revision": 3,
        "model_tier": "standard",
        "parent_run_id": "agent-run-parent-001",
        "link_id": "agent-link-child-001",
        "reservation_id": "agent-reservation-001",
        "spawn_operation_id": "op-agent-spawn-child-001",
        "depth": 1,
        "cancel_epoch": 0,
        "budget_snapshot_ref": "crp://agent-runs/agent-run-child-001/budget-snapshot",
    }
    request["agent_binding"] = binding
    schema = _json(CONTRACT_ROOT / "turn-request.schema.json")
    validator = Draft202012Validator(schema)

    assert validator.is_valid(request)
    assert validate_turn_request(request)["agent_binding"] == binding

    forged = copy.deepcopy(request)
    forged["agent_binding"]["role"] = "main"
    assert validator.is_valid(forged) is False
    with pytest.raises(AIKernelContractError, match="unknown fields"):
        validate_turn_request(forged)

    main = copy.deepcopy(request)
    main["agent_binding"] = {
        "schema_version": "1.0.0",
        "kind": "internal_agent_run_v1",
        "run_id": "agent-run-main-001",
        "role": "main",
        "profile_id": "main.orchestrator",
        "profile_revision": 4,
        "model_tier": "standard",
        "depth": 0,
        "cancel_epoch": 0,
        "budget_snapshot_ref": "crp://agent-runs/agent-run-main-001/budget-snapshot",
    }
    assert validator.is_valid(main)
    assert validate_turn_request(main)["agent_binding"] == main["agent_binding"]

    malformed_main = copy.deepcopy(main)
    malformed_main["agent_binding"]["parent_run_id"] = "agent-run-parent-001"
    assert validator.is_valid(malformed_main) is False
    with pytest.raises(AIKernelContractError, match="unknown fields"):
        validate_turn_request(malformed_main)

    modality_tier = copy.deepcopy(request)
    modality_tier["agent_binding"]["model_tier"] = "vision"
    assert validator.is_valid(modality_tier) is False
    with pytest.raises(AIKernelContractError, match="model tier"):
        validate_turn_request(modality_tier)


def test_turn_request_series_authority_is_required_strict_and_scope_bound() -> None:
    request = _json(CONTRACT_ROOT / "fixtures" / "turn-request" / "valid-project-answer.json")
    request["scope"] = {
        "kind": "series", "project_id": "project-alpha", "series_id": "series-alpha",
        "authority": {
            "kind": "project_series_scope_v1", "object_id": "series-memory-alpha",
            "payload_revision": 7, "storage_revision": 3,
            "authority_identity": "json:object-store-v1",
            "authority_ref": "crp://default/memory/series/series-memory-alpha",
        },
    }
    schema = _json(CONTRACT_ROOT / "turn-request.schema.json")
    validator = Draft202012Validator(schema)
    assert validate_turn_request(request)["scope"] == request["scope"]
    assert validator.is_valid(request)

    missing = copy.deepcopy(request)
    missing["scope"].pop("authority")
    with pytest.raises(AIKernelContractError, match="authority"):
        validate_turn_request(missing)

    unknown_kind = copy.deepcopy(request)
    unknown_kind["scope"]["kind"] = "workspace"
    with pytest.raises(AIKernelContractError, match="scope kind"):
        validate_turn_request(unknown_kind)
    assert not validator.is_valid(unknown_kind)

    missing_project = copy.deepcopy(request)
    missing_project["scope"]["project_id"] = None
    with pytest.raises(AIKernelContractError, match="project and series"):
        validate_turn_request(missing_project)
    assert not validator.is_valid(missing_project)

    invalid_project = copy.deepcopy(request)
    invalid_project["scope"]["project_id"] = "bad project!"
    with pytest.raises(AIKernelContractError, match="project identity"):
        validate_turn_request(invalid_project)
    assert not validator.is_valid(invalid_project)

    invalid_series = copy.deepcopy(request)
    invalid_series["scope"]["series_id"] = "bad series!"
    with pytest.raises(AIKernelContractError, match="series identity"):
        validate_turn_request(invalid_series)
    assert not validator.is_valid(invalid_series)

    integral_revision = copy.deepcopy(request)
    integral_revision["scope"]["authority"]["payload_revision"] = 7.0
    assert validator.is_valid(integral_revision)
    assert validate_turn_request(integral_revision)["scope"] == integral_revision["scope"]

    padded_project = copy.deepcopy(request)
    padded_project["scope"]["project_id"] = " project-alpha "
    with pytest.raises(AIKernelContractError, match="project identity"):
        validate_turn_request(padded_project)
    assert not validator.is_valid(padded_project)

    padded_ref = copy.deepcopy(request)
    padded_ref["scope"]["authority"]["authority_ref"] = " crp://default/memory/series/series-memory-alpha "
    with pytest.raises(AIKernelContractError, match="authority ref"):
        validate_turn_request(padded_ref)
    assert not validator.is_valid(padded_ref)
    assert validator.is_valid(missing) is False

    bad_ref = copy.deepcopy(request)
    bad_ref["scope"]["authority"]["authority_ref"] = "crp://default/secret/token"
    with pytest.raises(AIKernelContractError, match="ref"):
        validate_turn_request(bad_ref)
    assert validator.is_valid(bad_ref) is False

    project = _json(CONTRACT_ROOT / "fixtures" / "turn-request" / "valid-project-answer.json")
    project["scope"]["authority"] = request["scope"]["authority"]
    with pytest.raises(AIKernelContractError, match="non-series"):
        validate_turn_request(project)
    assert validator.is_valid(project) is False


def test_turn_action_requires_human_targeted_approval() -> None:
    action = _json(CONTRACT_ROOT / "fixtures" / "turn-action" / "valid-approve.json")
    assert validate_turn_action(action)["type"] == "approve"
    action["target_event_id"] = None
    with pytest.raises(AIKernelContractError, match="target approval event"):
        validate_turn_action(action)


def test_capability_write_requires_approval_and_operation_receipt() -> None:
    manifest = _json(
        CONTRACT_ROOT / "fixtures" / "capability-manifest" / "valid-memory-recall.json"
    )
    assert validate_capability_manifest(manifest)["capability_id"] == "memory.recall"
    manifest.update(mode="write", requires_approval=True, operation_semantics="read_only")
    with pytest.raises(AIKernelContractError, match="operation receipt"):
        validate_capability_manifest(manifest)


def test_turn_event_log_is_contiguous_identity_bound_and_terminal() -> None:
    accepted = _event(1, "turn.accepted", "accepted")
    running = _event(2, "context.resolved", "running")
    completed = _event(3, "turn.completed", "completed")
    assert validate_event_transition(None, accepted)["sequence"] == 1
    assert validate_event_transition(accepted, running)["sequence"] == 2
    assert validate_event_transition(running, completed)["sequence"] == 3

    with pytest.raises(AIKernelContractError, match="terminal"):
        validate_event_transition(completed, _event(4, "model.requested", "running"))
    with pytest.raises(AIKernelContractError, match="contiguous"):
        validate_event_transition(accepted, _event(3, "context.resolved", "running"))


def test_model_call_receipt_is_strict_terminal_metadata_only() -> None:
    receipt = _model_receipt()
    schema = _json(CONTRACT_ROOT / "model-call-receipt.schema.json")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    assert validate_model_call_receipt(receipt)["usage_status"] == "recorded"
    assert validator.is_valid(receipt)

    unrecorded = copy.deepcopy(receipt)
    unrecorded.update(usage_status="not_recorded", usage=None)
    assert validate_model_call_receipt(unrecorded)["usage"] is None
    assert validator.is_valid(unrecorded)

    leaked = copy.deepcopy(receipt)
    leaked["messages"] = [{"role": "user", "content": "private"}]
    with pytest.raises(AIKernelContractError, match="unknown fields"):
        validate_model_call_receipt(leaked)
    assert validator.is_valid(leaked) is False

    bad_usage = copy.deepcopy(receipt)
    bad_usage["usage"] = None
    with pytest.raises(AIKernelContractError, match="usage"):
        validate_model_call_receipt(bad_usage)
    assert validator.is_valid(bad_usage) is False

    bad_output = copy.deepcopy(receipt)
    bad_output["output_recorded"] = True
    with pytest.raises(AIKernelContractError, match="input or output"):
        validate_model_call_receipt(bad_output)

    inconsistent = copy.deepcopy(receipt)
    inconsistent["status"] = "failed"
    with pytest.raises(AIKernelContractError, match="requires an error code"):
        validate_model_call_receipt(inconsistent)
    assert validator.is_valid(inconsistent) is False


def test_prompt_cache_receipt_is_strict_provider_reported_metadata_only() -> None:
    receipt = _prompt_cache_receipt()
    schema = _json(CONTRACT_ROOT / "prompt-cache-receipt.schema.json")
    validator = Draft202012Validator(schema)

    assert validate_prompt_cache_receipt(receipt)["cache_status"] == "reported"
    assert validator.is_valid(receipt)

    unavailable = copy.deepcopy(receipt)
    unavailable.update(
        cache_status="unavailable", source_format="unavailable",
        cache_read_input_tokens=None, cache_write_input_tokens=None, uncached_input_tokens=None,
    )
    assert validate_prompt_cache_receipt(unavailable)["source_format"] == "unavailable"
    assert validator.is_valid(unavailable)

    leaked = copy.deepcopy(receipt)
    leaked["raw_response"] = {"cache_key": "private"}
    with pytest.raises(AIKernelContractError, match="unknown fields"):
        validate_prompt_cache_receipt(leaked)
    assert validator.is_valid(leaked) is False

    unbound = copy.deepcopy(receipt)
    unbound["prompt_cache_scope_identity"] = "scope-1"
    with pytest.raises(AIKernelContractError, match="scope"):
        validate_prompt_cache_receipt(unbound)
    assert validator.is_valid(unbound) is False

    invented = copy.deepcopy(receipt)
    invented.update(cache_status="reported", source_format="provider_usage")
    invented["cache_read_input_tokens"] = None
    invented["cache_write_input_tokens"] = None
    invented["uncached_input_tokens"] = None
    with pytest.raises(AIKernelContractError, match="requires a token count"):
        validate_prompt_cache_receipt(invented)
    assert validator.is_valid(invented) is False

    bad_status = copy.deepcopy(unavailable)
    bad_status["cache_read_input_tokens"] = 0
    with pytest.raises(AIKernelContractError, match="unavailable"):
        validate_prompt_cache_receipt(bad_status)
    assert validator.is_valid(bad_status) is False

    recorded = copy.deepcopy(receipt)
    recorded["input_recorded"] = True
    with pytest.raises(AIKernelContractError, match="cannot record input or output"):
        validate_prompt_cache_receipt(recorded)
    assert validator.is_valid(recorded) is False


def test_model_wire_attempt_receipt_is_strict_terminal_metadata_only() -> None:
    receipt = _model_wire_attempt_receipt()
    schema = _json(CONTRACT_ROOT / "model-wire-attempt-receipt.schema.json")
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    assert validate_model_wire_attempt_receipt(receipt)["status"] == "succeeded"
    assert validator.is_valid(receipt)

    historical = copy.deepcopy(receipt)
    historical.pop("execution_location")
    assert validate_model_wire_attempt_receipt(historical).get("execution_location") is None
    assert validator.is_valid(historical)

    invalid_location = copy.deepcopy(receipt)
    invalid_location["execution_location"] = "private_network"
    with pytest.raises(AIKernelContractError, match="execution location"):
        validate_model_wire_attempt_receipt(invalid_location)
    assert validator.is_valid(invalid_location) is False

    unavailable = copy.deepcopy(receipt)
    unavailable.update(usage_status="unavailable", usage=None, cache_status="unavailable", cache_metadata=None)
    assert validate_model_wire_attempt_receipt(unavailable)["usage"] is None
    assert validator.is_valid(unavailable)

    transport_failed = copy.deepcopy(unavailable)
    transport_failed.update(status="failed_transport", error_code="ai.provider_timeout")
    assert validate_model_wire_attempt_receipt(transport_failed)["status"] == "failed_transport"
    assert validator.is_valid(transport_failed)

    cancelled = copy.deepcopy(unavailable)
    cancelled.update(status="consumer_cancelled", error_code="ai.consumer_cancelled")
    assert validate_model_wire_attempt_receipt(cancelled)["status"] == "consumer_cancelled"
    assert validator.is_valid(cancelled)

    invalid_cancel = copy.deepcopy(cancelled)
    invalid_cancel["error_code"] = "ai.provider_timeout"
    with pytest.raises(AIKernelContractError, match="consumer-cancelled"):
        validate_model_wire_attempt_receipt(invalid_cancel)
    assert validator.is_valid(invalid_cancel) is False

    leaked = copy.deepcopy(receipt)
    leaked["response_body"] = "private provider response"
    with pytest.raises(AIKernelContractError, match="unknown fields"):
        validate_model_wire_attempt_receipt(leaked)
    assert validator.is_valid(leaked) is False

    nested_leak = copy.deepcopy(receipt)
    nested_leak["cache_metadata"]["cache_key"] = "private"
    with pytest.raises(AIKernelContractError, match="unknown fields"):
        validate_model_wire_attempt_receipt(nested_leak)
    assert validator.is_valid(nested_leak) is False

    unbound = copy.deepcopy(receipt)
    unbound["routing_snapshot_revision"] = "scope-1"
    with pytest.raises(AIKernelContractError, match="routing_snapshot_revision"):
        validate_model_wire_attempt_receipt(unbound)
    assert validator.is_valid(unbound) is False

    malformed = copy.deepcopy(receipt)
    malformed["attempt_number"] = 0
    with pytest.raises(AIKernelContractError, match="attempt number"):
        validate_model_wire_attempt_receipt(malformed)
    assert validator.is_valid(malformed) is False

    stored = copy.deepcopy(receipt)
    stored["input_stored"] = True
    with pytest.raises(AIKernelContractError, match="cannot store input or output"):
        validate_model_wire_attempt_receipt(stored)
    assert validator.is_valid(stored) is False

    invalid_error = copy.deepcopy(receipt)
    invalid_error["error_code"] = "ai.provider_timeout"
    with pytest.raises(AIKernelContractError, match="cannot carry an error code"):
        validate_model_wire_attempt_receipt(invalid_error)
    assert validator.is_valid(invalid_error) is False

    missing_error = copy.deepcopy(receipt)
    missing_error["status"] = "failed_transport"
    with pytest.raises(AIKernelContractError, match="requires an error code"):
        validate_model_wire_attempt_receipt(missing_error)
    assert validator.is_valid(missing_error) is False


def test_model_wire_attempt_dispatch_is_strict_pre_egress_metadata_only() -> None:
    dispatch = _model_wire_attempt_dispatch()
    schema = _json(CONTRACT_ROOT / "model-wire-attempt-dispatch.schema.json")
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    assert validate_model_wire_attempt_dispatch(dispatch)["attempt_number"] == 1
    assert validator.is_valid(dispatch)

    historical = copy.deepcopy(dispatch)
    historical.pop("execution_location")
    assert validate_model_wire_attempt_dispatch(historical).get("execution_location") is None
    assert validator.is_valid(historical)

    leaked = copy.deepcopy(dispatch)
    leaked["prompt"] = "private"
    with pytest.raises(AIKernelContractError, match="unknown fields"):
        validate_model_wire_attempt_dispatch(leaked)
    assert validator.is_valid(leaked) is False

    invalid_identity = copy.deepcopy(dispatch)
    invalid_identity["attempt_id"] = "model-wire-attempt-short"
    with pytest.raises(AIKernelContractError, match="identity"):
        validate_model_wire_attempt_dispatch(invalid_identity)
    assert validator.is_valid(invalid_identity) is False

    stored = copy.deepcopy(dispatch)
    stored["output_stored"] = True
    with pytest.raises(AIKernelContractError, match="cannot store input or output"):
        validate_model_wire_attempt_dispatch(stored)
    assert validator.is_valid(stored) is False


def _model_receipt() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "receipt_id": "model-receipt-0123456789abcdef",
        "turn_id": "turn-0123456789abcdef0123456789abcdef",
        "model_request_id": "model-request-0123456789abcdef0123456789abcdef",
        "status": "completed",
        "requested_at": "2026-08-24T04:00:00+00:00",
        "completed_at": "2026-08-24T04:00:01+00:00",
        "duration_ms": 1000,
        "provider_id": "deepseek",
        "model_id": "deepseek-chat",
        "usage_status": "recorded",
        "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        "input_recorded": False,
        "output_recorded": False,
        "error_code": None,
    }


def _prompt_cache_receipt() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "receipt_id": "prompt-cache-receipt-0123456789abcdef",
        "turn_id": "turn-0123456789abcdef0123456789abcdef",
        "model_request_id": "model-request-0123456789abcdef0123456789abcdef",
        "routing_snapshot_revision": "a" * 64,
        "prompt_cache_scope_identity": "b" * 64,
        "provider_id": "deepseek",
        "model_id": "deepseek-chat",
        "cache_status": "reported",
        "source_format": "provider_usage",
        "cache_read_input_tokens": 16,
        "cache_write_input_tokens": None,
        "uncached_input_tokens": 8,
        "input_recorded": False,
        "output_recorded": False,
    }


def _model_wire_attempt_receipt() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "attempt_id": "model-wire-attempt-0123456789abcdef",
        "turn_id": "turn-0123456789abcdef0123456789abcdef",
        "model_request_id": "model-request-0123456789abcdef0123456789abcdef",
        "attempt_number": 1,
        "routing_snapshot_revision": "a" * 64,
        "provider_id": "deepseek",
        "model_id": "deepseek-chat",
        "execution_location": "remote",
        "status": "succeeded",
        "started_at": "2026-08-25T04:00:00+00:00",
        "completed_at": "2026-08-25T04:00:01+00:00",
        "duration_ms": 1000,
        "usage_status": "reported",
        "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        "cache_status": "reported",
        "cache_metadata": {
            "source_format": "provider_usage",
            "cache_read_input_tokens": 8,
            "cache_write_input_tokens": None,
            "uncached_input_tokens": 3,
        },
        "input_stored": False,
        "output_stored": False,
        "error_code": None,
    }


def _model_wire_attempt_dispatch() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "attempt_id": "model-wire-attempt-0123456789abcdef",
        "turn_id": "turn-0123456789abcdef0123456789abcdef",
        "model_request_id": "model-request-0123456789abcdef0123456789abcdef",
        "attempt_number": 1,
        "routing_snapshot_revision": "a" * 64,
        "provider_id": "deepseek",
        "model_id": "deepseek-chat",
        "execution_location": "remote",
        "dispatched_at": "2026-08-25T04:00:00+00:00",
        "input_stored": False,
        "output_stored": False,
    }


def _event(sequence: int, event_type: str, status: str) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "event_id": f"event-{sequence:032x}",
        "turn_id": "turn-0123456789abcdef0123456789abcdef",
        "session_id": "session-project-alpha",
        "sequence": sequence,
        "type": event_type,
        "actor": "kernel",
        "correlation": {
            "step_id": None,
            "tool_call_id": None,
            "model_request_id": None,
            "operation_id": "op-project-answer-0001",
        },
        "data": {
            "status": status,
            "summary": event_type,
            "capability_id": None,
            "payload_ref": None,
            "receipt_ref": None,
            "evidence_refs": [],
            "error_code": None,
            "retryable": False,
        },
        "occurred_at": "2026-08-23T04:00:00Z",
    }


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))
