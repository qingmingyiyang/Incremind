from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import expert_catalog as expert_routes
from backend.api.routes.expert_catalog import router
from core.product_core.expert_catalog import default_video_research_expert_profile


CONTEXT_MANIFEST_ID = "context-manifest-turn-00000001"
MODEL_ROUTE_REVISION = "a" * 64
TOOL_REVISIONS = {
    "analyze_source": 3,
    "memory.recall": 1,
    "document.draft.propose": 2,
}


def _runtime_revisions(**overrides) -> dict:
    revisions = {
        "context_manifest_revision": CONTEXT_MANIFEST_ID,
        "boundary_revision": 4,
        "model_route_revision": MODEL_ROUTE_REVISION,
        "tool_capability_revisions": dict(TOOL_REVISIONS),
    }
    revisions.update(overrides)
    return revisions


def _client(tmp_path) -> TestClient:
    application = FastAPI()
    application.state.container = SimpleNamespace(root_dir=tmp_path)
    application.include_router(router)
    return TestClient(application)


def _authorize(monkeypatch) -> None:
    monkeypatch.setattr(expert_routes, "desktop_session", lambda: object())
    monkeypatch.setattr(expert_routes, "desktop_session_authorized", lambda _value: True)


def _profile() -> dict:
    return {**default_video_research_expert_profile(), "status": "active"}


def _create_expert(client: TestClient, monkeypatch, **overrides) -> object:
    response = client.post(
        "/api/ai/experts",
        json={
            "profile": overrides.pop("profile", _profile()),
            "expected_registry_revision": overrides.pop("expected", 0),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_routes_reject_unauthorized_requests(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(expert_routes, "desktop_session", lambda: object())
    monkeypatch.setattr(expert_routes, "desktop_session_authorized", lambda _value: False)
    client = _client(tmp_path)
    assert client.get("/api/ai/experts").status_code == 403
    assert client.post("/api/ai/experts", json={}).status_code == 403
    assert client.get("/api/ai/projects/project-a/expert-bindings").status_code == 403
    assert client.post("/api/ai/experts/selection-preview", json={}).status_code == 403


def test_expert_catalog_lifecycle_over_http(tmp_path, monkeypatch) -> None:
    _authorize(monkeypatch)
    client = _client(tmp_path)

    listing = client.get("/api/ai/experts")
    assert listing.status_code == 200
    assert listing.json() == {"registry_revision": 0, "experts": []}

    created = _create_expert(client, monkeypatch)
    assert created["revision"] == 1

    duplicate = client.post(
        "/api/ai/experts", json={"profile": _profile(), "expected_registry_revision": 1}
    )
    assert duplicate.status_code == 409

    lint_fail = client.post(
        "/api/ai/experts",
        json={"profile": {"expert_id": "bad id"}, "expected_registry_revision": 1},
    )
    assert lint_fail.status_code == 400

    upgraded = client.post(
        "/api/ai/experts/video-research-expert/upgrade",
        json={
            "profile": _profile() | {"method": "升级方法"},
            "expected_expert_revision": 1,
            "expected_registry_revision": 1,
        },
    )
    assert upgraded.status_code == 200
    assert upgraded.json()["revision"] == 2

    disabled = client.post(
        "/api/ai/experts/video-research-expert/status",
        json={
            "status": "disabled",
            "reason": "暂停",
            "expected_expert_revision": 2,
            "expected_registry_revision": 2,
        },
    )
    assert disabled.status_code == 200
    reactivated = client.post(
        "/api/ai/experts/video-research-expert/status",
        json={
            "status": "active",
            "reason": "恢复",
            "expected_expert_revision": 2,
            "expected_registry_revision": 3,
        },
    )
    assert reactivated.status_code == 200

    restarted = client.get("/api/ai/experts")
    assert restarted.json()["registry_revision"] == 4
    assert restarted.json()["experts"][0]["revision"] == 2


def test_binding_lifecycle_and_cas_conflicts_over_http(tmp_path, monkeypatch) -> None:
    _authorize(monkeypatch)
    client = _client(tmp_path)
    _create_expert(client, monkeypatch)

    bound = client.post(
        "/api/ai/projects/project-a/expert-bindings",
        json={
            "expert_id": "video-research-expert",
            "enabled_expert_revision": 1,
            "intent_affinity": ["media_analysis"],
            "selection_mode": "auto",
            "default": True,
            "reason": "试点",
            "expected_store_revision": 0,
        },
    )
    assert bound.status_code == 201, bound.text
    assert bound.json()["binding_revision"] == 1

    duplicate = client.post(
        "/api/ai/projects/project-a/expert-bindings",
        json={
            "expert_id": "video-research-expert",
            "enabled_expert_revision": 1,
            "intent_affinity": ["media_analysis"],
            "reason": "重复",
            "expected_store_revision": 1,
        },
    )
    assert duplicate.status_code == 409

    updated = client.post(
        "/api/ai/projects/project-a/expert-bindings/video-research-expert/update",
        json={
            "expected_binding_revision": 1,
            "expected_store_revision": 1,
            "selection_mode": "manual",
            "reason": "改手动",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["selection_mode"] == "manual"

    stale = client.post(
        "/api/ai/projects/project-a/expert-bindings/video-research-expert/update",
        json={
            "expected_binding_revision": 99,
            "expected_store_revision": 2,
            "reason": "过期",
        },
    )
    assert stale.status_code == 409

    removed = client.post(
        "/api/ai/projects/project-a/expert-bindings/video-research-expert/unbind",
        json={
            "expected_binding_revision": 2,
            "expected_store_revision": 2,
            "reason": "解绑",
        },
    )
    assert removed.status_code == 200
    assert client.get("/api/ai/projects/project-a/expert-bindings").json()["bindings"] == []


def test_selection_preview_and_snapshot_freeze_over_http(tmp_path, monkeypatch) -> None:
    _authorize(monkeypatch)
    client = _client(tmp_path)
    _create_expert(client, monkeypatch)
    client.post(
        "/api/ai/projects/project-a/expert-bindings",
        json={
            "expert_id": "video-research-expert",
            "enabled_expert_revision": 1,
            "intent_affinity": ["media_analysis"],
            "selection_mode": "auto",
            "default": True,
            "reason": "试点",
            "expected_store_revision": 0,
        },
    )

    preview = client.post(
        "/api/ai/experts/selection-preview",
        json={"project_id": "project-a", "task_intents": ["media_analysis"]},
    )
    assert preview.status_code == 200
    receipt = preview.json()
    assert receipt["selected"]["expert_id"] == "video-research-expert"
    assert receipt["selection_mode"] == "project_default"

    unbound = client.post(
        "/api/ai/experts/selection-preview",
        json={
            "project_id": "project-b",
            "task_intents": ["media_analysis"],
            "requested_expert_id": "video-research-expert",
        },
    )
    assert unbound.status_code == 200
    assert unbound.json()["selected"] is None
    assert unbound.json()["candidates"][0]["reason"] == "expert_not_bound_to_project"

    frozen = client.post(
        "/api/ai/experts/binding-snapshots",
        json={
            "selection_receipt": receipt,
            "budget": "research-2k",
            "runtime_revisions": _runtime_revisions(),
        },
    )
    assert frozen.status_code == 200, frozen.text
    snapshot = frozen.json()
    assert snapshot["snapshot_id"].startswith("ebs-")
    assert snapshot["expert_revision"] == 1
    assert snapshot["context_manifest_revision"] == CONTEXT_MANIFEST_ID

    # 目录升级后，同一 receipt 冻结必须拒绝（selection→freeze 之间漂移）
    client.post(
        "/api/ai/experts/video-research-expert/upgrade",
        json={
            "profile": _profile() | {"method": "新方法"},
            "expected_expert_revision": 1,
            "expected_registry_revision": 1,
        },
    )
    drifted = client.post(
        "/api/ai/experts/binding-snapshots",
        json={
            "selection_receipt": receipt,
            "runtime_revisions": _runtime_revisions(),
        },
    )
    assert drifted.status_code == 400
    assert "drifted" in drifted.json()["detail"]
