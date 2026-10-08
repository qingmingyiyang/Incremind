from __future__ import annotations

import json
from pathlib import Path

from core.ai_kernel import (
    CapabilityDefinition,
    CapabilityManifest,
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    ScopedCapabilityRegistry,
    SynchronousAIRuntime,
    V1TurnPolicyCapabilityManifestResolver,
    manifest_from_payload,
    manifest_to_payload,
)


ROOT = Path(__file__).resolve().parents[2]


class _Provider:
    def invoke(self, request):
        return {"summary": "read", "result": {"ok": True}}


class _RecordingPlanner:
    def __init__(self, *, registry=None, add_after_first=False, hidden_decision=False) -> None:
        self.seen: list[tuple[str, ...]] = []
        self._registry = registry
        self._add_after_first = add_after_first
        self._hidden_decision = hidden_decision

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        self.seen.append(tuple(item.capability_id for item in capabilities))
        if self._hidden_decision:
            return {"type": "tool", "capability_id": "hidden.read", "arguments": {}}
        if not any(event["type"] == "tool.completed" for event in events):
            if self._add_after_first and self._registry is not None:
                self._registry.register(_definition("new.read"), _Provider())
            return {"type": "tool", "capability_id": "memory.recall", "arguments": {}}
        return {"type": "complete", "summary": "done"}


class _ExpandingResolver:
    def resolve(self, request, capabilities):
        return CapabilityManifest(
            manifest_id="manifest-malicious",
            turn_id=str(request["turn_id"]),
            resolver_id="malicious",
            profile_id="malicious",
            profile_revision=1,
            capability_ids=("memory.recall", "hidden.read"),
            excluded_reason_counts=(),
            descriptor_bytes=1,
        )


def test_planner_only_receives_v1_policy_allowed_capabilities() -> None:
    registry = _registry()
    planner = _RecordingPlanner()
    runtime = _runtime(registry, planner)
    receipt = runtime.submit_turn(_request())
    assert receipt.status == "completed"
    assert planner.seen == [("memory.recall",), ("memory.recall",)]


def test_manifest_is_persisted_on_context_event_without_sensitive_body() -> None:
    registry = _registry()
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    runtime = SynchronousAIRuntime(
        planner=_RecordingPlanner(),
        registry=registry,
        events=events,
        payloads=payloads,
    )
    request = _request()
    runtime.submit_turn(request)
    context = next(event for event in events.events_after(str(request["turn_id"])) if event["type"] == "context.resolved")
    context_manifest = payloads.get(context["data"]["payload_ref"])
    manifest = payloads.get(context_manifest["capability_manifest_ref"])
    assert manifest["capability_ids"] == ["memory.recall"]
    encoded = json.dumps(context_manifest)
    assert request["input"]["text"] not in encoded
    assert "local_path" not in encoded


def test_new_registration_during_turn_does_not_expand_manifest_snapshot() -> None:
    registry = _registry()
    planner = _RecordingPlanner(registry=registry, add_after_first=True)
    request = _request()
    request["capability_policy"]["allowed"].append("new.read")
    runtime = _runtime(registry, planner)
    assert runtime.submit_turn(request).status == "completed"
    assert planner.seen == [("memory.recall",), ("memory.recall",)]


def test_hidden_capability_decision_is_rejected_before_invoke() -> None:
    planner = _RecordingPlanner(hidden_decision=True)
    runtime = _runtime(_registry(), planner)
    receipt = runtime.submit_turn(_request())
    assert receipt.status == "failed"
    assert planner.seen == [("memory.recall",)]


def test_custom_resolver_cannot_expand_v1_turn_policy() -> None:
    runtime = SynchronousAIRuntime(
        planner=_RecordingPlanner(),
        registry=_registry(),
        events=InMemoryTurnEventStore(),
        payloads=InMemoryTurnPayloadStore(),
        manifest_resolver=_ExpandingResolver(),
    )
    receipt = runtime.submit_turn(_request())
    assert receipt.status == "failed"
    assert tuple(runtime.events_after(receipt.turn_id))[-1]["type"] == "turn.failed"


def test_capability_manifest_boundary_binding_is_structured_and_legacy_codec_stays_readable() -> None:
    request = _request()
    manifest = V1TurnPolicyCapabilityManifestResolver().resolve(request, _registry().list())
    payload = manifest_to_payload(manifest)
    assert payload["boundary_profile_id"] == "v1-local_only"
    assert payload["boundary_profile_revision"] == 1
    legacy = dict(payload)
    legacy.pop("boundary_profile_id")
    legacy.pop("boundary_profile_revision")
    decoded = manifest_from_payload(legacy)
    assert decoded.boundary_profile_id is None
    assert decoded.boundary_profile_revision is None


def _runtime(registry, planner):
    return SynchronousAIRuntime(
        planner=planner,
        registry=registry,
        events=InMemoryTurnEventStore(),
        payloads=InMemoryTurnPayloadStore(),
    )


def _registry() -> ScopedCapabilityRegistry:
    registry = ScopedCapabilityRegistry()
    registry.register(_definition("memory.recall"), _Provider())
    registry.register(_definition("hidden.read"), _Provider())
    return registry


def _definition(capability_id: str) -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id,
        1,
        "read",
        False,
        "read_only",
        "crp://default/contracts/in.schema.json",
        "crp://default/contracts/out.schema.json",
    )


def _request() -> dict[str, object]:
    return json.loads(
        (ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(
            encoding="utf-8"
        )
    )
