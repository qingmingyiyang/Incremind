from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.external_apply_saga import ExternalDocumentApplyError, ExternalDocumentApplySagaService
from core.document_engine import DocumentDraft, ObjectStoreDocumentRepository
from core.storage_provider import JsonObjectStore, SQLiteExternalApplySagaStore, SQLiteStructuredRecordStore


def _components(tmp_path: Path):
    drafts = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    documents = ObjectStoreDocumentRepository(drafts)
    operations = SQLiteExternalApplySagaStore(
        SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    )
    return drafts, documents, operations


def _seed(tmp_path: Path):
    drafts, documents, operations = _components(tmp_path)
    document = documents.create(
        DocumentDraft(
            title="Saga service target",
            document_type="notes",
            markdown="Original saga content.",
            project_id="default",
            source_refs=({"source_id": "source-saga-service", "locator": "char:0-22"},),
        )
    )
    draft_id = "draft-saga-service-001"
    drafts.write(
        "external_agent_review_drafts",
        draft_id,
        {
            "schema_version": "1.0.0",
            "id": draft_id,
            "draft_type": "document_revision",
            "status": "pending_review",
            "project_id": "default",
            "target_id": document["id"],
            "proposed_content": "Reviewed saga content.",
            "source_refs": [{"source_id": "source-saga-service", "locator": "char:0-22"}],
            "review": {"state": "pending_review", "requires_user_confirmation": True},
            "application": {"state": "not_applied"},
        },
        expected_revision=0,
    )
    return drafts, documents, operations, draft_id, str(document["id"])


def _service(drafts, documents, operations):
    return ExternalDocumentApplySagaService(
        documents=documents,
        drafts=drafts,
        operations=operations,
        now=lambda: "2026-07-11T12:00:00+00:00",
    )


def test_service_finalizes_document_draft_and_operation_once(tmp_path: Path) -> None:
    drafts, documents, operations, draft_id, document_id = _seed(tmp_path)
    service = _service(drafts, documents, operations)

    result = service.apply(draft_id, expected_revision=1)
    replay = service.apply(draft_id, expected_revision=1)

    assert result.state == "finalized"
    assert result.document_revision == 2
    assert replay == result
    assert documents.read(document_id)["revision"] == 2
    assert documents.markdown(document_id) == "Reviewed saga content."
    assert len(documents.revisions(document_id)) == 2
    draft = drafts.read("external_agent_review_drafts", draft_id)
    assert draft["status"] == "applied"
    assert draft["application"]["operation_id"] == draft_id
    assert operations.get(draft_id).state == "finalized"


def test_service_recovers_when_operation_mark_fails_after_document_commit(tmp_path: Path, monkeypatch) -> None:
    drafts, documents, operations, draft_id, document_id = _seed(tmp_path)
    original_mark = SQLiteExternalApplySagaStore.mark_document_applied

    with monkeypatch.context() as patch:
        patch.setattr(
            SQLiteExternalApplySagaStore,
            "mark_document_applied",
            lambda self, *args, **kwargs: (_ for _ in ()).throw(OSError("injected operation mark failure")),
        )
        with pytest.raises(OSError, match="operation mark failure"):
            _service(drafts, documents, operations).apply(draft_id, expected_revision=1)

    assert documents.read(document_id)["revision"] == 2
    assert operations.get(draft_id).state == "prepared"
    assert drafts.read("external_agent_review_drafts", draft_id)["status"] == "pending_review"

    result = _service(drafts, documents, operations).apply(draft_id, expected_revision=1)

    assert result.state == "finalized"
    assert len(documents.revisions(document_id)) == 2
    assert original_mark is not None


def test_service_recovers_when_draft_finalize_fails_after_operation_mark(tmp_path: Path, monkeypatch) -> None:
    drafts, documents, operations, draft_id, document_id = _seed(tmp_path)
    original_write = JsonObjectStore.write

    def fail_finalize(self, collection, object_id, payload, expected_revision):
        if collection == "external_agent_review_drafts" and object_id == draft_id and payload.get("status") == "applied":
            raise OSError("injected draft finalize failure")
        return original_write(self, collection, object_id, payload, expected_revision)

    with monkeypatch.context() as patch:
        patch.setattr(JsonObjectStore, "write", fail_finalize)
        with pytest.raises(OSError, match="draft finalize failure"):
            _service(drafts, documents, operations).apply(draft_id, expected_revision=1)

    assert operations.get(draft_id).state == "document_applied"
    assert documents.read(document_id)["revision"] == 2
    assert drafts.read("external_agent_review_drafts", draft_id)["status"] == "pending_review"

    result = _service(drafts, documents, operations).apply(draft_id, expected_revision=1)

    assert result.state == "finalized"
    assert len(documents.revisions(document_id)) == 2
    assert drafts.read("external_agent_review_drafts", draft_id)["status"] == "applied"


def test_service_rejects_payload_drift_after_document_half_commit(tmp_path: Path, monkeypatch) -> None:
    drafts, documents, operations, draft_id, document_id = _seed(tmp_path)

    with monkeypatch.context() as patch:
        patch.setattr(
            SQLiteExternalApplySagaStore,
            "mark_document_applied",
            lambda self, *args, **kwargs: (_ for _ in ()).throw(OSError("injected operation mark failure")),
        )
        with pytest.raises(OSError):
            _service(drafts, documents, operations).apply(draft_id, expected_revision=1)

    drifted = dict(drafts.read("external_agent_review_drafts", draft_id))
    drifted["proposed_content"] = "Drifted content must not be accepted."
    drafts.write("external_agent_review_drafts", draft_id, drifted, expected_revision=None)

    with pytest.raises(ExternalDocumentApplyError, match="evidence drifted"):
        _service(drafts, documents, operations).apply(draft_id, expected_revision=1)

    assert documents.read(document_id)["revision"] == 2
    assert len(documents.revisions(document_id)) == 2
    assert operations.get(draft_id).state == "prepared"
