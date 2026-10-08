from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _payload(expected_revision: int) -> dict[str, object]:
    return {
        "expected_revision": expected_revision,
        "model_profiles": [],
        "prompts": [],
        "skills": [],
        "workflow_steps": [],
        "snapshots": [],
    }


def test_developer_studio_config_route_returns_conflict_for_stale_revision(tmp_path) -> None:
    client = _client(tmp_path)

    created = client.put("/api/rebuild/developer-studio/config", json=_payload(0))
    conflict = client.put("/api/rebuild/developer-studio/config", json=_payload(0))

    assert created.status_code == 200
    assert created.json()["revision"] == 1
    assert conflict.status_code == 409
    assert conflict.json() == {
        "status": "rejected",
        "detail": "developer studio config rejected",
        "error": "developer studio config revision conflict",
    }


def test_developer_studio_config_route_keeps_validation_errors_as_bad_requests(tmp_path) -> None:
    client = _client(tmp_path)
    payload = _payload(0)
    payload["model_profiles"] = [{"id": "unsafe", "api_key": "local-secret"}]

    response = client.put("/api/rebuild/developer-studio/config", json=payload)

    assert response.status_code == 400
    assert response.json()["status"] == "rejected"
    assert "sensitive field" in response.json()["error"]


def test_developer_studio_config_route_rejects_legacy_task_map_writes(tmp_path) -> None:
    client = _client(tmp_path)
    payload = _payload(0)
    payload["task_model_map"] = {"intakeMain": "mp-replacement"}

    response = client.put("/api/rebuild/developer-studio/config", json=payload)

    assert response.status_code == 400
    assert response.json() == {
        "status": "rejected",
        "detail": "developer studio config rejected",
        "error": "legacy task_model_map is read-only",
    }
