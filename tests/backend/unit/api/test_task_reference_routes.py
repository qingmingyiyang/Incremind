from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.container import get_container
from backend.api.routes import tasks
from backend.api.bilibili_favorite_batch import BilibiliFavoriteBatchRepository
from core.storage_provider import JsonObjectStore
from core.storage_provider import SQLiteStructuredRecordStore


def _client(tmp_path, monkeypatch, *, media: bool = False) -> TestClient:
    store = JsonObjectStore(tmp_path / "objects", namespace_id="default")
    repository = BilibiliFavoriteBatchRepository(store, namespace_id="default")
    repository.create(
        batch_id="favorite-batch-0001", project_id="project-a",
        snapshot_ref="crp://default/favorites/project-a/favorite-batch-0001",
        snapshot_revision="r1", items=[], created_at="2026-09-05T00:00:00Z",
    )
    if media:
        repository.object_store.write("sources", "source-media-1", {
            "id": "source-media-1", "project_id": "project-a", "type": "audio",
        }, expected_revision=None)
        repository.object_store.write("media_processing_jobs", "media-job-source-media-1", {
            "id": "media-job-source-media-1", "source_id": "source-media-1", "source_type": "audio",
            "status": "completed", "updated_at": "2026-09-06T00:01:00Z",
        }, expected_revision=None)
        repository.object_store.write("media_processing_outputs", "output-transcript", {
            "id": "output-transcript", "job_id": "media-job-source-media-1", "source_id": "source-media-1",
            "output_kind": "transcript", "status": "completed",
        }, expected_revision=None)
    monkeypatch.setattr(tasks, "_repository", lambda _container: (tmp_path, repository))
    monkeypatch.setattr(tasks, "_batch_projection", lambda _root, _store, payload: {
        "revision": payload["revision"], "updated_at": payload["updated_at"],
        "total": 1, "admission_status": "not_started", "processing_status": "not_started",
        "counts": {}, "has_failed": False, "child_outputs": [],
    })
    app = FastAPI()
    app.include_router(tasks.router)
    app.dependency_overrides[get_container] = lambda: SimpleNamespace(root_dir=tmp_path)
    return TestClient(app)


def test_task_routes_require_project_scope_and_use_no_store(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    missing = client.get("/api/rebuild/tasks")
    invalid = client.get("/api/rebuild/tasks?project_id=not%20a%20project")
    listed = client.get("/api/rebuild/tasks?project_id=project-a")

    assert missing.status_code == 422
    assert invalid.status_code == 400
    assert invalid.headers["cache-control"] == "no-store"
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    assert listed.json()["items"][0]["project_id"] == "project-a"


def test_task_routes_forward_only_supported_server_filters(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    active = client.get("/api/rebuild/tasks?project_id=project-a&filter=active")
    unsupported = client.get("/api/rebuild/tasks?project_id=project-a&filter=all")

    assert active.status_code == 200
    assert active.json()["items"][0]["status"] == "accepted"
    assert unsupported.status_code == 400
    assert unsupported.json() == {"detail": "task_query_invalid"}


def test_task_detail_hides_invalid_cross_project_and_missing_owners(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    task_ref = client.get("/api/rebuild/tasks?project_id=project-a").json()["items"][0]["task_ref"]

    malformed = client.get("/api/rebuild/tasks/not-a-ref?project_id=project-a")
    cross_project = client.get(f"/api/rebuild/tasks/{task_ref}?project_id=project-b")
    missing = client.get(
        "/api/rebuild/tasks/tr1_eyJpIjoiZmF2b3JpdGUtYmF0Y2gtMDAwMiIsImsiOiJiaWxpYmlsaV9mYXZvcml0ZV9iYXRjaCIsInAiOiJwcm9qZWN0LWEiLCJ2IjoidGFzay1yZWYudjEifQ?project_id=project-a"
    )

    for response in (malformed, cross_project, missing):
        assert response.headers["cache-control"] == "no-store"
    assert malformed.status_code == cross_project.status_code == 400
    assert missing.status_code == 404


def test_task_routes_expose_source_anchored_media_job_with_library_deep_link(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch, media=True)

    listed = client.get("/api/rebuild/tasks?project_id=project-a")
    media = next(item for item in listed.json()["items"] if item["title"] == "音频转写")
    detail = client.get(f"/api/rebuild/tasks/{media['task_ref']}?project_id=project-a")

    assert listed.status_code == detail.status_code == 200
    assert media["status"] == "delivered"
    assert detail.json()["detail"]["outputs"] == [{
        "artifact_id": "output-transcript", "kind": "transcript", "title": "转写文本",
        "status": "completed", "href": "#view=rebuild-library-overview&source_id=source-media-1",
    }]


def test_task_routes_project_only_verified_world_action_without_turn_content(tmp_path, monkeypatch) -> None:
    action_id = "world-action-12345678"
    turn_id = "world-turn-12345678"

    class _Workflow:
        def overview(self, *, project_id: str):
            return {"state": {"project_id": project_id, "planned_actions": [{
                "action_id": action_id, "title": "整理项目资料", "expected_outcome": "private",
            }]}}

        def action_status(self, *, project_id: str, turn_id: str):
            assert project_id == "project-a" and turn_id == "world-turn-12345678"
            return {
                "turn_id": turn_id, "action_id": action_id, "status": "completed", "terminal": True,
                "ready_for_feedback": False, "answer": "private", "context": {"raw": "private"},
            }

    client = _client(tmp_path, monkeypatch)
    workflow = _Workflow()
    monkeypatch.setattr(tasks, "_world_action_callbacks", lambda _request, _container: (
        workflow.overview, workflow.action_status,
    ))

    listed = client.get("/api/rebuild/tasks?project_id=project-a")
    world = next(item for item in listed.json()["items"] if item["title"] == "整理项目资料")
    detail = client.get(f"/api/rebuild/tasks/{world['task_ref']}?project_id=project-a")

    assert listed.status_code == detail.status_code == 200
    assert world["status"] == "completed"
    assert world["attention"]["kind"] == "outputs_unavailable"
    assert detail.json()["detail"]["outputs"] == []
    rendered = str({"list": listed.json(), "detail": detail.json()}).lower()
    for forbidden in ("world-action", "world-turn", "private", "answer", "context", "raw"):
        assert forbidden not in rendered


def test_task_world_action_callbacks_keep_cold_task_lists_out_of_runtime_composition() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ai_turn_effect_store=None)))

    assert tasks._world_action_callbacks(request, SimpleNamespace(root_dir="unused")) == (None, None)


def test_new_transform_task_opens_pending_review_then_published_document(tmp_path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    with records.begin() as tx:
        tx.put("workspace_review_intents", "review-source-a", {
            "id": "review-source-a", "source_id": "source-a", "project_id": "project-a",
            "state": "pending", "job_id": "job-a",
        }, expected_revision=0)
        tx.commit()
    task = {"kind": "workbench_content_transform", "detail": {
        "source_ids": ["source-a"], "outputs": [{"kind": "document", "title": "已整理文档",
            "href": "#view=rebuild-library-overview&item_id=doc-a"}],
    }}
    pending = tasks._attach_workbench_reviews(task, root=tmp_path, project_id="project-a")
    assert pending["detail"]["outputs"][0]["kind"] == "review"
    assert pending["detail"]["review_items"][0]["href"].endswith("item_id=review-source-a")
    with records.begin() as tx:
        row = tx.read("workspace_review_intents", "review-source-a")
        tx.put("workspace_review_intents", row.object_id, {**row.payload, "state": "confirmed"},
               expected_revision=row.revision)
        tx.commit()
    task["detail"]["outputs"] = [{"kind": "document", "href": "#view=rebuild-library-overview&item_id=doc-a"}]
    confirmed = tasks._attach_workbench_reviews(task, root=tmp_path, project_id="project-a")
    assert confirmed["detail"]["outputs"][0]["kind"] == "document"
