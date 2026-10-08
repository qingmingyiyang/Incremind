from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.ai import router
from backend.api.routes import ai as ai_routes


def _client(tmp_path) -> TestClient:
    application = FastAPI()
    application.state.container = SimpleNamespace(root_dir=tmp_path)
    application.include_router(router)
    return TestClient(application)


def test_ingress_selection_route_publishes_hands_and_revisioned_legacy_rollback(
    tmp_path, monkeypatch,
) -> None:
    ready = SimpleNamespace(readiness=lambda: SimpleNamespace(ready=True))
    monkeypatch.setattr(ai_routes, "get_or_build_ai_runtime", lambda *_args: object())
    monkeypatch.setattr(ai_routes, "current_media_hands_runtime", lambda _app: ready)
    client = _client(tmp_path)

    initial = client.get("/api/ai/media-ingress-selection")
    assert initial.status_code == 200
    assert initial.json()["mode"] == "legacy"
    assert initial.json()["revision"] == 0
    assert initial.json()["persisted"] is False
    assert initial.json()["activation"] == "after_in_flight_requests_drain"

    hands_body = {
        "command_id": "select-hands-route-0001",
        "expected_revision": 0,
        "confirm": True,
        "mode": "hands",
    }
    hands = client.post("/api/ai/media-ingress-selection/revisions", json=hands_body)
    replay = client.post("/api/ai/media-ingress-selection/revisions", json=hands_body)
    assert hands.status_code == replay.status_code == 200
    assert hands.json() == replay.json()
    assert hands.json()["mode"] == "hands" and hands.json()["revision"] == 1

    rollback = client.post(
        "/api/ai/media-ingress-selection/revisions",
        json={
            "command_id": "rollback-legacy-route-0002",
            "expected_revision": 1,
            "confirm": True,
            "mode": "legacy",
        },
    )
    assert rollback.status_code == 200
    assert rollback.json()["mode"] == "legacy" and rollback.json()["revision"] == 2


def test_ingress_selection_route_rejects_hands_when_runtime_is_not_ready(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(ai_routes, "get_or_build_ai_runtime", lambda *_args: object())
    monkeypatch.setattr(ai_routes, "current_media_hands_runtime", lambda _app: None)
    response = _client(tmp_path).post(
        "/api/ai/media-ingress-selection/revisions",
        json={
            "command_id": "select-hands-route-0001",
            "expected_revision": 0,
            "confirm": True,
            "mode": "hands",
        },
    )

    assert response.status_code == 409
    assert response.json()["reason"] == "media_hands_unavailable"
    assert _client(tmp_path).get("/api/ai/media-ingress-selection").json()["revision"] == 0


def test_ingress_selection_route_is_local_governance_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ai_routes, "desktop_session", lambda: object())
    monkeypatch.setattr(ai_routes, "desktop_session_authorized", lambda _value: False)
    client = _client(tmp_path)

    assert client.get("/api/ai/media-ingress-selection").status_code == 403
    assert client.post(
        "/api/ai/media-ingress-selection/revisions",
        json={
            "command_id": "rollback-legacy-route-0001",
            "expected_revision": 0,
            "confirm": True,
            "mode": "legacy",
        },
    ).status_code == 403
