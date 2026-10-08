from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.ai import router
from backend.security.project_boundary_mode_command import BoundaryModeCommandConflict


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    app.include_router(router)
    return TestClient(app)


def test_mode_command_route_is_strict_local_and_does_not_build_runtime(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post("/api/ai/projects/alpha/boundary-mode", json={
            "command_id": "mode-1", "mode": "open", "expected_boundary_revision": 1,
            "expected_capability_revision": 1, "confirm": True,
        })
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.headers["cache-control"] == "no-store"


def test_mode_command_route_rejects_extra_or_unconfirmed_body(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post("/api/ai/projects/alpha/boundary-mode", json={
            "command_id": "mode-1", "mode": "open", "expected_boundary_revision": 1,
            "expected_capability_revision": 1, "confirm": False, "ignored": True,
        })
    assert response.status_code == 400


def test_mode_command_route_maps_busy_receipt_authority_to_conflict(tmp_path: Path, monkeypatch) -> None:
    def busy_service(_root_dir):
        raise BoundaryModeCommandConflict("Boundary mode receipt authority is busy")

    monkeypatch.setattr("backend.api.routes.ai.ProjectBoundaryModeCommandService", busy_service)
    with _client(tmp_path) as client:
        response = client.post("/api/ai/projects/alpha/boundary-mode", json={
            "command_id": "mode-1", "mode": "open", "expected_boundary_revision": 1,
            "expected_capability_revision": 1, "confirm": True,
        })
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
