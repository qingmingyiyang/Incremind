from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.ai import router
from backend.api.routes import ai as ai_routes
from core.media_hands import default_personal_workbench_policy_snapshot
from core.storage_provider import SQLiteStructuredRecordStore


def _client(tmp_path) -> TestClient:
    application = FastAPI()
    application.state.container = SimpleNamespace(root_dir=tmp_path)
    application.include_router(router)
    return TestClient(application)


def _policy(revision: int, *, enabled: bool = True) -> dict[str, object]:
    policy = default_personal_workbench_policy_snapshot()
    policy["enabled"] = enabled
    policy["revision"] = f"personal-workbench-r{revision}"
    return policy


def test_media_policy_route_publishes_cas_revision_and_replays_command(tmp_path) -> None:
    client = _client(tmp_path)
    missing = client.get("/api/ai/media-hands-policy")
    assert missing.status_code == 200
    assert missing.json()["persisted"] is False
    assert missing.json()["policy"]["enabled"] is False
    assert missing.json()["activation"] == "restart_required"

    body = {
        "command_id": "media-policy-route-0001",
        "expected_revision": 0,
        "confirm": True,
        "policy": _policy(1),
    }
    created = client.post("/api/ai/media-hands-policy/revisions", json=body)
    replay = client.post("/api/ai/media-hands-policy/revisions", json=body)

    assert created.status_code == replay.status_code == 200
    assert created.json() == replay.json()
    assert created.json()["revision"] == 1
    assert created.json()["activation"] == "restart_required"
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "jobs.sqlite3")
    assert len(records.list("media_hands_policy_revisions")) == 1
    assert len(records.list("media_hands_policy_commands")) == 1


def test_media_policy_route_rejects_unconfirmed_extra_and_stale_commands(tmp_path) -> None:
    client = _client(tmp_path)
    base = {
        "command_id": "media-policy-route-0001",
        "expected_revision": 0,
        "confirm": True,
        "policy": _policy(1),
    }
    assert client.post(
        "/api/ai/media-hands-policy/revisions", json={**base, "confirm": False}
    ).status_code == 400
    assert client.post(
        "/api/ai/media-hands-policy/revisions", json={**base, "unexpected": True}
    ).status_code == 400
    assert client.post("/api/ai/media-hands-policy/revisions", json=base).status_code == 200
    stale = {
        "command_id": "media-policy-route-0002",
        "expected_revision": 0,
        "confirm": True,
        "policy": _policy(2, enabled=False),
    }
    assert client.post(
        "/api/ai/media-hands-policy/revisions", json=stale
    ).status_code == 409


def test_media_policy_route_requires_authorized_desktop_session(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setattr(ai_routes, "desktop_session", lambda: object())
    monkeypatch.setattr(ai_routes, "desktop_session_authorized", lambda _value: False)
    client = _client(tmp_path)

    assert client.get("/api/ai/media-hands-policy").status_code == 403
    assert client.post(
        "/api/ai/media-hands-policy/revisions",
        json={
            "command_id": "media-policy-route-0001",
            "expected_revision": 0,
            "confirm": True,
            "policy": _policy(1),
        },
    ).status_code == 403
    assert not (tmp_path / ".rebuild-data" / "jobs.sqlite3").exists()


def test_media_policy_route_persists_disable_and_reenable_revisions(tmp_path) -> None:
    client = _client(tmp_path)
    for revision, enabled in ((1, True), (2, False), (3, True)):
        response = client.post(
            "/api/ai/media-hands-policy/revisions",
            json={
                "command_id": f"media-policy-route-{revision:04d}",
                "expected_revision": revision - 1,
                "confirm": True,
                "policy": _policy(revision, enabled=enabled),
            },
        )
        assert response.status_code == 200
        assert response.json()["revision"] == revision
        assert response.json()["policy"]["enabled"] is enabled

    current = client.get("/api/ai/media-hands-policy")
    assert current.status_code == 200
    assert current.json()["revision"] == 3
    assert current.json()["policy"]["enabled"] is True
