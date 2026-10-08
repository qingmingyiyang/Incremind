from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.media_ingress_selection_authority import MediaIngressSelectionAuthority
from backend.api.routes.bilibili_media_ingress import router
from backend.api.routes import bilibili_media_ingress as ingress_routes
from core.storage_provider import SQLiteStructuredRecordStore


class _Resolver:
    def __init__(self, platform: str = "bilibili") -> None:
        self.platform = platform
        self.permission_granted = False
        self.calls = []

    def resolve(self, arguments, scope):
        self.calls.append((arguments, scope))
        return SimpleNamespace(
            manifest=SimpleNamespace(
                platform=self.platform,
                source_id=f"{self.platform}-source-1",
                content_kind="mixed" if self.platform == "xiaohongshu" else "video",
            ),
            manifest_ref="crp://default/source-manifests/projects/project-1/bili-r1",
            manifest_revision="r1",
            terminal_reason=None,
            permission_snapshot=(object() if self.permission_granted else None),
        )


class _Runtime:
    namespace_id = "default"

    def __init__(self, platform: str = "bilibili") -> None:
        self.resolver = _Resolver(platform)
        self.admissions = {}
        self.calls = []

    def readiness(self):
        return SimpleNamespace(ready=True)

    def provision(self, **kwargs):
        self.calls.append(kwargs)
        key = kwargs["idempotency_key"]
        replayed = key in self.admissions
        record = self.admissions.setdefault(
            key,
            SimpleNamespace(payload={"id": "media_hands:bili-source-1:analyze_source"}),
        )
        return SimpleNamespace(record=record, replayed=replayed)


def _client(tmp_path, runtime: _Runtime, monkeypatch) -> TestClient:
    application = FastAPI()
    application.state.container = SimpleNamespace(root_dir=tmp_path)
    application.include_router(router)
    monkeypatch.setattr(ingress_routes, "get_or_build_ai_runtime", lambda *_args: object())
    monkeypatch.setattr(ingress_routes, "current_media_hands_runtime", lambda _app: runtime)
    return TestClient(application)


def _select_hands(tmp_path) -> None:
    MediaIngressSelectionAuthority(
        SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "jobs.sqlite3")
    ).publish(
        "hands", expected_revision=0, command_id="select-hands-route-0001",
        actor="local-user", created_at="2026-08-26T00:00:00Z",
    )


def test_resolve_freezes_manifest_and_requires_explicit_permission(tmp_path, monkeypatch) -> None:
    _select_hands(tmp_path)
    runtime = _Runtime()
    response = _client(tmp_path, runtime, monkeypatch).post(
        "/api/rebuild/media-ingress/bilibili/resolve",
        json={
            "ingress_request_id": "ingress-resolve-0001",
            "project_id": "default",
            "url": "https://www.bilibili.com/video/BV1xx411c7mD/",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "permission_required"
    assert response.json()["permission"] == "unknown"
    assert response.json()["project_id"] == "default"
    assert response.json()["manifest_ref"].startswith("crp://default/source-manifests/")
    assert runtime.calls == []
    assert runtime.resolver.calls[0][0] == {
        "input": {
            "kind": "text",
            "text": "https://www.bilibili.com/video/BV1xx411c7mD/",
            "source_ref": None,
        }
    }


def test_favorite_resolve_returns_frozen_collection_preview(tmp_path, monkeypatch) -> None:
    snapshot = SimpleNamespace(
        replayed=False,
        public_ref=(
            "crp://default/bilibili-favorite-snapshots/projects/default/"
            "favorite-resolve-0001/r1"
        ),
        revision="r1",
        payload={
            "favorite_id": "1103407912",
            "title": "研究收藏夹",
            "owner": "测试用户",
            "page_count": 2,
            "raw_item_count": 2,
            "video_item_count": 2,
            "skipped_counts": {"non_video": 0, "unavailable": 0, "duplicate": 0},
            "items": [
                {
                    "ordinal": 0,
                    "bvid": "BV1xx411c7mD",
                    "url": "https://www.bilibili.com/video/BV1xx411c7mD/",
                    "title": "第一条",
                    "uploader": "",
                    "duration_seconds": 10,
                }
            ],
        },
    )
    captured = {}
    service = SimpleNamespace(
        resolve=lambda **kwargs: captured.update(kwargs) or snapshot
    )
    monkeypatch.setattr(
        ingress_routes,
        "build_rebuild_object_store",
        lambda *_args: (object(), SimpleNamespace(namespace_id="default")),
    )
    monkeypatch.setattr(
        ingress_routes,
        "build_bilibili_favorite_collection_service",
        lambda *_args, **_kwargs: service,
    )

    response = _client(tmp_path, _Runtime(), monkeypatch).post(
        "/api/rebuild/media-ingress/bilibili/favorites/resolve",
        json={
            "collection_request_id": "favorite-resolve-0001",
            "project_id": "default",
            "url": "https://space.bilibili.com/84912/favlist?fid=1103407912&ftype=create",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "snapshot_ready"
    assert response.json()["video_item_count"] == 2
    assert response.json()["items"][0]["bvid"] == "BV1xx411c7mD"
    assert captured["snapshot_id"] == "favorite-resolve-0001"


def test_platform_neutral_route_resolves_and_admits_xiaohongshu_without_private_locators(
    tmp_path, monkeypatch,
) -> None:
    _select_hands(tmp_path)
    runtime = _Runtime("xiaohongshu")
    client = _client(tmp_path, runtime, monkeypatch)
    resolved = client.post(
        "/api/rebuild/media-ingress/resolve",
        json={
            "ingress_request_id": "media-resolve-xhs-0001",
            "project_id": "default",
            "url": "https://www.xiaohongshu.com/explore/65f1234567890abc12345678",
        },
    )
    assert resolved.status_code == 200
    assert resolved.json()["platform"] == "xiaohongshu"
    assert resolved.json()["content_kind"] == "mixed"
    assert runtime.calls == []

    runtime.resolver.permission_granted = True
    admitted = client.post(
        "/api/rebuild/media-ingress/admit",
        json={
            "ingress_request_id": "media-admit-xhs-0001",
            "project_id": "default",
            "manifest_ref": resolved.json()["manifest_ref"],
        },
    )
    assert admitted.status_code == 202
    assert admitted.json()["platform"] == "xiaohongshu"
    assert runtime.calls[0]["operation"] == "analyze_source"
    assert runtime.calls[0]["idempotency_key"] == "xiaohongshu-ingress-media-admit-xhs-0001"
    assert not any(key in admitted.text.lower() for key in ("cookie", "cdn", "output_root"))


def test_explicit_ingress_composes_media_hands_before_ai_runtime_freezes(
    tmp_path, monkeypatch,
) -> None:
    application = FastAPI()
    container = SimpleNamespace(root_dir=tmp_path)
    application.state.container = container
    request = SimpleNamespace(app=application)
    runtime = _Runtime()
    events: list[str] = []

    monkeypatch.setattr(
        ingress_routes,
        "current_media_hands_runtime",
        lambda _application: runtime if "compose" in events else None,
    )
    monkeypatch.setattr(
        "backend.api.media_hands_composition.compose_application_media_hands",
        lambda app, resolved: events.append("compose"),
    )
    monkeypatch.setattr(
        ingress_routes,
        "get_or_build_ai_runtime",
        lambda _request, _container: events.append("ai"),
    )

    assert ingress_routes._runtime(request, container) is runtime
    assert events == ["compose", "ai"]


def test_admit_requires_permission_then_replays_one_fixed_media_job(tmp_path, monkeypatch) -> None:
    _select_hands(tmp_path)
    runtime = _Runtime()
    client = _client(tmp_path, runtime, monkeypatch)
    body = {
        "ingress_request_id": "ingress-admit-0001",
        "project_id": "project-1",
        "manifest_ref": "crp://default/source-manifests/projects/project-1/bili-r1",
    }

    denied = client.post("/api/rebuild/media-ingress/bilibili/admit", json=body)
    assert denied.status_code == 409
    assert denied.json()["status"] == "permission_required"
    assert runtime.calls == []

    runtime.resolver.permission_granted = True
    admitted = client.post("/api/rebuild/media-ingress/bilibili/admit", json=body)
    replay = client.post("/api/rebuild/media-ingress/bilibili/admit", json=body)
    assert admitted.status_code == 202 and replay.status_code == 200
    assert admitted.json()["job_id"] == replay.json()["job_id"]
    assert admitted.json()["replayed"] is False and replay.json()["replayed"] is True
    assert {call["operation"] for call in runtime.calls} == {"analyze_source"}
    assert {call["idempotency_key"] for call in runtime.calls} == {
        "bilibili-ingress-ingress-admit-0001"
    }


def test_requeue_legacy_unknown_creates_fresh_media_hands_job_without_mutating_history(
    tmp_path, monkeypatch,
) -> None:
    _select_hands(tmp_path)
    runtime = _Runtime()
    runtime.resolver.permission_granted = True
    legacy = {
        "id": "legacy-media-job-0001",
        "job_type": "media_hands",
        "execution_version": "legacy-v1-readonly",
        "status": "legacy_unknown",
        "media_hands": {
            "manifest": {
                "ref": "crp://default/source-manifests/projects/project-1/bili-r1",
            },
            "permission_snapshot": {"project_id": "project-1"},
        },
    }
    repository = SimpleNamespace(get=lambda job_id: legacy if job_id == legacy["id"] else None)
    monkeypatch.setattr(
        ingress_routes,
        "build_rebuild_job_repository",
        lambda *_args: repository,
    )
    monkeypatch.setattr(
        ingress_routes,
        "build_rebuild_object_store",
        lambda *_args: (object(), SimpleNamespace()),
    )
    client = _client(tmp_path, runtime, monkeypatch)

    response = client.post(
        "/api/rebuild/media-ingress/jobs/legacy-media-job-0001/requeue",
        json={"command_id": "legacy-requeue-0001"},
    )

    assert response.status_code == 202
    assert response.json()["requeued_from_job_id"] == "legacy-media-job-0001"
    assert response.json()["job_id"] == "media_hands:bili-source-1:analyze_source"
    assert runtime.calls[0]["idempotency_key"] == "bilibili-ingress-legacy-requeue-0001"
    assert legacy["status"] == "legacy_unknown"


def test_retry_failed_bilibili_postprocess_creates_a_fresh_child(
    tmp_path, monkeypatch,
) -> None:
    previous = {
        "id": "bilibili-postprocess:media-job-1",
        "job_type": "bilibili_media_postprocess",
        "execution_version": "effect-v2",
        "status": "failed",
    }
    repository = SimpleNamespace(
        sqlite=SimpleNamespace(database_path=tmp_path / "jobs.sqlite3"),
        get=lambda job_id: previous if job_id == previous["id"] else None,
    )
    captured = {}
    monkeypatch.setattr(
        ingress_routes,
        "build_rebuild_job_repository",
        lambda *_args: repository,
    )
    monkeypatch.setattr(
        ingress_routes,
        "build_rebuild_object_store",
        lambda *_args: (object(), SimpleNamespace(namespace_id="default")),
    )

    def readmit(**kwargs):
        captured.update(kwargs)
        return {"id": "bilibili-postprocess-retry:retry-command-0001"}

    monkeypatch.setattr(ingress_routes, "readmit_bilibili_postprocess", readmit)
    response = _client(tmp_path, _Runtime(), monkeypatch).post(
        "/api/rebuild/media-ingress/postprocess/bilibili-postprocess%3Amedia-job-1/retry",
        json={"command_id": "retry-command-0001"},
    )

    assert response.status_code == 202
    assert response.json() == {
        "status": "admitted",
        "job_id": "bilibili-postprocess-retry:retry-command-0001",
        "rebuilt_from_job_id": "bilibili-postprocess:media-job-1",
    }
    assert captured["previous_job"] is previous
    assert captured["namespace_id"] == "default"
    assert previous["status"] == "failed"


def test_hands_routes_reject_legacy_parameters_and_legacy_mode_before_runtime(
    tmp_path, monkeypatch,
) -> None:
    runtime = _Runtime()
    client = _client(tmp_path, runtime, monkeypatch)
    legacy = client.post(
        "/api/rebuild/media-ingress/bilibili/resolve",
        json={
            "ingress_request_id": "ingress-resolve-0001",
            "project_id": "project-1",
            "url": "https://www.bilibili.com/video/BV1xx411c7mD/",
        },
    )
    assert legacy.status_code == 409
    assert legacy.json()["status"] == "hands_ingress_disabled"
    assert runtime.resolver.calls == []

    _select_hands(tmp_path)
    rejected = client.post(
        "/api/rebuild/media-ingress/bilibili/resolve",
        json={
            "ingress_request_id": "ingress-resolve-0002",
            "project_id": "project-1",
            "url": "https://www.bilibili.com/video/BV1xx411c7mD/",
            "output_root": "user-path",
        },
    )
    assert rejected.status_code == 400
    assert rejected.json()["reason"] == "ingress_body_invalid"
    assert runtime.resolver.calls == []


def test_admit_rejects_request_id_reuse_with_a_different_manifest(
    tmp_path, monkeypatch,
) -> None:
    _select_hands(tmp_path)
    runtime = _Runtime()
    client = _client(tmp_path, runtime, monkeypatch)
    first = client.post(
        "/api/rebuild/media-ingress/bilibili/admit",
        json={
            "ingress_request_id": "ingress-admit-drift-0001",
            "project_id": "project-1",
            "manifest_ref": "crp://default/source-manifests/projects/project-1/bili-r1",
        },
    )
    drift = client.post(
        "/api/rebuild/media-ingress/bilibili/admit",
        json={
            "ingress_request_id": "ingress-admit-drift-0001",
            "project_id": "project-1",
            "manifest_ref": "crp://default/source-manifests/projects/project-1/bili-r2",
        },
    )

    assert first.status_code == 409 and first.json()["status"] == "permission_required"
    assert drift.status_code == 409 and drift.json()["status"] == "request_conflict"
    assert len(runtime.resolver.calls) == 1
