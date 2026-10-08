from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.document_engine import DocumentDraft, ObjectStoreDocumentRepository
from core.storage_provider import JsonObjectStore


def _store(root):
    return JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library")


def _source(source_id: str, *, project: str = "project-alpha", video: bool = False,
            tags: tuple[str, ...] = (), state: str = "captured") -> dict[str, object]:
    return {
        "id": source_id,
        "project_id": project,
        "title": f"Entry {source_id}",
        "media_type": "text/uri-list" if video else "text/plain",
        "processing_state": state,
        "metadata": {
            "content_kind": "video" if video else "text",
            "manual_tags": list(tags),
            "content_read": {
                "status": "completed",
                "content_read": True,
                "read_ref": f"crp://default/source-content-reads/read-{source_id}.json",
                "preview": "ordinary preview",
            },
        },
    }


def _write_source(store, source_id: str, **kwargs):
    store.write("sources", source_id, _source(source_id, **kwargs), expected_revision=None)
    store.write("source_content_reads", f"read-{source_id}", {
        "id": f"read-{source_id}", "source_id": source_id,
        "status": "completed", "text": "needle appears only in the full body",
    }, expected_revision=None)


def _search(client, **params):
    return client.get("/api/rebuild/library/search", params={"scope": "overview", "q": "needle", **params})


def test_overview_search_counts_before_paging_and_filters_after_thirty(tmp_path):
    store = _store(tmp_path)
    for index in range(47):
        _write_source(store, f"source-{index:02d}", video=index >= 40,
                      tags=("featured",) if index >= 40 else ())
    _write_source(store, "other-project", project="project-beta")
    store.write("sources", "unrelated", _source("unrelated"), expected_revision=None)

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        first = _search(client, project_id="project-alpha", offset=0, limit=30)
        second = _search(client, project_id="project-alpha", offset=30, limit=30)
        video = _search(client, project_id="project-alpha", filter_id="video", limit=5)
        tag = _search(client, project_id="project-alpha", tag="FEATURED", offset=5, limit=5)
        beta = _search(client, project_id="project-beta")
        absent = _search(client, q="not-in-any-text")
    assert first.status_code == second.status_code == video.status_code == tag.status_code == 200
    assert first.json()["total"] == second.json()["total"] == 47
    assert len(first.json()["items"]) == 30 and first.json()["has_more"] is True
    assert len(second.json()["items"]) == 17 and second.json()["has_more"] is False
    assert {item["item_id"] for item in first.json()["items"]}.isdisjoint(
        {item["item_id"] for item in second.json()["items"]}
    )
    assert video.json()["total"] == 7 and video.json()["has_more"] is True
    assert all(item["source_content_kind"] == "video" for item in video.json()["items"])
    assert tag.json()["total"] == 7 and len(tag.json()["items"]) == 2
    assert beta.json()["total"] == 1
    assert absent.json()["total"] == 0 and absent.json()["items"] == []


def test_overview_search_uses_visibility_and_type_identity(tmp_path):
    store = _store(tmp_path)
    _write_source(store, "shared")
    _write_source(store, "deleted")
    deleted = store.read("sources", "deleted")
    store.write("sources", "deleted", {**deleted, "library_lifecycle": {"status": "deleted"}},
                expected_revision=store.revision("sources", "deleted"))
    _write_source(store, "hidden")
    hidden = store.read("sources", "hidden")
    store.write("sources", "hidden", {**hidden, "identity_method": "workspace_confirmation"},
                expected_revision=store.revision("sources", "hidden"))
    _write_source(store, "stale-read")
    stale = store.read("sources", "stale-read")
    stale_metadata = dict(stale["metadata"])
    stale_metadata["content_read"] = {**stale_metadata["content_read"], "status": "not_started", "content_read": False}
    store.write("sources", "stale-read", {**stale, "metadata": stale_metadata},
                expected_revision=store.revision("sources", "stale-read"))
    store.write("memory_candidates", "shared", {
        "id": "shared", "project_id": "project-alpha", "title": "needle candidate",
        "status": "pending_review", "import_batch_id": "batch-1",
    }, expected_revision=None)
    store.write("external_agent_review_drafts", "draft-1", {
        "id": "draft-1", "project_id": "project-alpha", "title": "needle draft",
        "status": "pending_review",
    }, expected_revision=None)
    store.write("documents", "document-1", {
        "id": "document-1", "project_id": "project-alpha", "title": "Ordinary document",
        "status": "draft", "content": "needle inside document body",
        "type": "note", "revision": 1,
    }, expected_revision=None)
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        response = _search(client, project_id="project-alpha")
        batch = _search(client, project_id="project-alpha", import_batch_id="batch-1")
        pending = _search(client, project_id="project-alpha", filter_id="pending_memory")
        invalid = _search(client, filter_id="bogus")
    assert response.status_code == 200
    identities = {(item["item_type"], item["item_id"]) for item in response.json()["items"]}
    assert ("source", "shared") in identities and ("memory_candidate", "shared") in identities
    assert ("document", "document-1") in identities
    assert ("source", "deleted") not in identities and ("source", "hidden") not in identities
    assert ("source", "stale-read") not in identities
    assert batch.json()["total"] == pending.json()["total"] == 1
    assert batch.json()["items"][0]["candidate_revision"] is not None
    document = next(item for item in response.json()["items"] if item["item_type"] == "document")
    assert document["document_revision"] is not None and document["document_type"] == "note"
    assert invalid.status_code == 400


def test_overview_search_reads_current_document_blocks_after_user_edit(tmp_path):
    store = _store(tmp_path)
    store.write("sources", "document-source", _source("document-source"), expected_revision=None)
    documents = ObjectStoreDocumentRepository(store)
    created = documents.create(DocumentDraft(
        title="Ordinary document", document_type="note",
        markdown="first-body-needle", project_id="project-alpha",
        source_refs=({"source_id": "document-source", "locator": "source://document-source"},),
    ))
    documents.save_user_edit(created["id"], markdown="edited-body-needle", expected_revision=1,
                             reason="search current text")
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        current = _search(client, q="edited-body-needle", project_id="project-alpha")
        old = _search(client, q="first-body-needle", project_id="project-alpha")
        other = _search(client, q="edited-body-needle", project_id="project-beta")
    assert current.status_code == 200
    assert [(item["item_type"], item["item_id"]) for item in current.json()["items"]] == [
        ("document", created["id"]),
    ]
    assert old.json()["total"] == other.json()["total"] == 0


def test_overview_search_preserves_legacy_recall_contract_and_rejects_bad_page(tmp_path):
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        legacy = client.get("/api/rebuild/library/search", params={"q": "needle"})
        bad = _search(client, offset="-1")
    assert legacy.status_code == 200 and "hits" in legacy.json() and "items" not in legacy.json()
    assert bad.status_code == 400


def test_overview_search_reads_only_linked_completed_media_full_text(tmp_path):
    store = _store(tmp_path)
    for source_id, output_id, output_source, status, keyword in (
        ("audio-full", "output-full", "audio-full", "completed", "deepneedle"),
        ("audio-foreign", "output-foreign", "different-source", "completed", "foreignneedle"),
        ("audio-failed", "output-failed", "audio-failed", "failed", "failedneedle"),
    ):
        source = _source(source_id)
        source["media_type"] = "audio/mpeg"
        source["metadata"] = {
            "audio_transcription": {
                "status": "completed", "asr_state": "completed",
                "transcript_output_id": output_id,
                "transcript_output_ref": f"crp://default/media-processing-outputs/{output_id}.json",
                "transcript_preview": "intro only",
            },
        }
        store.write("sources", source_id, source, expected_revision=None)
        store.write("media_processing_outputs", output_id, {
            "id": output_id, "source_id": output_source, "status": status,
            "output_kind": "transcript", "preview": "intro only",
            "text": "intro " + "x" * 700 + keyword,
        }, expected_revision=None)

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        intro = _search(client, q="intro", project_id="project-alpha")
        deep = _search(client, q="deepneedle", project_id="project-alpha")
        foreign = _search(client, q="foreignneedle", project_id="project-alpha")
        failed = _search(client, q="failedneedle", project_id="project-alpha")
    assert intro.status_code == deep.status_code == foreign.status_code == failed.status_code == 200
    assert intro.json()["total"] == 3
    assert [(item["item_type"], item["item_id"]) for item in deep.json()["items"]] == [
        ("source", "audio-full")
    ]
    assert foreign.json()["total"] == failed.json()["total"] == 0
