from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import project_task_cases


PROJECT = "task-case-api"


class _Workflow:
    def overview(self, *, project_id: str):
        return {
            "state": {
                "project_id": project_id,
                "phase": "ready",
                "goal": {"goal_id": "goal-1", "title": "Organize", "success_criteria": []},
                "tasks": [], "blockers": [], "planned_actions": [],
                "pending_action_ids": [], "latest_feedback_id": None,
                "confidence": 1.0, "risk_codes": [],
            },
            "recent_actions": [],
        }


def _client(monkeypatch, *, client_address=("testclient", 50000)) -> TestClient:
    application = FastAPI()
    application.state.container = SimpleNamespace(root_dir=Path("."))
    monkeypatch.setattr(project_task_cases, "_workflow", lambda *_args: _Workflow())
    application.include_router(project_task_cases.router)
    return TestClient(application, client=client_address)


def test_task_case_api_is_local_no_store_and_does_not_accept_turn_query(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        response = client.get(f"/api/rebuild/projects/{PROJECT}/task-case?turn_id=foreign-turn")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["action"] is None
    assert response.json()["organization"] is None


def test_task_case_api_rejects_nonlocal_request(monkeypatch) -> None:
    with _client(monkeypatch, client_address=("203.0.113.5", 50000)) as client:
        response = client.get(
            f"/api/rebuild/projects/{PROJECT}/task-case",
        )

    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"


def test_task_case_api_uses_the_local_task_graph_callback_without_exposing_graph_ids(monkeypatch) -> None:
    graph = SimpleNamespace(
        spec=SimpleNamespace(project_id=PROJECT, nodes=(
            SimpleNamespace(node_id="private-node-one", dependency_ids=()),
            SimpleNamespace(node_id="private-node-two", dependency_ids=("private-node-one",)),
        )),
        nodes=(
            SimpleNamespace(node_id="private-node-one", status="settled", validation_status="verified"),
            SimpleNamespace(node_id="private-node-two", status="waiting_dependency", validation_status="pending"),
        ),
    )
    monkeypatch.setattr(project_task_cases, "_task_graph_trace_for_project", lambda *_args: lambda project_id: graph if project_id == PROJECT else None)

    with _client(monkeypatch) as client:
        response = client.get(f"/api/rebuild/projects/{PROJECT}/task-case")

    assert response.status_code == 200
    trace = response.json()["trace"]
    assert trace["dependencies"]["items"] == [{
        "from_label": "任务节点 2", "relation_label": "依赖于",
        "to_label": "任务节点 1", "state_label": "等待依赖",
    }]
    assert trace["verification"] == {
        "state": "pending", "verified_count": 1, "pending_count": 1,
        "latest_kind_label": "任务图节点核验",
    }
    assert "private-node" not in str(trace).lower()


def test_task_graph_callback_groups_world_records_and_projects_the_latest_graph(monkeypatch) -> None:
    graph_one_created = SimpleNamespace(project_id=PROJECT, graph_id="graph-one")
    graph_two_created = SimpleNamespace(project_id=PROJECT, graph_id="graph-two")
    graph_one_updated = SimpleNamespace(project_id=PROJECT, graph_id="graph-one")
    world_events = tuple(
        SimpleNamespace(kind=project_task_cases.WorldEventKind.TASK_GRAPH_EVENT_RECORDED, payload={"graph": graph})
        for graph in (graph_one_created, graph_two_created, graph_one_updated)
    )
    workflow = SimpleNamespace(_world=SimpleNamespace(events=lambda project_id: world_events if project_id == PROJECT else ()))
    monkeypatch.setattr(project_task_cases.TaskGraphEvent, "from_payload", lambda payload: payload["graph"])
    projected: list[tuple[object, ...]] = []
    sentinel = SimpleNamespace()
    monkeypatch.setattr(project_task_cases, "project_task_graph", lambda events: projected.append(tuple(events)) or sentinel)

    assert project_task_cases._task_graph_trace_for_project(workflow)(PROJECT) is sentinel
    assert projected == [(graph_one_created, graph_one_updated)]
