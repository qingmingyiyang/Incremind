from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.document_engine import SQLiteDocumentRepository
from core.document_engine.ports import DocumentDraft
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore, SourceAssetRuntimeStore
from core.product_core.source_content_read import ReadSourceTextContent
from backend.memory_app.workspace_confirmation import WorkspaceConfirmation, COLLECTION


def _ready_item(item_id: str, project_id: str = "alpha") -> dict[str, object]:
    return {
        "id": item_id, "project_id": project_id, "input_kind": "text", "title": "Reviewed text",
        "source_text": "One verifiable sentence.", "status": "ready", "document_id": None,
        "created_at": "2026-09-24T00:00:00+00:00",
        "draft": {"title": "Reviewed text", "facts": [{"text": "One verifiable sentence.",
                 "evidence": {"start": 0, "end": 24, "quote": "One verifiable sentence."}}], "todos": []},
    }


def _setup(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    documents = SQLiteDocumentRepository(records, namespace_id="default")
    service = WorkspaceConfirmation(tmp_path, records, documents)
    public_sources = SourceAssetRuntimeStore(
        json_store=JsonObjectStore(tmp_path / ".rebuild-data", namespace_id="default"),
        sqlite_records=None, library_root=tmp_path / "library", authority_identity="json",
    )
    return records, documents, service, public_sources


def test_confirmation_publishes_one_source_document_and_item(tmp_path):
    records, documents, service, sources = _setup(tmp_path)
    item = _ready_item("workspace-one")
    with records.begin() as tx:
        tx.put("workspace_items", item["id"], item, expected_revision=0)
        tx.commit()
    first = service.confirm(item["id"], "alpha", 1,
                            lambda _draft: "# Reviewed text\n\nOne verifiable sentence.")
    second = service.confirm(item["id"], "alpha", 1, lambda _draft: "ignored repeat")
    assert first == second
    assert first["status"] == "confirmed"
    assert first["source_id"] == "source-workspace-one"
    assert sources.read("sources", first["source_id"])["workspace_item_id"] == item["id"]
    assert documents.read(first["document_id"])["project_id"] == "alpha"
    assert records.read(COLLECTION, "confirm-workspace-one").payload["state"] == "committed"
    assert len(documents.list()) == 1
    prior_revision = service.source_store.revision("sources", first["source_id"])
    read = ReadSourceTextContent(sources).execute(source_id=first["source_id"])
    assert read.content_read is True and read.read_ref is None
    assert service.source_store.revision("sources", first["source_id"]) == prior_revision


def test_confirmation_freezes_only_the_reviewed_draft_revision(tmp_path):
    records, documents, service, sources = _setup(tmp_path)
    item = _ready_item("workspace-revision")
    with records.begin() as tx:
        tx.put("workspace_items", item["id"], item, expected_revision=0)
        tx.commit()
    revised = {**item, "draft": {**item["draft"], "title": "New reviewed title"}}
    with records.begin() as tx:
        tx.put("workspace_items", item["id"], revised, expected_revision=1)
        tx.commit()
    with pytest.raises(ValueError, match="draft_revision_conflict"):
        service.confirm(item["id"], "alpha", 1, lambda draft: "# " + draft["title"])
    assert records.read(COLLECTION, "confirm-workspace-revision") is None
    assert documents.list() == ()
    result = service.confirm(item["id"], "alpha", 2, lambda draft: "# " + draft["title"])
    operation = records.read(COLLECTION, "confirm-workspace-revision").payload
    assert operation["reviewed_revision"] == 2
    assert operation["draft"]["title"] == "New reviewed title"
    assert operation["markdown"] == "# New reviewed title"
    assert documents.markdown(result["document_id"]) == "# New reviewed title"
    assert service.confirm(item["id"], "alpha", 2, lambda _draft: "ignored") == result
    with pytest.raises(ValueError, match="draft_revision_conflict"):
        service.confirm(item["id"], "alpha", 1, lambda _draft: "ignored")
    assert len(documents.list()) == 1


def test_default_workspace_api_confirms_exact_reviewed_revision(tmp_path):
    from backend.memory_app.workspace import install_workspace_routes
    from backend.recognition import RecognitionService
    from tests.memory_app.test_workspace import Model

    records, documents, service, sources = _setup(tmp_path)
    app = FastAPI()
    install_workspace_routes(app, runtime_root=tmp_path, records=records, models=Model(),
                             documents=documents, service=RecognitionService(records))
    http = TestClient(app)
    item = http.post("/api/workspace/v1/items/text", json={
        "project_id": "alpha", "text": "原文证据更多内容",
    }).json()
    path = f"/api/workspace/v1/items/{item['id']}"
    ready = http.post(path + "/process", json={"project_id": "alpha"}).json()
    draft = {**ready["draft"], "summary": "人工核对摘要"}
    saved = http.put(path + "/draft", json={
        "project_id": "alpha", "expected_revision": ready["revision"], **draft,
    }).json()
    stale = http.post(path + "/confirm", json={
        "project_id": "alpha", "expected_revision": ready["revision"],
    })
    assert stale.status_code == 409
    assert records.read(COLLECTION, "confirm-" + item["id"]) is None
    confirmed = http.post(path + "/confirm", json={
        "project_id": "alpha", "expected_revision": saved["revision"],
    })
    assert confirmed.status_code == 200
    result = confirmed.json()
    assert result["reviewed_revision"] == saved["revision"]
    assert documents.markdown(result["document_id"]).startswith(
        "# " + draft["title"] + "\n\n## 摘要\n\n人工核对摘要\n")
    assert sources.read("sources", result["source_id"])["project_id"] == "alpha"
    assert http.post(path + "/confirm", json={
        "project_id": "alpha", "expected_revision": saved["revision"],
    }).status_code == 200


def test_pending_source_is_hidden_and_recovered_after_payload_metadata_gap(tmp_path):
    records, documents, service, sources = _setup(tmp_path)
    item = _ready_item("workspace-two")
    with records.begin() as tx:
        tx.put("workspace_items", item["id"], item, expected_revision=0)
        tx.commit()
    # Freeze the same immutable intent, then emulate an interrupted JSON write.
    from backend.memory_app.workspace_confirmation import _source_payload, _refs
    operation_id = "confirm-workspace-two"
    source_id = "source-workspace-two"
    payload = _source_payload(item, operation_id, source_id)
    with records.begin() as tx:
        tx.put(COLLECTION, operation_id, {
            "id": operation_id, "state": "pending", "workspace_item_id": item["id"],
            "project_id": "alpha", "source_id": source_id, "source_payload": payload,
            "markdown": "# Reviewed text", "draft": item["draft"], "source_refs": _refs(item, source_id),
        }, expected_revision=0)
        tx.put("workspace_items", item["id"], {**item, "status": "confirming"}, expected_revision=1)
        tx.commit()
    service.source_store.write("sources", source_id, payload, expected_revision=0)
    meta = tmp_path / ".rebuild-data" / "objects" / "default" / "sources" / f"{source_id}.meta.json"
    meta.unlink()
    assert sources.read("sources", source_id) is None
    assert len(documents.list()) == 0
    assert service.recover_pending() == ()
    assert service.source_store.revision("sources", source_id) == 1
    assert sources.read("sources", source_id) is not None
    assert len(documents.list()) == 1


def test_video_link_confirmation_preserves_link_mime_and_video_semantics():
    from backend.memory_app.workspace_confirmation import _source_payload

    item = {**_ready_item("workspace-video"), "input_kind": "link",
            "url": "https://www.bilibili.com/video/BV1test",
            "content_kind": "video", "platform": "bilibili"}
    source = _source_payload(item, "confirm-workspace-video", "source-workspace-video")
    assert source["media_type"] == "text/uri-list"
    assert source["metadata"]["content_kind"] == "video"
    assert source["metadata"]["platform"] == "bilibili"


def test_historical_confirmed_item_backfills_source_without_changing_document(tmp_path):
    records, documents, service, sources = _setup(tmp_path)
    item = _ready_item("workspace-historical")
    document = documents.create_or_replay_generated(DocumentDraft(
        title="Historical", document_type=item["id"], markdown="# Historical\n\nOne verifiable sentence.",
        source_refs=({"source_id": item["id"], "locator": "workspace://" + item["id"]},),
        project_id="alpha"))
    item = {**item, "status": "confirmed", "document_id": document["id"]}
    with records.begin() as tx:
        tx.put("workspace_items", item["id"], item, expected_revision=0)
        tx.commit()
    before_document = documents.read(document["id"])
    before_markdown = documents.markdown(document["id"])
    first = service.backfill_confirmed_source(item["id"], "alpha")
    second = service.backfill_confirmed_source(item["id"], "alpha")
    assert first == second
    assert first["source_id"] == "source-workspace-historical"
    assert sources.read("sources", first["source_id"])["workspace_item_id"] == item["id"]
    assert documents.read(document["id"]) == before_document
    assert documents.markdown(document["id"]) == before_markdown


def test_interrupted_historical_source_backfill_recovers_without_new_document(tmp_path):
    from backend.memory_app.workspace_confirmation import _source_payload

    records, documents, service, sources = _setup(tmp_path)
    item = _ready_item("workspace-recovery")
    document = documents.create(DocumentDraft(
        title="Historical", document_type=item["id"], markdown="# Historical",
        source_refs=({"source_id": item["id"], "locator": "workspace://" + item["id"]},),
        project_id="alpha"))
    item = {**item, "status": "confirmed", "document_id": document["id"]}
    operation_id = "backfill-" + item["id"]
    source_id = "source-" + item["id"]
    payload = _source_payload(item, operation_id, source_id)
    with records.begin() as tx:
        tx.put("workspace_items", item["id"], item, expected_revision=0)
        tx.put(COLLECTION, operation_id, {"id": operation_id, "kind": "historical_source_backfill",
               "state": "pending", "workspace_item_id": item["id"], "project_id": "alpha",
               "document_id": document["id"], "source_id": source_id,
               "source_payload": payload}, expected_revision=0)
        tx.commit()
    service.source_store.write("sources", source_id, payload, expected_revision=0)
    assert sources.read("sources", source_id) is None
    assert service.recover_pending() == ()
    assert records.read("workspace_items", item["id"]).payload["source_id"] == source_id
    assert sources.read("sources", source_id) is not None
    assert len(documents.list()) == 1
