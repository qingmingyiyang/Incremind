from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.memory_app.workspace import install_workspace_routes
from backend.memory_app.v2.projects import assign_scene, scene_of
from backend.recognition import RecognitionService
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordUnitOfWork


class LocalModel:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, *, max_tokens, validate_current=None):
        self.calls += 1
        if validate_current:
            validate_current()
        source = messages[-1]["content"]
        return json.dumps({
            "title": "整理稿", "summary": "摘要", "topics": [],
            "facts": [{"text": "事实", "evidence": {
                "start": 0, "end": 2, "quote": source[:2],
            }}], "todos": [], "uncertainties": [], "people": [],
            "dates": [], "suggestions": [],
        }, ensure_ascii=False), {}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("CHRIPTMAS_APP_ROOT", str(tmp_path))
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "records.sqlite3")
    documents = SQLiteDocumentRepository(records, namespace_id="default")
    model = LocalModel()
    app = FastAPI()
    domains = install_workspace_routes(
        app, runtime_root=tmp_path, records=records, models=model,
        documents=documents, service=RecognitionService(records),
    )
    return SimpleNamespace(records=records, documents=documents, model=model,
                           domains=domains, http=TestClient(app))


def item(runtime):
    return asyncio.run(runtime.domains.intake.add_text({
        "project_id": "alpha", "text": "原文证据更多内容",
    }))


def confirm(runtime, item_id):
    from backend.memory_app.v2.auto_confirm import process_and_confirm
    return asyncio.run(process_and_confirm(runtime.domains, item_id, "alpha"))


def test_process_confirms_real_document_without_publishing_recognitions(runtime):
    from backend.memory_app.v2.layers import is_verified
    result = confirm(runtime, item(runtime)["id"])
    assert result["status"] == "confirmed" and result["document_id"]
    assert runtime.documents.read(result["document_id"])["project_id"] == "alpha"
    assert runtime.domains.confirmations.source_store.read("sources", result["source_id"])
    assert is_verified(runtime.records, runtime.documents, result["document_id"]) is False
    assert runtime.records.list("recognitions") == ()
    assert "processing_consent" not in result and "original_path" not in result


def test_v1_processing_still_stops_at_ready(runtime):
    created = item(runtime)
    result = runtime.http.post(f"/api/workspace/v1/items/{created['id']}/process",
                               json={"project_id": "alpha"})
    assert result.status_code == 200
    assert result.json()["status"] == "ready"
    assert result.json()["document_id"] is None
    assert runtime.documents.list() == ()


def test_ready_then_confirmed_reentry_is_idempotent_and_inherits_scene(runtime):
    created = item(runtime)
    assign_scene(runtime.records, "item", created["id"], "alpha", "阅读")
    asyncio.run(runtime.domains.intake.process(created["id"], {"project_id": "alpha"}))
    first = confirm(runtime, created["id"])
    doc_id = first["document_id"]
    assert scene_of(runtime.records, "document", doc_id) == {"project_id": "alpha", "scene": "阅读"}
    original_item = runtime.records.read("workspace_items", created["id"])
    original_doc = runtime.documents.read(doc_id)
    original_scene = runtime.records.read("v2_scene_assignments_document", doc_id)
    assert confirm(runtime, created["id"]) == first
    assert runtime.model.calls == 1
    assert runtime.records.read("workspace_items", created["id"]) == original_item
    assert runtime.documents.read(doc_id) == original_doc
    assert runtime.records.read("v2_scene_assignments_document", doc_id) == original_scene
    assert len(runtime.documents.list()) == 1


def test_processing_is_returned_without_starting_second_model(runtime):
    created = item(runtime)
    row = runtime.domains.items.item_for(created["id"], "alpha")
    runtime.domains.items.processing_lease.claim(
        created["id"], "alpha", row.revision, "run-one", None, {},
    )
    result = confirm(runtime, created["id"])
    assert result["status"] == "processing"
    assert runtime.model.calls == 0
    assert "processing_run_id" not in result


def test_confirm_cas_conflict_returns_current_ready_row(runtime, monkeypatch, caplog):
    created = item(runtime)
    asyncio.run(runtime.domains.intake.process(created["id"], {"project_id": "alpha"}))
    original = runtime.domains.review.confirm

    async def concurrent_edit(item_id, body):
        runtime.domains.items.update(item_id, "alpha", {"ready"}, title="新标题")
        return await original(item_id, body)

    monkeypatch.setattr(runtime.domains.review, "confirm", concurrent_edit)
    result = confirm(runtime, created["id"])
    assert result["status"] == "ready" and result["title"] == "新标题"
    assert runtime.documents.list() == ()
    assert "stage=confirm" in caplog.text and "HTTPException" in caplog.text


def test_real_confirmation_io_failure_recovers_frozen_operation(runtime, monkeypatch, caplog):
    created = item(runtime)
    store = runtime.domains.confirmations.source_store
    original = type(store).write

    def broken_write(*args, **kwargs):
        raise OSError("private-source-body-do-not-log")

    monkeypatch.setattr(type(store), "write", broken_write)
    failed = confirm(runtime, created["id"])
    assert failed["status"] == "confirming" and failed["document_id"] is None
    assert runtime.documents.list() == ()
    assert "OSError" in caplog.text and "private-source-body-do-not-log" not in caplog.text
    monkeypatch.setattr(type(store), "write", original)
    recovered = confirm(runtime, created["id"])
    assert recovered["status"] == "confirmed" and recovered["document_id"]
    assert recovered["reviewed_revision"] == failed["reviewed_revision"]
    assert runtime.model.calls == 1 and len(runtime.documents.list()) == 1
    assert confirm(runtime, created["id"]) == recovered


def test_scene_failure_after_commit_is_repaired_without_reconfirmation(runtime, monkeypatch, caplog):
    created = item(runtime)
    assign_scene(runtime.records, "item", created["id"], "alpha", "阅读")
    original = SQLiteStructuredRecordUnitOfWork.put

    def broken_scene(self, collection, *args, **kwargs):
        if collection == "v2_scene_assignments_document":
            raise OSError("private-scene-details-do-not-log")
        return original(self, collection, *args, **kwargs)

    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", broken_scene)
    result = confirm(runtime, created["id"])
    assert result["status"] == "confirmed"
    doc_id = result["document_id"]
    assert scene_of(runtime.records, "document", doc_id) is None
    before = runtime.documents.read(doc_id)
    assert "stage=scene" in caplog.text and "private-scene-details-do-not-log" not in caplog.text
    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", original)
    assert confirm(runtime, created["id"]) == result
    assert scene_of(runtime.records, "document", doc_id)["scene"] == "阅读"
    assert runtime.documents.read(doc_id) == before
    assert runtime.model.calls == 1


def test_reentry_preserves_manual_document_scene(runtime):
    created = item(runtime)
    assign_scene(runtime.records, "item", created["id"], "alpha", "阅读")
    result = confirm(runtime, created["id"])
    assigned = assign_scene(runtime.records, "document", result["document_id"], "alpha", "手动")
    confirm(runtime, created["id"])
    assert runtime.records.read("v2_scene_assignments_document", result["document_id"]) == assigned


def test_scene_inheritance_rechecks_assignment_in_write_transaction(runtime, monkeypatch):
    import backend.memory_app.v2.auto_confirm as orchestration
    created = item(runtime)
    result = confirm(runtime, created["id"])
    doc_id = result["document_id"]
    assign_scene(runtime.records, "item", created["id"], "alpha", "阅读")
    def concurrent_assignment(records, object_type, object_id, project_id, scene, **kwargs):
        assign_scene(runtime.records, "document", doc_id, "alpha", "手动")
        return assign_scene(records, object_type, object_id, project_id, scene, **kwargs)

    monkeypatch.setattr(orchestration, "assign_scene", concurrent_assignment)
    confirm(runtime, created["id"])
    assert scene_of(runtime.records, "document", doc_id)["scene"] == "手动"


def test_scene_helper_if_absent_preserves_existing_revision_and_default_replaces(runtime):
    first = assign_scene(runtime.records, "document", "doc-one", "alpha", "阅读", if_absent=True)
    assert first.revision == 1 and first.payload == {"project_id": "alpha", "scene": "阅读"}
    assert assign_scene(runtime.records, "document", "doc-one", "alpha", "手动", if_absent=True) == first
    changed = assign_scene(runtime.records, "document", "doc-one", "alpha", "手动")
    assert changed.revision == 2 and changed.payload["scene"] == "手动"


def test_scene_from_other_project_is_not_inherited(runtime):
    created = item(runtime)
    assign_scene(runtime.records, "item", created["id"], "beta", "阅读")
    result = confirm(runtime, created["id"])
    assert scene_of(runtime.records, "document", result["document_id"]) is None


def test_wrong_project_is_rejected_before_processing(runtime):
    from backend.memory_app.v2.auto_confirm import process_and_confirm
    created = item(runtime)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(process_and_confirm(runtime.domains, created["id"], "beta"))
    assert exc.value.status_code == 404
    assert runtime.model.calls == 0


def test_user_edit_in_any_historical_revision_verifies_document(runtime):
    from backend.memory_app.v2.layers import is_verified
    result = confirm(runtime, item(runtime)["id"])
    doc_id = result["document_id"]
    document = runtime.documents.read(doc_id)
    edited = runtime.documents.save_user_edit(doc_id, markdown="# 用户修改", expected_revision=document["revision"])
    assert is_verified(runtime.records, runtime.documents, doc_id) is True
    runtime.documents.archive(doc_id, expected_revision=edited["revision"])
    assert runtime.documents.revisions(doc_id)[-1]["operation"] != "user_edit"
    assert is_verified(runtime.records, runtime.documents, doc_id) is True


def test_manual_verification_is_sidecar_monotonic_and_idempotent(runtime):
    from backend.memory_app.v2.layers import is_verified, mark_verified
    result = confirm(runtime, item(runtime)["id"])
    doc_id = result["document_id"]
    before_doc = runtime.documents.read(doc_id)
    before_item = runtime.records.read("workspace_items", result["id"])
    first = mark_verified(runtime.records, doc_id, before_doc["revision"])
    assert first.payload == {"document_revision": before_doc["revision"]}
    assert is_verified(runtime.records, runtime.documents, doc_id) is True
    assert mark_verified(runtime.records, doc_id, before_doc["revision"]) == first
    assert runtime.documents.read(doc_id) == before_doc
    assert runtime.records.read("workspace_items", result["id"]) == before_item
    archived = runtime.documents.archive(doc_id, expected_revision=before_doc["revision"])
    assert is_verified(runtime.records, runtime.documents, doc_id) is True
    updated = mark_verified(runtime.records, doc_id, archived["revision"])
    assert updated.revision == first.revision + 1
    assert mark_verified(runtime.records, doc_id, before_doc["revision"]) == updated


def test_missing_document_and_future_marker_are_unverified(runtime):
    from backend.memory_app.v2.layers import is_verified
    assert is_verified(runtime.records, runtime.documents, "missing") is False
    result = confirm(runtime, item(runtime)["id"])
    doc_id = result["document_id"]
    with runtime.records.begin() as tx:
        tx.put("v2_verifications", doc_id, {"document_revision": 99}, expected_revision=0)
        tx.commit()
    assert is_verified(runtime.records, runtime.documents, doc_id) is False


def test_mark_rejects_missing_document(runtime):
    from backend.memory_app.v2.layers import mark_verified
    with pytest.raises(ValueError, match="document_not_found"):
        mark_verified(runtime.records, "missing", 1)
    assert runtime.records.read("v2_verifications", "missing") is None


def test_mark_rejects_future_revision(runtime):
    from backend.memory_app.v2.layers import mark_verified
    result = confirm(runtime, item(runtime)["id"])
    doc_id = result["document_id"]
    revision = runtime.documents.read(doc_id)["revision"]
    with pytest.raises(ValueError, match="document_revision_conflict"):
        mark_verified(runtime.records, doc_id, revision + 1)
    assert runtime.records.read("v2_verifications", doc_id) is None


def test_concurrent_processing_claim_runs_one_model(runtime, monkeypatch):
    from backend.memory_app.v2.auto_confirm import process_and_confirm
    created = item(runtime)
    started, release = threading.Event(), threading.Event()
    original = runtime.model.complete

    def slow_complete(*args, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.model, "complete", slow_complete)

    async def run():
        first = asyncio.create_task(process_and_confirm(runtime.domains, created["id"], "alpha"))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            second = await process_and_confirm(runtime.domains, created["id"], "alpha")
            assert second["status"] == "processing"
        finally:
            release.set()
        return await first

    result = asyncio.run(run())
    assert result["status"] == "confirmed"
    assert runtime.model.calls == 1 and len(runtime.documents.list()) == 1


def test_concurrent_manual_marks_create_one_sidecar_revision(runtime):
    from backend.memory_app.v2.layers import mark_verified
    result = confirm(runtime, item(runtime)["id"])
    doc_id = result["document_id"]
    revision = runtime.documents.read(doc_id)["revision"]
    barrier = threading.Barrier(2)

    def mark():
        barrier.wait(timeout=5)
        return mark_verified(runtime.records, doc_id, revision)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [future.result(timeout=5) for future in [pool.submit(mark), pool.submit(mark)]]
    assert first == second and first.revision == 1
    assert runtime.records.read("v2_verifications", doc_id) == first


@pytest.mark.parametrize("revision", [True, False, 0, -1, "1", 1.5])
def test_mark_rejects_nonpositive_or_noninteger_revision(runtime, revision):
    from backend.memory_app.v2.layers import mark_verified
    result = confirm(runtime, item(runtime)["id"])
    with pytest.raises(ValueError):
        mark_verified(runtime.records, result["document_id"], revision)
    assert runtime.records.read("v2_verifications", result["document_id"]) is None
