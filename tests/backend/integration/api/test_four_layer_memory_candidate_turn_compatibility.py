from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import rebuild as route_module
from backend.api.routes.product import memory_candidates as product_memory_candidates


class Runtime:
    composition_metadata = {"memory_candidate_remote_usable": True}

    def __init__(self):
        self.requests = []
        self.actions = []

    def submit_turn(self, request):
        self.requests.append(request)
        return SimpleNamespace(turn_id=request["turn_id"], status="waiting_approval", current_sequence=5)

    def events_after(self, _turn_id):
        return ({"type": "approval.required", "event_id": "event-memory-approval"},)

    def apply_action(self, action):
        self.actions.append(action)
        return SimpleNamespace(turn_id=action["turn_id"], status="completed", current_sequence=9)

    def presentation_for(self, _turn_id):
        return {"status": "pending_review", "candidate_ids": ["candidate-1"], "candidate_count": 1}


def test_legacy_four_layer_endpoint_maps_to_durable_turn_without_direct_provider(monkeypatch):
    runtime = Runtime()
    monkeypatch.setattr(product_memory_candidates, "get_or_build_ai_runtime", lambda *_args: runtime)
    monkeypatch.setattr(route_module, "_build_deepseek_provider_for_rebuild", lambda *_args: (_ for _ in ()).throw(AssertionError("legacy provider called")))
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=".")
    app.include_router(route_module.router)
    with TestClient(app) as client:
        response = client.post("/api/rebuild/sources/source-1/four-layer-candidates", json={"project_id": "project-alpha", "evidence_kind": "source_content_read", "request_id": "memory-request-1", "confirm_egress": True})
        grant_store = client.app.state.four_layer_memory_candidate_evidence_grant_store
    assert response.status_code == 200
    assert response.json()["import_result"]["memory_publication_state"] == "candidates_created_not_published"
    assert runtime.requests[0]["desired_outcome"] == "memory.candidate.propose"
    assert runtime.actions[0]["type"] == "approve"
    assert grant_store._grants == {}
