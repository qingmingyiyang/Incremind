from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.api import context_graph_replay_evidence as evidence
from core.context_graph import FrozenContextRevisions, ReplayRequest


TURN_ID = "turn-replay-evidence"
OPERATION_ID = "operation-replay-evidence"
BINDING_REF = "crp://context-bindings/project-a/binding-a"
REVISIONS = FrozenContextRevisions("cap-1", "7", "provider-1", "3", "2.0.0")
ROUTING_REVISION = "a" * 64
TERMINAL_REF = "event-turn-completed"
ATTEMPT_REF = f"crp://session/{TURN_ID}/model-wire-attempt-receipt/one"


@dataclass
class _Binding:
    graph_id: str = "graph-a"
    graph_revision: str = "r1"
    capability_revision: str = "cap-1"
    boundary_revision: str = "7"
    provider_revision: str = "provider-1"
    model_route_revision: str = "3"
    compiler_revision: str = "2.0.0"


class _Store:
    def __init__(self, request, events, payloads, binding_payload):
        self.request, self.events, self.payloads, self.binding_payload = request, events, payloads, binding_payload

    def get_request(self, turn_id):
        return self.request if turn_id == TURN_ID else None

    def events_after(self, turn_id, after_sequence=0):
        assert turn_id == TURN_ID
        return self.events

    def get(self, ref):
        return self.payloads[ref]

    def get_immutable_payload(self, turn_id, kind):
        assert turn_id == TURN_ID and kind == "context-binding-v1"
        return (f"crp://session/{TURN_ID}/context-binding-v1/one", self.binding_payload)


def _request():
    return {
        "turn_id": TURN_ID,
        "operation_id": OPERATION_ID,
        "scope": {"kind": "project", "project_id": "project-a"},
        "input": {"refs": [{"kind": "context_binding", "object_id": "binding-a", "uri": BINDING_REF}]},
        "desired_outcome": "context.evaluate",
    }


def _replay_request():
    return ReplayRequest(
        "replay-request-a", "replay-plan-a", "project-a", "graph-a", "r1", "r2",
        "node-a", 0, "fingerprint-a", BINDING_REF, TURN_ID, OPERATION_ID,
        (), REVISIONS,
    )


def _events(*, terminal=True, duplicate_model=False):
    model = {
        "event_id": "event-model-completed", "turn_id": TURN_ID, "type": "model.completed",
        "correlation": {"step_id": "step-a", "model_request_id": "model-request-a"},
        "data": {"receipt_ref": f"crp://session/{TURN_ID}/model-receipt/one", "evidence_refs": [
            f"crp://session/{TURN_ID}/model-routing/one", ATTEMPT_REF,
        ]},
    }
    output = [model] + ([{**model, "event_id": "event-model-completed-two"}] if duplicate_model else [])
    if terminal:
        output.append({
            "event_id": TERMINAL_REF, "turn_id": TURN_ID, "type": "turn.completed",
            "correlation": {"step_id": "step-a", "model_request_id": "model-request-a"},
            "data": {"summary": "trusted regenerated answer"},
        })
    return tuple(output)


def _payloads(*, provider_revision="provider-1"):
    routing_ref = f"crp://session/{TURN_ID}/model-routing/one"
    receipt_ref = f"crp://session/{TURN_ID}/model-receipt/one"
    routing = {
        "project": {"project_id": "project-a"},
        "selected": {"provider_id": "provider-a", "provider_revision": provider_revision, "model_name": "model-a", "route_revision": 3},
        "boundary": {"profile_revision": 7},
        "requirement": {"required_capability": "structured", "output_contract": "json_object", "input_refs": [{"kind": "context_binding", "object_id": "binding-a"}]},
        "turn": {"turn_id": TURN_ID},
    }
    receipt = {"turn_id": TURN_ID, "model_request_id": "model-request-a", "status": "completed", "usage_status": "recorded", "provider_id": "provider-a", "model_id": "model-a", "completed_at": "2026-08-30T10:00:01Z"}
    attempt = {"turn_id": TURN_ID, "model_request_id": "model-request-a", "status": "succeeded", "routing_snapshot_revision": ROUTING_REVISION, "provider_id": "provider-a", "model_id": "model-a"}
    return {routing_ref: routing, receipt_ref: receipt, ATTEMPT_REF: attempt}


def _store(**kwargs):
    return _Store(_request(), _events(), _payloads(**kwargs), {
        "schema_version": "1.0.0", "project_id": "project-a", "binding_id": "binding-a",
        "capability_revision": "cap-1", "binding": {"ignored": True},
    })


@pytest.fixture(autouse=True)
def _lightweight_contracts(monkeypatch):
    monkeypatch.setattr(evidence, "validate_turn_request", lambda value: dict(value))
    monkeypatch.setattr(evidence, "validate_model_call_receipt", lambda value: dict(value))
    monkeypatch.setattr(evidence, "validate_model_wire_attempt_receipt", lambda value: dict(value))
    def _routing(value):
        if not isinstance(value, dict) or "selected" not in value:
            raise ValueError("not routing")
        return dict(value)

    monkeypatch.setattr(evidence, "validate_turn_model_routing_snapshot", _routing)
    monkeypatch.setattr(evidence, "turn_model_routing_snapshot_revision", lambda value: ROUTING_REVISION)
    monkeypatch.setattr(evidence, "context_binding_from_payload", lambda value: _Binding())


def test_success_issues_private_trusted_completion_from_terminal_summary():
    result = evidence.verified_replay_completion_from_turn(
        store=_store(), replay_request=_replay_request(), expected_operation_id=OPERATION_ID,
        expected_turn_request=_request(),
    )

    assert result.output_text == "trusted regenerated answer"
    assert result.completion.output_ref == f"turn-terminal-summary:{TERMINAL_REF}"
    assert result.completion.terminal_evidence.terminal_event_ref == TERMINAL_REF
    assert result.completion.terminal_evidence.turn_operation_id == OPERATION_ID
    assert result.completion.model_wire_evidence.model_wire_receipt_ref == ATTEMPT_REF


def test_rejects_forged_client_binding_reference():
    request = _replay_request()
    forged = ReplayRequest(
        request.replay_request_id, request.replay_plan_id, request.project_id, request.graph_id,
        request.source_graph_revision, request.result_graph_revision, request.node_id, request.plan_index,
        request.input_fingerprint, "crp://context-bindings/project-a/forged", request.turn_id,
        request.turn_operation_id, request.predecessor_receipt_refs,
        request.revisions,
    )
    with pytest.raises(evidence.ContextGraphReplayEvidenceError, match="ContextBinding identity drifted"):
        evidence.verified_replay_completion_from_turn(
            store=_store(), replay_request=forged, expected_operation_id=OPERATION_ID,
            expected_turn_request=_request(),
        )


@pytest.mark.parametrize("events", [_events(terminal=False), _events(duplicate_model=True)])
def test_rejects_failed_or_ambiguous_turn(events):
    bad = _store()
    bad.events = events
    with pytest.raises(evidence.ContextGraphReplayEvidenceError, match="completion is absent or ambiguous"):
        evidence.verified_replay_completion_from_turn(
            store=bad, replay_request=_replay_request(), expected_operation_id=OPERATION_ID,
            expected_turn_request=_request(),
        )


def test_rejects_revision_drift():
    with pytest.raises(evidence.ContextGraphReplayEvidenceError, match="routing evidence drifted"):
        evidence.verified_replay_completion_from_turn(
            store=_store(provider_revision="provider-drift"), replay_request=_replay_request(),
            expected_operation_id=OPERATION_ID, expected_turn_request=_request(),
        )


def test_rejects_binding_revision_drift(monkeypatch):
    monkeypatch.setattr(evidence, "context_binding_from_payload", lambda value: _Binding(compiler_revision="other"))
    with pytest.raises(evidence.ContextGraphReplayEvidenceError, match="ContextBinding revisions drifted"):
        evidence.verified_replay_completion_from_turn(
            store=_store(), replay_request=_replay_request(), expected_operation_id=OPERATION_ID,
            expected_turn_request=_request(),
        )


def test_rejects_any_drift_from_the_server_frozen_turn_envelope():
    store = _store()
    store.request = {**_request(), "input": {
        "kind": "text", "text": "client replaced replay instruction",
        "refs": _request()["input"]["refs"],
    }}

    with pytest.raises(evidence.ContextGraphReplayEvidenceError, match="request drifted"):
        evidence.verified_replay_completion_from_turn(
            store=store, replay_request=_replay_request(),
            expected_operation_id=OPERATION_ID, expected_turn_request=_request(),
        )
