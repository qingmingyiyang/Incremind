from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.ai_turn_runner import AITurnRunnerCapacityError
from backend.api.app import create_app
from backend.api.personal_world_model_context import frozen_world_state_planning
from core.ai_kernel.tool_invocation import intent_from_payload
from core.effect_log import EffectState


PROJECT = "default"
GOAL_COMMAND = "goal-vertical-0001"
FIRST_COMMAND = "action-vertical-0001"
SECOND_COMMAND = "action-vertical-0002"
FEEDBACK_COMMAND = "feedback-vertical-0001"


class _CapacityUnavailableOrganization:
    def start(self, request, *, agent_turn_mode=False):
        del request, agent_turn_mode
        raise AITurnRunnerCapacityError("capacity unavailable")


def _goal() -> dict[str, object]:
    return {
        "command_id": GOAL_COMMAND,
        "title": "Make project planning learn from verified outcomes",
        "success_criteria": ["The second governed Tool Intent contains the first feedback"],
    }


def _action(command_id: str, title: str) -> dict[str, object]:
    return {
        "command_id": command_id,
        "title": title,
        "expected_outcome": "A grounded next project step is ready for user evaluation",
        "question": "Based on the project state, what is the next evidence-grounded step?",
    }


def _wait_for_terminal(client: TestClient, turn_id: str) -> dict[str, object]:
    for _ in range(160):
        response = client.get(
            f"/api/rebuild/projects/{PROJECT}/world-model/workflow/actions/{turn_id}"
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["terminal"] is True:
            return payload
        time.sleep(0.02)
    raise AssertionError("project-world action did not reach a terminal receipt")


def _tool_intent(store, turn_id: str):
    event = next(
        item
        for item in store.events_after(turn_id)
        if item["type"] == "tool.intent.recorded"
    )
    return intent_from_payload(store.get(event["data"]["payload_ref"]))


def test_real_project_vertical_crosses_gate_effect_receipt_and_next_plan_uses_feedback(
    tmp_path: Path,
) -> None:
    application = create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(application) as client:
        goal = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/workflow/goal",
            json=_goal(),
        )
        assert goal.status_code == 201, goal.text

        first = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/workflow/actions",
            json=_action(FIRST_COMMAND, "Inspect the first project step"),
        )
        assert first.status_code == 202, first.text
        first_turn = first.json()["turn_id"]
        first_status = _wait_for_terminal(client, first_turn)
        assert first_status["status"] == "completed"
        assert first_status["governance"] == {
            "gate": "passed",
            "effect": "settled",
            "handler": "completed",
            "receipt": "terminal",
            "feedback": "not_recorded",
        }

        feedback = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/workflow/actions/{first_turn}/feedback",
            json={
                "command_id": FEEDBACK_COMMAND,
                "actual_outcome": "The first recommendation was useful but one acceptance check remains",
                "outcome": "partial",
                "state_delta": [
                    {
                        "field": "acceptance.status",
                        "before": "assumed",
                        "after": "pending",
                    }
                ],
                "user_evaluation": {
                    "verdict": "corrected",
                    "rating": 3,
                    "note": "Keep the remaining acceptance check in the next plan",
                },
            },
        )
        assert feedback.status_code == 201, feedback.text
        feedback_id = feedback.json()["feedback_id"]
        assert feedback.json()["state"]["phase"] == "needs_revision"
        assert (
            feedback.json()["state"]["supervision"]["latest_decision"]["disposition"]
            == "replan_required"
        )

        second = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/workflow/actions/pivot",
            json={
                **_action(SECOND_COMMAND, "Revise the project step from feedback"),
                "supersedes_action_id": first.json()["action_id"],
            },
        )
        assert second.status_code == 202, second.text
        second_turn = second.json()["turn_id"]
        second_status = _wait_for_terminal(client, second_turn)
        assert second_status["context"]["latest_feedback_consumed"] is True
        assert second_status["context"]["latest_feedback_outcome"] == "partial"

        store = application.state.ai_turn_effect_store
        first_events = tuple(store.events_after(first_turn))
        second_events = tuple(store.events_after(second_turn))
        first_context = frozen_world_state_planning(
            first_events,
            store,
            turn_id=first_turn,
            project_id=PROJECT,
        )
        second_context = frozen_world_state_planning(
            second_events,
            store,
            turn_id=second_turn,
            project_id=PROJECT,
        )
        first_intent = _tool_intent(store, first_turn)
        second_intent = _tool_intent(store, second_turn)
        assert first_context["latest_feedback"] is None
        assert second_context["latest_feedback"]["feedback_id"] == feedback_id
        assert first_intent.capability_id == "agent.list"
        assert second_intent.capability_id == "agent.list"

        topology = client.get(
            f"/api/ai/projects/{PROJECT}/agent-topology?turn_id={second_turn}"
        )
        assert topology.status_code == 200, topology.text
        assert topology.json()["run"]["profile_id"] == "main.orchestrator"
        assert any(
            item["profile_id"] == "steward.scheduler"
            for item in topology.json()["children"]
        )

        effect = application.state.ai_effect_runtime.log.get(second_intent.invocation_id)
        assert effect.kind == "tool_call_pure"
        assert effect.state is EffectState.SETTLED_OK
        assert effect.gate_decision_id
        assert any(item["type"] == "tool.completed" for item in second_events)
        receipt = application.state.ai_runtime.receipt_for(second_turn)
        assert receipt.status == "completed"
        assert receipt.operation_id == second.json()["action_id"]

        crossed = client.get(
            f"/api/rebuild/projects/project-other/world-model/workflow/actions/{second_turn}"
        )
        assert crossed.status_code == 409

    restarted_application = create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(restarted_application) as restarted:
        overview = restarted.get(
            f"/api/rebuild/projects/{PROJECT}/world-model/workflow"
        )
        replay = restarted.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/workflow/actions/pivot",
            json={
                **_action(SECOND_COMMAND, "Revise the project step from feedback"),
                "supersedes_action_id": first.json()["action_id"],
            },
        )

        assert overview.status_code == 200, overview.text
        assert overview.json()["state"]["latest_feedback_id"] == feedback_id
        assert len(overview.json()["recent_actions"]) == 2
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True


def test_capacity_failure_keeps_the_real_api_plan_retryable(tmp_path: Path) -> None:
    application = create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(application) as client:
        goal = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/workflow/goal",
            json=_goal(),
        )
        assert goal.status_code == 201, goal.text

        organization = application.state.agent_organization_runtime
        application.state.agent_organization_runtime = _CapacityUnavailableOrganization()
        unavailable = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/workflow/actions",
            json=_action(FIRST_COMMAND, "Inspect the retryable project step"),
        )
        assert unavailable.status_code == 503, unavailable.text
        assert unavailable.json()["detail"] == "personal_world_model_workflow_capacity"

        pending = client.get(
            f"/api/rebuild/projects/{PROJECT}/world-model/workflow"
        )
        assert pending.status_code == 200, pending.text
        assert pending.json()["state"]["pending_action_ids"] == [
            f"world-action-{FIRST_COMMAND}"
        ]
        assert pending.json()["recent_actions"][0]["status"] == "not_admitted"

        application.state.agent_organization_runtime = organization
        retried = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/workflow/actions",
            json=_action(FIRST_COMMAND, "Inspect the retryable project step"),
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["replayed"] is True
        terminal = _wait_for_terminal(client, retried.json()["turn_id"])
        assert terminal["status"] == "completed"
