from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import workbench_input_classifier as route_module


class _LegacyRuntime:
    def __init__(self): self.bodies = []
    def execute(self, *, method, path, body):
        self.bodies.append(body)
        return SimpleNamespace(status_code=200, body={"status": "classified", "provider_boundary": "local_rule_classifier_no_remote_provider", "processing_recipe_trace": {"consumer": "workbench.input-classifier"}}, headers={"Cache-Control": "no-store"})


class _TurnRuntime:
    composition_metadata = {"intake_classification_remote_usable": True}
    def __init__(self): self.requests = []; self.actions = []
    def submit_turn(self, request): self.requests.append(request); return SimpleNamespace(turn_id=request["turn_id"], status="waiting_approval", current_sequence=9)
    def events_after(self, _turn_id): return ({"type": "approval.required", "event_id": "event-approval"},)
    def apply_action(self, action): self.actions.append(action); return SimpleNamespace(turn_id=action["turn_id"], status="completed", current_sequence=12)
    def presentation_for(self, _turn_id): return {"status": "completed", "classification": {"status": "provider_enhanced", "input_type": "direct_idea"}}


def _client(monkeypatch):
    legacy, turn = _LegacyRuntime(), _TurnRuntime()
    monkeypatch.setattr(route_module, "build_rebuild_object_store", lambda _root: (object(), object()))
    monkeypatch.setattr(route_module, "build_workbench_input_classifier_runtime", lambda _container, _store: legacy)
    monkeypatch.setattr(route_module, "get_or_build_ai_runtime", lambda _request, _container: turn)
    app = FastAPI(); app.state.container = SimpleNamespace(root_dir="."); app.include_router(route_module.router)
    return TestClient(app), legacy, turn


def test_local_compatibility_forces_legacy_classifier_to_stay_local(monkeypatch):
    client, legacy, turn = _client(monkeypatch)
    with client:
        response = client.post("/api/rebuild/workbench/input-classifier", json={"content": "本地分类", "allow_provider_enhancement": False})
    assert response.status_code == 200 and turn.requests == []
    assert legacy.bodies == [{"content": "本地分类", "allow_provider_enhancement": False}]


def test_provider_compatibility_routes_through_approved_turn_and_revokes_grant(monkeypatch):
    client, legacy, turn = _client(monkeypatch)
    with client:
        response = client.post("/api/rebuild/workbench/input-classifier", json={"content": "原始正文", "urls": ["https://example.test/private"], "allow_provider_enhancement": True, "request_id": "classifier-request-1", "project_id": "project-a"})
        grant_store = client.app.state.workbench_classification_input_store
    assert response.status_code == 200 and response.json()["status"] == "provider_enhanced"
    assert legacy.bodies[0]["allow_provider_enhancement"] is False
    assert turn.requests[0]["scope"]["project_id"] == "project-a"
    assert turn.requests[0]["input"]["text"] == "enhance selected workbench input"
    assert turn.actions and turn.actions[0]["type"] == "approve"
    assert grant_store._records == {}


def test_provider_compatibility_requires_stable_request_id(monkeypatch):
    client, legacy, turn = _client(monkeypatch)
    with client:
        response = client.post("/api/rebuild/workbench/input-classifier", json={"content": "正文", "allow_provider_enhancement": True})
    assert response.status_code == 400 and legacy.bodies == [] and turn.requests == []
