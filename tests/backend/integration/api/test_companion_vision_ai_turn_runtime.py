from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

import pytest

from backend.api.companion_vision_ai_runtime import (
    COMPANION_VISION_ANALYZE_CAPABILITY,
    COMPANION_VISION_CONTEXT_CAPABILITY,
    COMPANION_VISION_OUTCOME,
    CompanionVisionAnalyzeCapability,
    CompanionVisionContextCapability,
    CompanionVisionTurnPlanner,
)
from backend.model_routing_snapshot import (
    payload_requirement_identity,
    validate_turn_model_routing_snapshot,
)
from core.ai_kernel import (
    CapabilityDefinition,
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    InMemoryTurnStateStore,
    ScopedCapabilityRegistry,
    SynchronousAIRuntime,
    validate_prompt_cache_receipt,
)
from core.ai_kernel.contracts import validate_model_call_receipt
from core.model_gateway import ModelResult


@dataclass
class Grant:
    grant_id: str = "vision-grant-test"
    media_type: str = "image/jpeg"
    byte_length: int = 12
    sha256: str = "a" * 64

    def public(self):
        return {
            "grant_id": self.grant_id,
            "media_type": self.media_type,
            "byte_length": self.byte_length,
            "sha256": self.sha256,
        }


class GrantStore:
    def __init__(self):
        self.grant = Grant()
        self.inspect_calls = 0
        self.consume_calls = 0

    def inspect(self, grant_id):
        assert grant_id == self.grant.grant_id
        self.inspect_calls += 1
        return self.grant

    def consume_expected(self, grant_id, expected_public):
        assert grant_id == self.grant.grant_id
        assert expected_public == self.grant.public()
        self.consume_calls += 1
        return self.grant, b"\xff\xd8\xffpixels"


class Gateway:
    def __init__(self):
        self.calls = []

    def invoke(self, request):
        sink = request.metadata_sink
        assert sink is not None
        snapshot = request.parameters["_model_routing_snapshot"]
        sink.model_call_routed(
            snapshot_ref=request.parameters["_model_routing_snapshot_ref"],
            snapshot_revision=request.parameters["_model_routing_snapshot_revision"],
            prompt_cache_scope_identity=snapshot["prompt_cache_scope"]["identity"],
            provider="fixture",
            model="vision-1",
            execution_location="remote",
        )
        self.calls.append(request)
        sink.model_call_started(provider="fixture", model="vision-1")
        sink.model_call_cache_observed(observation={
            "cache_read_input_tokens": 7,
            "cache_miss_input_tokens": 3,
        })
        usage = {"input_tokens": 10, "output_tokens": 6, "total_tokens": 16}
        sink.model_call_completed(usage=usage)
        return ModelResult(output="画面中有一只杯子。", provider="fixture", model="vision-1", usage=usage)


class RejectModelRouteEventStore(InMemoryTurnEventStore):
    def append(self, event, *, expected_sequence, run_lease=None):
        if event.get("type") == "model.routed":
            raise RuntimeError("model route event unavailable")
        return super().append(
            event,
            expected_sequence=expected_sequence,
            run_lease=run_lease,
        )


class RejectNestedModelRequestEventStore(InMemoryTurnEventStore):
    def append(self, event, *, expected_sequence, run_lease=None):
        correlation = event.get("correlation")
        if (
            event.get("type") == "model.requested"
            and isinstance(correlation, dict)
            and correlation.get("tool_call_id") is not None
        ):
            raise RuntimeError("nested model request event unavailable")
        return super().append(
            event,
            expected_sequence=expected_sequence,
            run_lease=run_lease,
        )


class RejectModelReceiptPayloadStore(InMemoryTurnPayloadStore):
    def put(self, turn_id, kind, payload):
        if kind == "model-call-receipt":
            raise RuntimeError("model receipt unavailable")
        return super().put(turn_id, kind, payload)


class CrashAfterToolOutcomeEventStore(InMemoryTurnEventStore):
    def __init__(self):
        super().__init__()
        self.armed = True

    def append(self, event, *, expected_sequence, run_lease=None):
        stored = super().append(
            event,
            expected_sequence=expected_sequence,
            run_lease=run_lease,
        )
        data = event.get("data")
        if (
            self.armed
            and event.get("type") == "tool.outcome.recorded"
            and isinstance(data, dict)
            and data.get("capability_id") == COMPANION_VISION_ANALYZE_CAPABILITY
        ):
            self.armed = False
            raise SystemExit("simulated crash after durable tool outcome")
        return stored


def _runtime(
    *, store=None, gateway=None, revision=1, routing_snapshot="valid", events=None,
    payloads=None,
):
    grants = store or GrantStore()
    payloads = payloads if payloads is not None else InMemoryTurnPayloadStore()
    registry = ScopedCapabilityRegistry()
    if routing_snapshot != "missing":
        payloads.get_or_create_immutable_payload(
            "turn-vision-001",
            "turn-model-routing-snapshot-v1",
            _vision_routing_snapshot(project_id=("default" if routing_snapshot == "valid" else "other-project")),
        )
    def loader():
        return "你是视觉助手", revision, 1
    registry.register(
        CapabilityDefinition(
            COMPANION_VISION_CONTEXT_CAPABILITY, 1, "read", False, "read_only", "crp://in", "crp://out"
        ),
        CompanionVisionContextCapability(grant_store=grants, prompt_profile_loader=loader),
    )
    registry.register(
        CapabilityDefinition(
            COMPANION_VISION_ANALYZE_CAPABILITY, 1, "write", True, "receipt_required", "crp://in", "crp://out"
        ),
        CompanionVisionAnalyzeCapability(
            grant_store=grants, prompt_profile_loader=loader, gateway=gateway, receipt_store=payloads
        ),
    )
    return (
        SynchronousAIRuntime(
            planner=CompanionVisionTurnPlanner(),
            registry=registry,
            events=events or InMemoryTurnEventStore(),
            payloads=payloads,
            state=InMemoryTurnStateStore(),
        ),
        grants,
        payloads,
    )


def _vision_routing_snapshot(*, project_id: str) -> dict[str, object]:
    requirement = {
        "required_capability": "vision",
        "modality": "image_input",
        "output_contract": "text",
        "egress_purpose": "companion_vision",
        "egress_categories": ["image_frame", "instructions"],
        "privacy_scope": "remote_allowed",
        "retention_policy": "turn_only",
        "protocol_version": "1.0.0",
        "capability_ids": [
            COMPANION_VISION_ANALYZE_CAPABILITY,
            COMPANION_VISION_CONTEXT_CAPABILITY,
        ],
        "skill_snapshot_revision": None,
        "context_policy": {
            "include_project_skill": False,
            "include_memory": False,
            "include_session_history": False,
            "max_context_bytes": 4096,
        },
        "input_refs": [],
    }
    selected = {
        "tier": "vision",
        "route_key": "tier.vision",
        "route_revision": 1,
        "provider_id": "fixture",
        "provider_revision": "1",
        "model_name": "vision-1",
        "adapter_kind": "openai-compatible-vision",
        "execution_location": "remote",
        "reason": "integration_test_frozen_route",
    }
    route = {
        "route_key": "tier.vision",
        "provider_id": "fixture",
        "provider_revision": "1",
        "model_name": "vision-1",
        "adapter_kind": "openai-compatible-vision",
        "enabled": True,
        "revision": 1,
    }
    prompt_scope = {
        "project_id": project_id,
        "profile": ["test-project-profile", 1],
        "boundary": ["test-boundary-profile", 1],
        "skill_snapshot_revision": None,
        "protocol_version": "1.0.0",
        "requirement": payload_requirement_identity(
            "vision",
            "image_input",
            "text",
            "companion_vision",
            ("image_frame", "instructions"),
            "remote_allowed",
            "turn_only",
            tuple(requirement["capability_ids"]),
            requirement["context_policy"],
            [],
        ),
        "selected": selected,
    }
    return validate_turn_model_routing_snapshot(
        {
            "schema_version": "1.0.0",
            "turn": {"turn_id": "turn-vision-001"},
            "project": {"project_id": project_id},
            "profile": {"profile_id": "test-project-profile", "profile_revision": 1, "preferred_model_tier": "vision"},
            "boundary": {"profile_id": "test-boundary-profile", "profile_revision": 1},
            "requirement": requirement,
            "routing": {
                "profile_revision": 1,
                "rules_version": 1,
                "text_default_tier": "standard",
                "authority_binding": None,
            },
            "registry": {"registry_revision": 0},
            "runtime": {
                "runtime_revision": 0,
                "mode": "inactive",
                "runtime_activation": False,
                "activation_fingerprint": None,
            },
            "activation": {"activation_fingerprint": None, "binding_drift": False},
            "tiers": [
                {
                    "tier": "fast",
                    "route": None,
                    "capabilities": [],
                    "execution_location": None,
                    "eligible": False,
                    "exclusion_reasons": ["tier_not_applicable", "tier_unconfigured"],
                },
                {
                    "tier": "standard",
                    "route": None,
                    "capabilities": [],
                    "execution_location": None,
                    "eligible": False,
                    "exclusion_reasons": ["tier_not_applicable", "tier_unconfigured"],
                },
                {
                    "tier": "deep",
                    "route": None,
                    "capabilities": [],
                    "execution_location": None,
                    "eligible": False,
                    "exclusion_reasons": ["tier_not_applicable", "tier_unconfigured"],
                },
                {
                    "tier": "vision",
                    "route": route,
                    "capabilities": ["vision"],
                    "execution_location": "remote",
                    "eligible": True,
                    "exclusion_reasons": [],
                },
                {
                    "tier": "image_generation",
                    "route": None,
                    "capabilities": [],
                    "execution_location": None,
                    "eligible": False,
                    "exclusion_reasons": ["image_generation_unavailable", "tier_unconfigured"],
                },
            ],
            "selected": selected,
            "catalog_revision": "c" * 64,
            "prompt_cache_scope": {
                "identity": hashlib.sha256(
                    json.dumps(prompt_scope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "project_id": project_id,
                "profile_id": "test-project-profile",
                "profile_revision": 1,
                "boundary_profile_id": "test-boundary-profile",
                "boundary_profile_revision": 1,
                "skill_snapshot_revision": None,
                "protocol_version": "1.0.0",
            },
        }
    )


def _request(*, allow_remote=True):
    return {
        "schema_version": "1.0.0",
        "turn_id": "turn-vision-001",
        "session_id": "session-vision-001",
        "operation_id": "vision-request-001",
        "idempotency_key": "vision-turn-001",
        "scope": {"kind": "project", "project_id": "default", "series_id": None},
        "input": {
            "kind": "text",
            "text": "请描述屏幕",
            "refs": [
                {
                    "kind": "companion_vision_grant",
                    "object_id": "vision-grant-test",
                    "uri": "crp://default/companion/vision/grants/vision-grant-test",
                }
            ],
        },
        "desired_outcome": COMPANION_VISION_OUTCOME,
        "privacy": {
            "mode": "remote_allowed" if allow_remote else "local_only",
            "allow_remote": allow_remote,
            "pii": "possible",
            "consent_refs": ["crp://default/consent/provider-egress-policy"] if allow_remote else [],
            "retention": "local_durable",
        },
        "capability_policy": {
            "allowed": [COMPANION_VISION_CONTEXT_CAPABILITY, COMPANION_VISION_ANALYZE_CAPABILITY],
            "denied": [],
            "require_approval": [COMPANION_VISION_ANALYZE_CAPABILITY],
        },
        "context_policy": {
            "include_project_skill": False,
            "include_memory": False,
            "include_session_history": False,
            "max_context_bytes": 4096,
        },
        "approval_policy": {"mode": "explicit", "auto_approve_read_only": True},
        "created_at": "2026-08-23T00:00:00+00:00",
    }


def _approve(runtime, waiting):
    approval = next(
        event for event in reversed(list(runtime.events_after(waiting.turn_id))) if event["type"] == "approval.required"
    )
    return runtime.apply_action(
        {
            "schema_version": "1.0.0",
            "action_id": "action-vision-001",
            "turn_id": waiting.turn_id,
            "type": "approve",
            "target_event_id": approval["event_id"],
            "reason": "analyze screen",
            "actor": "user",
            "expected_sequence": approval["sequence"],
            "idempotency_key": "approve-vision-001",
            "created_at": "2026-08-23T00:00:01+00:00",
        }
    )


def _assert_no_pixels_or_sensitive(value):
    if isinstance(value, dict):
        assert not ({"sha256", "path", "pixels", "bytes", "base64"} & set(value))
        for child in value.values():
            _assert_no_pixels_or_sensitive(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_pixels_or_sensitive(child)


def test_vision_turn_does_not_consume_or_call_gateway_before_approval():
    gateway = Gateway()
    runtime, grants, _payloads = _runtime(gateway=gateway)
    waiting = runtime.submit_turn(_request())
    assert waiting.status == "waiting_approval" and grants.consume_calls == 0 and gateway.calls == []
    events = list(runtime.events_after(waiting.turn_id))
    context_ref = next(
        event["data"]["payload_ref"]
        for event in events
        if event["type"] == "tool.completed" and event["data"]["capability_id"] == COMPANION_VISION_CONTEXT_CAPABILITY
    )
    _assert_no_pixels_or_sensitive(_payloads.get(context_ref))


def test_vision_turn_approved_once_consumes_and_persists_redacted_receipt():
    gateway = Gateway()
    runtime, grants, payloads = _runtime(gateway=gateway)
    completed = _approve(runtime, runtime.submit_turn(_request()))
    assert completed.status == "completed" and grants.consume_calls == 1 and len(gateway.calls) == 1
    assert gateway.calls[0].execution_control is not None
    assert gateway.calls[0].parameters["_model_routing_snapshot"]["requirement"]["required_capability"] == "vision"
    result = runtime.presentation_for(completed.turn_id)
    assert result == {
        "status": "completed",
        "request_id": "vision-request-001",
        "text": "画面中有一只杯子。",
        "provider_id": "fixture",
        "model_name": "vision-1",
        "provider_call_performed": True,
        "replayed": False,
    }
    assert "prompt_cache_scope_identity" not in repr(result)
    assert "model_routing_snapshot_revision" not in repr(result)
    assert "model_routing_catalog_revision" not in repr(result)
    receipt_ref = next(
        event["data"]["receipt_ref"]
        for event in runtime.events_after(completed.turn_id)
        if event["type"] == "tool.completed" and event["data"]["capability_id"] == COMPANION_VISION_ANALYZE_CAPABILITY
    )
    _assert_no_pixels_or_sensitive(payloads.get(receipt_ref))
    persisted = payloads.get(receipt_ref)
    assert "prompt_cache_scope_identity" not in repr(persisted)
    assert "model_routing_snapshot_revision" not in repr(persisted)
    assert "model_routing_catalog_revision" not in repr(persisted)
    events = list(runtime.events_after(completed.turn_id))
    analyze_started = next(
        event for event in events
        if event["type"] == "tool.started"
        and event["data"]["capability_id"] == COMPANION_VISION_ANALYZE_CAPABILITY
    )
    nested = [
        event for event in events
        if event["type"].startswith("model.")
        and event["correlation"]["tool_call_id"] == analyze_started["correlation"]["tool_call_id"]
    ]
    assert [event["type"] for event in nested] == [
        "model.requested",
        "model.routed",
        "model.completed",
    ]
    assert all(
        event["correlation"]["step_id"] == analyze_started["correlation"]["step_id"]
        for event in nested
    )
    model_terminal = nested[-1]
    model_receipt = validate_model_call_receipt(
        payloads.get(model_terminal["data"]["receipt_ref"])
    )
    assert model_receipt["provider_id"] == "fixture"
    assert model_receipt["model_id"] == "vision-1"
    assert model_receipt["input_recorded"] is False
    assert model_receipt["output_recorded"] is False
    cache_receipts = []
    for ref in model_terminal["data"]["evidence_refs"]:
        candidate = payloads.get(ref)
        if isinstance(candidate, dict) and "cache_status" in candidate:
            cache_receipts.append(validate_prompt_cache_receipt(candidate))
    assert len(cache_receipts) == 1
    assert cache_receipts[0]["cache_read_input_tokens"] == 7
    assert cache_receipts[0]["uncached_input_tokens"] == 3
    _assert_no_pixels_or_sensitive(model_receipt)
    _assert_no_pixels_or_sensitive(cache_receipts[0])
    developer = runtime.execution_projection_for(completed.turn_id, "developer")
    nested_step = next(
        item for item in developer["model_steps"]
        if item["model_request_id"] == model_terminal["correlation"]["model_request_id"]
    )
    assert nested_step["parent_tool_call_id"] == analyze_started["correlation"]["tool_call_id"]
    assert nested_step["routing_status"] == "recorded"
    assert nested_step["prompt_cache"] == {
        "status": "reported",
        "source": "provider_usage",
        "cache_read_input_tokens": 7,
        "cache_write_input_tokens": None,
        "uncached_input_tokens": 3,
    }


def test_vision_approval_action_replays_without_second_consume_or_gateway_call():
    gateway = Gateway()
    runtime, grants, _ = _runtime(gateway=gateway)
    waiting = runtime.submit_turn(_request())
    first = _approve(runtime, waiting)
    replay = _approve(runtime, waiting)
    assert first.status == replay.status == "completed" and replay.replayed is True
    assert grants.consume_calls == 1 and len(gateway.calls) == 1


def test_vision_route_event_failure_prevents_provider_egress_and_retry() -> None:
    gateway = Gateway()
    runtime, grants, payloads = _runtime(
        gateway=gateway,
        events=RejectModelRouteEventStore(),
    )

    failed = _approve(runtime, runtime.submit_turn(_request()))

    assert failed.status == "failed"
    assert grants.consume_calls == 1
    assert gateway.calls == []
    event_types = [event["type"] for event in runtime.events_after(failed.turn_id)]
    assert "model.requested" in event_types
    assert "model.routed" not in event_types
    assert "model.failed" in event_types
    assert event_types.count("tool.started") == 2


def test_vision_nested_request_event_failure_prevents_gateway_call() -> None:
    gateway = Gateway()
    runtime, grants, _ = _runtime(
        gateway=gateway,
        events=RejectNestedModelRequestEventStore(),
    )

    failed = _approve(runtime, runtime.submit_turn(_request()))

    assert failed.status == "failed"
    assert grants.consume_calls == 1
    assert gateway.calls == []
    assert not any(
        event["type"] == "model.requested"
        and event["correlation"]["tool_call_id"] is not None
        for event in runtime.events_after(failed.turn_id)
    )


def test_vision_model_receipt_failure_rejects_remote_success_artifact() -> None:
    gateway = Gateway()
    runtime, grants, payloads = _runtime(
        gateway=gateway,
        payloads=RejectModelReceiptPayloadStore(),
    )

    failed = _approve(runtime, runtime.submit_turn(_request()))

    assert failed.status == "failed"
    assert grants.consume_calls == 1
    assert len(gateway.calls) == 1
    assert runtime.presentation_for(failed.turn_id) is None
    events = list(runtime.events_after(failed.turn_id))
    nested_failed = next(
        event for event in events
        if event["type"] == "model.failed"
        and event["correlation"]["tool_call_id"] is not None
    )
    assert nested_failed["data"]["error_code"] == "ai.model_receipt_failed"
    analyze_outcome = next(
        event for event in events
        if event["type"] == "tool.outcome.recorded"
        and event["data"]["capability_id"] == COMPANION_VISION_ANALYZE_CAPABILITY
    )
    persisted_outcome = payloads.get(analyze_outcome["data"]["payload_ref"])
    assert persisted_outcome["status"] == "unknown_effect"
    assert persisted_outcome["error_code"] == "ai.tool_outcome_unknown"


def test_vision_approval_replay_converges_durable_outcome_without_second_egress() -> None:
    gateway = Gateway()
    runtime, grants, _ = _runtime(
        gateway=gateway,
        events=CrashAfterToolOutcomeEventStore(),
    )
    waiting = runtime.submit_turn(_request())

    with pytest.raises(SystemExit, match="after durable tool outcome"):
        _approve(runtime, waiting)

    completed = _approve(runtime, waiting)

    assert completed.status == "completed"
    assert grants.consume_calls == 1
    assert len(gateway.calls) == 1
    events = list(runtime.events_after(completed.turn_id))
    analyze_call = next(
        event["correlation"]["tool_call_id"]
        for event in events
        if event["type"] == "tool.outcome.recorded"
        and event["data"]["capability_id"] == COMPANION_VISION_ANALYZE_CAPABILITY
    )
    assert sum(
        event["type"] == "tool.completed"
        and event["correlation"]["tool_call_id"] == analyze_call
        for event in events
    ) == 1
    assert sum(
        event["type"] == "model.completed"
        and event["correlation"]["tool_call_id"] == analyze_call
        for event in events
    ) == 1


def test_vision_baseline_drift_stops_before_consuming_grant():
    grants = GrantStore()
    revision = {"value": 1}
    payloads = InMemoryTurnPayloadStore()
    registry = ScopedCapabilityRegistry()
    def loader():
        return "你是视觉助手", revision["value"], 1
    registry.register(
        CapabilityDefinition(
            COMPANION_VISION_CONTEXT_CAPABILITY, 1, "read", False, "read_only", "crp://in", "crp://out"
        ),
        CompanionVisionContextCapability(grant_store=grants, prompt_profile_loader=loader),
    )
    registry.register(
        CapabilityDefinition(
            COMPANION_VISION_ANALYZE_CAPABILITY, 1, "write", True, "receipt_required", "crp://in", "crp://out"
        ),
        CompanionVisionAnalyzeCapability(
            grant_store=grants, prompt_profile_loader=loader, gateway=Gateway(), receipt_store=payloads
        ),
    )
    runtime = SynchronousAIRuntime(
        planner=CompanionVisionTurnPlanner(),
        registry=registry,
        events=InMemoryTurnEventStore(),
        payloads=payloads,
        state=InMemoryTurnStateStore(),
    )
    waiting = runtime.submit_turn(_request())
    revision["value"] = 2
    failed = _approve(runtime, waiting)
    assert failed.status == "failed" and grants.consume_calls == 0
    assert list(runtime.events_after(failed.turn_id))[-1]["data"]["error_code"] == "ai.stale_baseline"


def test_vision_turn_uses_local_fallback_when_remote_is_not_allowed():
    gateway = Gateway()
    runtime, grants, _ = _runtime(gateway=gateway)
    completed = _approve(runtime, runtime.submit_turn(_request(allow_remote=False)))
    assert completed.status == "completed" and grants.consume_calls == 1 and gateway.calls == []
    assert runtime.presentation_for(completed.turn_id)["provider_id"] == "local-fallback"


def test_vision_never_egresses_when_routing_snapshot_is_missing_or_tampered():
    for snapshot in ("missing", "tampered"):
        gateway = Gateway()
        runtime, grants, _ = _runtime(gateway=gateway, routing_snapshot=snapshot)

        completed = _approve(runtime, runtime.submit_turn(_request()))

        assert completed.status == "completed"
        assert grants.consume_calls == 1
        assert gateway.calls == []
        assert runtime.presentation_for(completed.turn_id)["provider_id"] == "local-fallback"
