from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.ai_kernel import (
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    ScopedCapabilityRegistry,
    ScopedTurnPayloadError,
    ScopedTurnPayloadView,
    SynchronousAIRuntime,
)


ROOT = Path(__file__).resolve().parents[2]


class _ReadRefPlanner:
    def __init__(self, payload_ref: str) -> None:
        self.payload_ref = payload_ref

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        payloads.get(self.payload_ref)
        return {"type": "complete", "summary": "must not complete"}


class _CrossTurnWritePlanner:
    def plan(self, request, events, capabilities, payloads, execution_control=None):
        payloads.put("turn-other", "planner", {"forbidden": True})
        return {"type": "complete", "summary": "must not complete"}


def test_scoped_view_reads_only_explicit_current_turn_ref() -> None:
    store = InMemoryTurnPayloadStore()
    allowed = store.put("turn-a", "context", {"value": "allowed"})
    hidden = store.put("turn-a", "hidden", {"value": "hidden"})
    other = store.put("turn-b", "context", {"value": "other"})
    view = ScopedTurnPayloadView(store, turn_id="turn-a", allowed_refs=(allowed,))
    assert view.get(allowed) == {"value": "allowed"}
    for ref in (hidden, other, "crp://default/not-session/ref"):
        with pytest.raises(ScopedTurnPayloadError):
            view.get(ref)


def test_scoped_view_reads_immutable_payload_only_when_linked_and_same_turn() -> None:
    store = InMemoryTurnPayloadStore()
    allowed = store.get_or_create_immutable_payload("turn-a", "routing", {"value": "allowed"})
    store.get_or_create_immutable_payload("turn-a", "hidden", {"value": "hidden"})
    other = store.get_or_create_immutable_payload("turn-b", "routing", {"value": "other"})
    view = ScopedTurnPayloadView(store, turn_id="turn-a", allowed_refs=(allowed,))

    assert view.get_immutable_payload("turn-a", "routing") == (allowed, {"value": "allowed"})
    with pytest.raises(ScopedTurnPayloadError, match="outside Planner context manifest"):
        view.get_immutable_payload("turn-a", "hidden")
    with pytest.raises(ScopedTurnPayloadError, match="crossed Turn identity"):
        view.get_immutable_payload("turn-b", "routing")
    assert other != allowed


def test_scoped_view_allows_governed_same_turn_write_and_rejects_cross_turn_write() -> None:
    store = InMemoryTurnPayloadStore()
    view = ScopedTurnPayloadView(store, turn_id="turn-a", allowed_refs=())
    ref = view.put("turn-a", "presentation", {"status": "ready"})
    assert store.get(ref) == {"status": "ready"}
    with pytest.raises(ScopedTurnPayloadError, match="crossed Turn identity"):
        view.put("turn-b", "planner", {"forbidden": True})


@pytest.mark.parametrize("cross_turn", [False, True])
def test_runtime_planner_cannot_read_unlinked_or_cross_turn_payload(cross_turn: bool) -> None:
    payloads = InMemoryTurnPayloadStore()
    request = _request()
    target_turn = "turn-other" if cross_turn else str(request["turn_id"])
    hidden = payloads.put(target_turn, "hidden", {"private": "not selected"})
    runtime = SynchronousAIRuntime(
        planner=_ReadRefPlanner(hidden),
        registry=ScopedCapabilityRegistry(),
        events=InMemoryTurnEventStore(),
        payloads=payloads,
    )
    receipt = runtime.submit_turn(request)
    assert receipt.status == "failed"
    assert tuple(runtime.events_after(receipt.turn_id))[-1]["type"] == "turn.failed"


def test_runtime_planner_cannot_write_cross_turn_payload() -> None:
    runtime = SynchronousAIRuntime(
        planner=_CrossTurnWritePlanner(),
        registry=ScopedCapabilityRegistry(),
        events=InMemoryTurnEventStore(),
        payloads=InMemoryTurnPayloadStore(),
    )
    receipt = runtime.submit_turn(_request())
    assert receipt.status == "failed"


def _request() -> dict[str, object]:
    return json.loads(
        (ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(
            encoding="utf-8"
        )
    )


class _RoleGateway:
    def __init__(self):
        self.requests = []

    def invoke(self, request):
        from core.model_gateway import ModelResult
        self.requests.append(request)
        return ModelResult({"type": "complete", "summary": "done", "evidence_refs": []}, "test", "model", {})


def test_runtime_model_receives_frozen_role_through_scoped_view():
    from core.ai_kernel import ModelGatewayAgentPlanner
    request = _request()
    payloads = InMemoryTurnPayloadStore()
    gateway = _RoleGateway()
    runtime = SynchronousAIRuntime(planner=ModelGatewayAgentPlanner(gateway), registry=ScopedCapabilityRegistry(),
                                   events=InMemoryTurnEventStore(), payloads=payloads)
    runtime.accept_turn(request)
    brief = {"schema_version": "1.0.0", "kind": "agent.role-brief.v1", "profile_id": "subagent.explorer",
             "profile_revision": 1, "organization_role": "explorer", "work_description": "Read evidence",
             "instructions": "Return evidence and identify missing facts."}
    payloads.get_or_create_immutable_payload(request["turn_id"], "agent-role-brief-v1", brief)
    receipt = runtime.run_accepted_turn(request["turn_id"])
    assert receipt.status == "completed"
    assert len(gateway.requests) == 1
    role = json.loads(gateway.requests[0].input)["role"]
    assert role["instructions"] == brief["instructions"]
    assert role["organization_role"] == brief["organization_role"]


def test_planner_role_allowlist_rejects_cross_turn_authority():
    from core.ai_kernel.scoped_payloads import planner_context_payload_refs
    class CrossTurnStore(InMemoryTurnPayloadStore):
        def get_immutable_payload(self, turn_id, kind):
            return ("crp://session/turn-other/agent-role-brief-v1", {})
    with pytest.raises(ScopedTurnPayloadError):
        planner_context_payload_refs([], CrossTurnStore(), turn_id="turn-a")


def test_planner_role_allowlist_does_not_expose_other_private_kinds():
    from core.ai_kernel.scoped_payloads import planner_context_payload_refs
    store = InMemoryTurnPayloadStore()
    role = store.get_or_create_immutable_payload("turn-a", "agent-role-brief-v1", {})
    store.get_or_create_immutable_payload("turn-a", "private-v1", {"private": True})
    refs = planner_context_payload_refs([], store, turn_id="turn-a")
    assert refs == (role,)
    view = ScopedTurnPayloadView(store, turn_id="turn-a", allowed_refs=refs)
    with pytest.raises(ScopedTurnPayloadError):
        view.get_immutable_payload("turn-a", "private-v1")
