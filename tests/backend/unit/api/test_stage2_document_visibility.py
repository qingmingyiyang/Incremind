from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from backend.api.app import create_app
from backend.api.library_overview_runtime import _VisibleDocumentRepository, _VisibleLibraryReader
from backend.api.routes.product import (
    document_visibility as product_document_visibility,
    repositories as product_repositories,
)
from backend.memory_app.document_visibility import LegacyDocumentVisibility
from core.document_engine import DocumentDraft, SQLiteDocumentRepository
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore
from fastapi.testclient import TestClient


def _document(repository: SQLiteDocumentRepository, kind: str, locator: str):
    return repository.create(DocumentDraft(
        title=kind + locator,
        document_type=kind,
        markdown="body",
        source_refs=({"source_id": "source", "locator": locator},),
        project_id="project-a",
    ))


def test_shared_legacy_document_view_requires_confirmation_or_completed_task(tmp_path: Path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / "structured-records.sqlite3")
    documents = SQLiteDocumentRepository(records)
    legacy = _document(documents, "summary", "source://legacy")
    confirmed = _document(documents, "workspace-item", "workspace://confirmed")
    pending = _document(documents, "workspace-item", "workspace://pending")
    completed = _document(documents, "agent-result", "task://completed")
    ready = _document(documents, "agent-result", "task://ready")
    internal = _document(documents, "restructure-internal", "task://internal")
    with records.begin() as tx:
        tx.put("workspace_items", "confirmed", {"project_id": "project-a", "document_id": confirmed["id"],
            "status": "confirmed"}, expected_revision=0)
        tx.put("workspace_items", "pending", {"project_id": "project-a", "document_id": pending["id"],
            "status": "ready"}, expected_revision=0)
        tx.put("recognition_tasks", "completed", {"project_id": "project-a", "document_id": completed["id"],
            "kind": "context", "state": "completed"}, expected_revision=0)
        tx.put("recognition_tasks", "ready", {"project_id": "project-a", "document_id": ready["id"],
            "kind": "context", "state": "result_ready"}, expected_revision=0)
        tx.put("recognition_tasks", "internal", {"project_id": "project-a", "document_id": internal["id"],
            "kind": "restructure", "state": "completed"}, expected_revision=0)
        tx.commit()

    visible = {item["id"] for item in _VisibleDocumentRepository(documents).list()}
    assert visible == {legacy["id"], confirmed["id"], completed["id"]}
    visibility = LegacyDocumentVisibility.from_repository(documents)
    assert not visibility.allows({**confirmed, "project_id": "project-b"})


def test_pending_workspace_source_is_hidden_until_operation_commits(tmp_path: Path) -> None:
    class Reader:
        def sources(self):
            return ({"id": "legacy"},
                    {"id": "new", "identity_method": "workspace_confirmation",
                     "confirmation_operation_id": "confirm-item"})

    reader = _VisibleLibraryReader(Reader(), tmp_path)
    assert [item["id"] for item in reader.sources()] == ["legacy"]
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    with records.begin() as tx:
        tx.put("workspace_confirmation_operations", "confirm-item", {"state": "pending"}, expected_revision=0)
        tx.commit()
    assert [item["id"] for item in reader.sources()] == ["legacy"]
    with records.begin() as tx:
        tx.put("workspace_confirmation_operations", "confirm-item", {"state": "committed"}, expected_revision=1)
        tx.commit()
    assert [item["id"] for item in reader.sources()] == ["legacy", "new"]


def test_confirmed_video_link_recovers_media_semantics_without_rewriting_source(tmp_path: Path) -> None:
    source = {
        "id": "source-workspace-video", "identity_method": "workspace_confirmation",
        "confirmation_operation_id": "confirm-workspace-video", "workspace_item_id": "workspace-video",
        "project_id": "project-a", "media_type": "text/uri-list", "metadata": {"input_kind": "link"},
    }

    class Reader:
        def sources(self):
            return (source,)

    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    with records.begin() as tx:
        tx.put("workspace_confirmation_operations", "confirm-workspace-video", {"state": "committed"}, expected_revision=0)
        tx.put("workspace_items", "workspace-video", {
            "status": "confirmed", "project_id": "project-a", "content_kind": "video", "platform": "bilibili",
        }, expected_revision=0)
        tx.commit()
    enriched = _VisibleLibraryReader(Reader(), tmp_path).sources()[0]
    assert enriched["media_type"] == "text/uri-list"
    assert enriched["metadata"]["content_kind"] == "video"
    assert enriched["metadata"]["platform"] == "bilibili"
    assert source["metadata"] == {"input_kind": "link"}


def test_new_auto_intake_document_and_source_wait_for_review(tmp_path: Path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    documents = SQLiteDocumentRepository(records)
    document = documents.create(DocumentDraft(
        title="New auto material", document_type="source_document", markdown="Generated draft",
        source_refs=({"source_id": "source-new", "locator": "text:0:4"},),
        project_id="default"))

    class Reader:
        def sources(self):
            return ({"id": "source-new", "title": "New auto material"},)

    source_reader = _VisibleLibraryReader(Reader(), tmp_path)
    assert LegacyDocumentVisibility.from_repository(documents).allows(document)
    assert len(source_reader.sources()) == 1
    with records.begin() as tx:
        tx.put("workspace_review_intents", "review-source-new", {
            "id": "review-source-new", "source_id": "source-new", "project_id": "default",
            "job_id": "job-new", "state": "pending"}, expected_revision=0)
        tx.commit()
    assert not LegacyDocumentVisibility.from_repository(documents).allows(document)
    assert source_reader.sources() == ()
    with records.begin() as tx:
        tx.put("workspace_review_intents", "review-source-new", {
            "id": "review-source-new", "source_id": "source-new", "project_id": "default",
            "job_id": "job-new", "state": "confirmed"}, expected_revision=1)
        tx.commit()
    assert LegacyDocumentVisibility.from_repository(documents).allows(document)
    assert len(source_reader.sources()) == 1


def test_auto_intake_candidate_uses_unified_recognition_review(tmp_path: Path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    with records.begin() as tx:
        tx.put("workspace_review_intents", "review-source-a", {
            "id": "review-source-a", "source_id": "source-a", "project_id": "default",
            "state": "pending", "job_id": "job-a",
        }, expected_revision=0)
        tx.commit()
    candidate = {"source_refs": [{"source_id": "source-a", "locator": "text:0:4"}]}
    review_sources = product_document_visibility._workspace_review_sources(tmp_path)
    assert review_sources == {"source-a"}
    assert product_document_visibility._candidate_uses_source(candidate, review_sources)
    with records.begin() as tx:
        row = tx.read("workspace_review_intents", "review-source-a")
        tx.put("workspace_review_intents", row.object_id, {**row.payload, "state": "confirmed"},
               expected_revision=row.revision)
        tx.commit()
    assert product_document_visibility._candidate_uses_source(
        candidate, product_document_visibility._workspace_review_sources(tmp_path))


def test_all_old_candidate_review_routes_defer_to_unified_review(tmp_path: Path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library", namespace_id="default")
    store.write("memory_candidates", "candidate-old", {
        "id": "candidate-old", "status": "pending_review",
        "source_refs": [{"source_id": "source-reviewed"}],
    }, expected_revision=0)
    store.write("memory_candidate_conflicts", "conflict-old", {
        "id": "conflict-old", "candidate_id": "candidate-old",
        "incoming": {"source_id": "source-reviewed", "content": "old candidate"},
    }, expected_revision=0)
    with records.begin() as tx:
        tx.put("workspace_review_intents", "review-source-reviewed", {
            "id": "review-source-reviewed", "source_id": "source-reviewed",
            "project_id": "default", "state": "pending",
        }, expected_revision=0)
        tx.commit()
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        base = "/api/rebuild/memory-candidates/candidate-old"
        assert client.get(base + "/review").status_code == 404
        assert client.post(base + "/review", json={"action": "reject"}).status_code == 409
        assert client.post(base + "/auto-publication", json={}).status_code == 409
        assert client.post("/api/rebuild/sources/source-reviewed/memory-candidate", json={}).status_code == 409
        assert client.post("/api/rebuild/sources/source-reviewed/four-layer-candidates", json={}).status_code == 409
        assert client.post("/api/rebuild/memory/candidates/conflict", json={
            "conflict_id": "conflict-old", "resolution": "accept_incoming",
        }).status_code == 409
        with records.begin() as tx:
            row = tx.read("workspace_review_intents", "review-source-reviewed")
            tx.put("workspace_review_intents", row.object_id, {**row.payload, "state": "confirmed"},
                   expected_revision=row.revision)
            tx.commit()
        assert client.post(base + "/review", json={"action": "reject"}).status_code == 409


def test_old_document_routes_hide_unconfirmed_recognition_document(tmp_path: Path, monkeypatch) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    documents = SQLiteDocumentRepository(records)
    pending = _document(documents, "workspace-item", "workspace://pending")
    legacy = _document(documents, "summary", "source://legacy")
    monkeypatch.setattr(product_repositories, "_document_repository", lambda *_args: documents)

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        pending_id = pending["id"]
        for suffix in ("", "/revisions", "/html"):
            assert client.get(f"/api/rebuild/documents/{pending_id}{suffix}?project_id=project-a").status_code == 404
        assert client.put(f"/api/rebuild/documents/{pending_id}?project_id=project-a", json={
            "expected_revision": 1, "markdown": "changed",
        }).status_code == 404
        assert client.patch(f"/api/rebuild/documents/{pending_id}?project_id=project-a", json={
            "expected_revision": 1, "blocks": [{"text": "changed"}],
        }).status_code == 404
        assert client.post(f"/api/rebuild/documents/{pending_id}/archive?project_id=project-a", json={
            "expected_revision": 1,
        }).status_code == 404
        assert client.post(f"/api/rebuild/documents/{pending_id}/html-export?project_id=project-a").status_code == 404
        assert client.post("/api/rebuild/document-deliveries?project_id=project-a", json={
            "document_id": pending_id, "expected_document_revision": 1, "formats": ["html"],
        }).status_code == 404
        assert client.get(f"/api/rebuild/documents/{legacy['id']}?project_id=project-a").status_code == 200
        assert client.get(f"/api/rebuild/documents/{legacy['id']}").status_code == 404
