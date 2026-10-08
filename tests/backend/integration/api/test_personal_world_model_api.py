from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import personal_world_model as personal_world_model_routes
from backend.api.routes.personal_world_model import router
from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.api.task_reference_projection import task_ref_for_world_action
from backend.api.project_provenance_runtime import ProjectProvenanceRuntime
from core.ai_kernel.tool_invocation import ToolInvocationOutcome, outcome_to_payload
from core.long_horizon_runtime import TraceSubject, VersionBinding


PROJECT = "project-api"
ACTION = "operation-api"
TURN = "turn-api"
OUTCOME_REF = "crp://session/turn-api/tool-invocation-outcome/outcome-api"
NOW = "2026-09-01T11:00:00Z"


@dataclass(frozen=True)
class _Receipt:
    turn_id: str = TURN
    operation_id: str = ACTION
    status: str = "completed"
    current_sequence: int = 4


class _TurnAuthority:
    def __init__(self) -> None:
        self.outcome = outcome_to_payload(ToolInvocationOutcome(
            invocation_id="tool-call-api",
            turn_id=TURN,
            capability_id="project.write",
            attempt=1,
            status="completed",
            effect_certainty="confirmed_applied",
            payload_ref=None,
            receipt_ref="crp://receipts/project-api-write",
            evidence_refs=(),
            error_code=None,
            retryable=False,
        ))

    def get_request(self, turn_id: str):
        return (
            {"scope": {"kind": "project", "project_id": PROJECT}}
            if turn_id == TURN
            else None
        )

    def receipt_for(self, turn_id: str, *, replayed: bool = False):
        if turn_id != TURN:
            raise KeyError(turn_id)
        return _Receipt()

    def events_after(self, turn_id: str, after_sequence: int = 0):
        events = (
            {"sequence": 1, "type": "turn.accepted", "data": {}, "correlation": {}, "occurred_at": "2026-09-01T10:59:55Z"},
            {"sequence": 2, "type": "tool.outcome.recorded", "data": {"payload_ref": OUTCOME_REF, "capability_id": "project.write"}, "correlation": {"tool_call_id": "tool-call-api"}, "occurred_at": "2026-09-01T10:59:57Z"},
            {"sequence": 3, "type": "tool.completed", "data": {"capability_id": "project.write"}, "correlation": {"tool_call_id": "tool-call-api"}, "occurred_at": "2026-09-01T10:59:58Z"},
            {"sequence": 4, "type": "turn.completed", "data": {"status": "completed"}, "correlation": {"operation_id": ACTION}, "occurred_at": "2026-09-01T10:59:59Z"},
        )
        return tuple(item for item in events if int(item["sequence"]) > after_sequence)

    def get(self, payload_ref: str):
        if payload_ref != OUTCOME_REF:
            raise KeyError(payload_ref)
        return dict(self.outcome)


def _client(root: Path, authority: _TurnAuthority) -> TestClient:
    application = FastAPI()
    application.state.container = SimpleNamespace(root_dir=root)
    application.state.ai_runtime = authority
    application.state.ai_turn_effect_store = authority
    application.include_router(router)
    return TestClient(application)


def _event(kind: str, event_id: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "event_id": event_id,
        "kind": kind,
        "actor": "user",
        "source_ref": f"crp://api-events/{event_id}",
        "source_revision": "1",
        "occurred_at": NOW,
        "recorded_at": NOW,
        "payload": payload,
    }


def _seed_plan(client: TestClient) -> None:
    goal = client.post(f"/api/rebuild/projects/{PROJECT}/world-model/events", json=_event(
        "goal.declared",
        "goal-api-event",
        {
            "goal_id": "goal-api",
            "title": "Complete one API-governed project change",
            "success_criteria": ["The next state contains verified feedback"],
            "target_at": None,
            "evidence_refs": ["crp://projects/project-api/goals/goal-api"],
        },
    ))
    assert goal.status_code == 201
    action = client.post(f"/api/rebuild/projects/{PROJECT}/world-model/events", json=_event(
        "action.planned",
        "action-api-event",
        {
            "action_id": ACTION,
            "title": "Write the governed project artifact",
            "expected_outcome": "The project artifact is updated",
            "effect_class": "QUERYABLE",
            "gate_requirement": "approval",
            "due_at": None,
            "evidence_refs": ["crp://plans/project-api/operation-api"],
        },
    ))
    assert action.status_code == 201


def _feedback() -> dict[str, object]:
    return {
        "event_id": "feedback-api-event",
        "feedback_id": "feedback-api",
        "supersedes_feedback_id": None,
        "action_id": ACTION,
        "turn_id": TURN,
        "outcome_ref": OUTCOME_REF,
        "expected_outcome": "The project artifact is updated",
        "actual_outcome": "The persisted artifact revision was observed",
        "outcome": "achieved",
        "state_delta": [{"field": "artifact.revision", "before": 1, "after": 2}],
        "cost": {
            "elapsed_ms": 900,
            "model_input_tokens": 20,
            "model_output_tokens": 8,
            "external_calls": 1,
            "human_attention_seconds": 3,
        },
        "user_evaluation": {"verdict": "accepted", "rating": 5, "note": "Observed"},
        "observed_evidence_refs": ["crp://artifacts/project-api/revision-2"],
        "actor": "user",
        "occurred_at": NOW,
        "recorded_at": NOW,
    }


def test_api_records_verified_feedback_and_rebuilds_state_after_restart(tmp_path: Path) -> None:
    authority = _TurnAuthority()
    with _client(tmp_path, authority) as client:
        _seed_plan(client)
        response = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/feedback",
            json=_feedback(),
        )
        assert response.status_code == 201
        assert response.json()["verified_outcome"]["effect_certainty"] == "confirmed_applied"
        assert response.json()["state"]["latest_feedback_id"] == "feedback-api"

    with _client(tmp_path, authority) as restarted:
        state = restarted.get(f"/api/rebuild/projects/{PROJECT}/world-model/state")
        assert state.status_code == 200
        assert state.json()["through_sequence"] == 3
        assert state.json()["persisted_as_authority"] is False
        replay = restarted.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/feedback",
            json=_feedback(),
        )
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True


def test_api_projects_safe_provenance_summary_without_internal_references(tmp_path: Path) -> None:
    provenance = ProjectProvenanceRuntime(
        world=PersonalWorldModelRuntime.for_root(tmp_path),
    )
    provenance.record_subject(
        subject=TraceSubject(PROJECT, "data", "dataset-api"),
        version=VersionBinding(
            "crp://sources/project-api/dataset-api",
            "revision-3",
            "dataset-fingerprint-3",
        ),
        recorded_at=NOW,
    )

    with _client(tmp_path, _TurnAuthority()) as client:
        response = client.get(
            f"/api/rebuild/projects/{PROJECT}/world-model/provenance",
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "kind": "project_provenance_summary.v1",
        "counts": {"subjects": 1, "links": 0, "validations": 0},
        "subject_kinds": [{"kind": "data", "count": 1}],
        "relations": [],
        "validation_statuses": [],
    }
    serialized = response.text.lower()
    assert "crp://" not in serialized
    assert "fingerprint" not in serialized
    assert "evidence" not in serialized


def test_api_rejects_unverified_feedback_and_sensitive_narrative(tmp_path: Path) -> None:
    authority = _TurnAuthority()
    with _client(tmp_path, authority) as client:
        _seed_plan(client)
        feedback = _feedback()
        feedback["outcome_ref"] = "crp://session/turn-api/tool-invocation-outcome/not-real"
        assert client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/feedback",
            json=feedback,
        ).status_code == 409

        unsafe = _event(
            "project.observation",
            "unsafe-observation",
            {
                "observation_id": "unsafe",
                "category": "environment",
                "summary": "api_key=secret-value-123456",
                "evidence_refs": ["crp://observations/unsafe"],
            },
        )
        assert client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/events",
            json=unsafe,
        ).status_code == 409


def test_generic_event_endpoint_cannot_bypass_feedback_evidence_gate(tmp_path: Path) -> None:
    with _client(tmp_path, _TurnAuthority()) as client:
        response = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/events",
            json=_event("feedback.recorded", "feedback-bypass", {}),
        )
    assert response.status_code == 409


def test_generic_event_endpoint_rejects_client_supervision_events(tmp_path: Path) -> None:
    with _client(tmp_path, _TurnAuthority()) as client:
        response = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/events",
            json=_event("supervision.claim.declared", "supervision-bypass", {}),
        )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "kind",
    (
        "provenance.subject.recorded",
        "provenance.link.recorded",
        "provenance.validation.recorded",
        "task_graph.event.recorded",
        "trajectory.checkpoint.recorded",
    ),
)
def test_generic_event_endpoint_rejects_client_provenance_facts(
    tmp_path: Path,
    kind: str,
) -> None:
    with _client(tmp_path, _TurnAuthority()) as client:
        response = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/events",
            json=_event(kind, f"provenance-bypass-{kind.split('.')[1]}", {}),
        )
    assert response.status_code == 409


def test_workflow_pivot_route_uses_only_the_explicit_managed_contract(
    tmp_path: Path, monkeypatch,
) -> None:
    calls: list[dict[str, object]] = []

    class _PivotWorkflow:
        def pivot_action(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                replayed=False,
                to_payload=lambda: {
                    "turn_id": "world-turn-pivot-api",
                    "action_id": "world-action-pivot-api",
                    "status": "accepted",
                    "replayed": False,
                    "state": {"pending_action_ids": ["world-action-pivot-api"]},
                },
            )

    monkeypatch.setattr(
        personal_world_model_routes,
        "_workflow",
        lambda request, container: _PivotWorkflow(),
    )
    payload = {
        "command_id": "pivot-api-12345678",
        "supersedes_action_id": "world-action-original-api",
        "title": "Test the corrected direction",
        "expected_outcome": "Current evidence supports the replacement",
        "question": "Proceed using the corrected direction?",
    }

    with _client(tmp_path, _TurnAuthority()) as client:
        response = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/workflow/actions/pivot",
            json=payload,
        )
        extra = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/workflow/actions/pivot",
            json={**payload, "claim_id": "client-controlled-claim"},
        )

    assert response.status_code == 202
    assert response.json()["action_id"] == "world-action-pivot-api"
    assert response.json()["task_ref"] == task_ref_for_world_action(
        project_id=PROJECT, action_id="world-action-pivot-api",
    )
    assert calls == [{"project_id": PROJECT, **payload}]
    assert extra.status_code == 400


@pytest.mark.parametrize("replayed", [False, True])
def test_action_admission_links_new_and_replayed_actions_to_same_task(
    tmp_path: Path, monkeypatch, replayed: bool,
) -> None:
    payload = {
        "turn_id": TURN, "action_id": ACTION, "status": "accepted",
        "replayed": replayed, "state": {"pending_action_ids": [ACTION]},
    }
    calls = []

    def submit_action(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(replayed=replayed, to_payload=lambda: dict(payload))

    monkeypatch.setattr(personal_world_model_routes, "_workflow", lambda *_: SimpleNamespace(
        submit_action=submit_action,
    ))
    body = {
        "command_id": "admission-command-12345678", "title": "Deliver a reviewable result",
        "expected_outcome": "The result can be reopened", "question": "Prepare the result",
    }
    with _client(tmp_path, _TurnAuthority()) as client:
        response = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/workflow/actions", json=body,
        )
    assert response.status_code == (200 if replayed else 202)
    assert response.json() == {
        **payload, "task_ref": task_ref_for_world_action(project_id=PROJECT, action_id=ACTION),
    }
    assert calls == [{"project_id": PROJECT, **body}]
