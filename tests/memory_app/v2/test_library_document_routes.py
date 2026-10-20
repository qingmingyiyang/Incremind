"""The v2 document read/write routes reuse the document repository rules."""
from types import SimpleNamespace

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.v2.library import install_library_routes
from backend.memory_app.source_egress import recognition_service
from core.document_engine import DocumentDraft, SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore

MARKDOWN = "# Synthetic\n\n## 摘要\nalpha\n\n## 正文\nbeta"


def make(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    documents = SQLiteDocumentRepository(records)
    service = recognition_service(records)
    app = FastAPI()
    install_library_routes(app, records=records, service=service, documents=documents,
                           workspace=SimpleNamespace(query=None), models=None)
    document = documents.create(DocumentDraft(
        title="Synthetic", document_type="note", markdown=MARKDOWN,
        source_refs=({"source_id": "source-1", "locator": "source_content_v1:0-10"},),
        project_id="alpha"))
    return TestClient(app), records, documents, document


def test_read_document_returns_markdown_and_revision(tmp_path):
    http, _, _, document = make(tmp_path)
    response = http.get(f"/api/v2/library/documents/{document['id']}", params={"project_id": "alpha"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["id"] == document["id"]
    assert data["markdown"] == MARKDOWN
    assert data["revision"] == 1


def test_edit_document_appends_a_user_revision(tmp_path):
    http, _, documents, document = make(tmp_path)
    response = http.patch(f"/api/v2/library/documents/{document['id']}", json={
        "project_id": "alpha", "expected_revision": 1,
        "markdown": MARKDOWN + " gamma",
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["revision"] == 2
    assert data["markdown"].endswith("gamma")
    assert documents.read(document["id"])["revision"] == 2
    assert any(row.get("operation") == "user_edit" for row in documents.revisions(document["id"]))


def test_edit_conflict_and_project_isolation(tmp_path):
    http, _, documents, document = make(tmp_path)
    conflict = http.patch(f"/api/v2/library/documents/{document['id']}", json={
        "project_id": "alpha", "expected_revision": 7, "markdown": MARKDOWN,
    })
    assert conflict.status_code == 409
    missing = http.get(f"/api/v2/library/documents/{document['id']}", params={"project_id": "beta"})
    assert missing.status_code == 404
    assert documents.read(document["id"])["revision"] == 1


@pytest.mark.parametrize("markdown", ["", " \t\r\n\u3000"], ids=["empty", "whitespace"])
def test_edit_rejects_blank_markdown_without_changing_saved_state(tmp_path, markdown):
    http, records, documents, document = make(tmp_path)
    before_document = documents.read(document["id"])
    before_markdown = documents.markdown(document["id"])
    before_revisions = documents.revisions(document["id"])
    before_sidecars = {
        collection: records.list(collection)
        for collection in ("v2_outcome_corrections", "v2_usage_document")
    }

    response = http.patch(f"/api/v2/library/documents/{document['id']}", json={
        "project_id": "alpha", "expected_revision": 1, "markdown": markdown,
    })

    assert response.status_code == 400, response.text
    assert response.json() == {"detail": "invalid_document_fields"}
    assert documents.read(document["id"]) == before_document
    assert documents.markdown(document["id"]) == before_markdown
    assert documents.revisions(document["id"]) == before_revisions
    assert {
        collection: records.list(collection) for collection in before_sidecars
    } == before_sidecars


def test_edit_preserves_surrounding_whitespace_in_nonblank_markdown(tmp_path):
    http, _, documents, document = make(tmp_path)
    markdown = " \t\n" + MARKDOWN + "\n\u3000 "
    response = http.patch(f"/api/v2/library/documents/{document['id']}", json={
        "project_id": "alpha", "expected_revision": 1, "markdown": markdown,
    })

    assert response.status_code == 200, response.text
    assert response.json()["markdown"] == markdown
    assert response.json()["revision"] == 2
    assert documents.read(document["id"])["revision"] == 2
    assert documents.markdown(document["id"]) == markdown
    assert documents.markdown(document["id"], revision=1) == MARKDOWN
    revisions = documents.revisions(document["id"])
    assert len(revisions) == 2
    assert any(row.get("operation") == "user_edit" and row["revision"] == 2 for row in revisions)
