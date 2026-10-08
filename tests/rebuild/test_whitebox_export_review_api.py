from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.product_core import ImportExternalAgentProposal
from core.storage_provider import JsonObjectStore


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _store(tmp_path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _seed_memory_data(store: JsonObjectStore) -> None:
    store.write(
        "memory_atoms",
        "atom-whitebox-1",
        {
            "schema_version": "1.0.0",
            "id": "atom-whitebox-1",
            "layer": "atom",
            "title": "白盒导出测试原子",
            "summary": "用于验证白盒导出链路的测试原子记忆。",
            "source_refs": ["source-whitebox-1"],
            "status": "published",
            "published_by": "user",
            "created_at": "2026-07-03T20:00:00+08:00",
        },
        expected_revision=None,
    )
    store.write(
        "project_skills",
        "project-skill-default",
        {
            "schema_version": "1.0.0",
            "id": "project-skill-default",
            "project_id": "default",
            "name": "默认项目技能",
            "markdown": "# 默认项目技能\n\n输出结构和阅读要求。",
            "structured": {"output_structure": "answer_manual"},
            "revision": 1,
            "status": "active",
            "created_at": "2026-07-03T20:00:00+08:00",
            "updated_at": "2026-07-03T20:00:00+08:00",
        },
        expected_revision=None,
    )


def _seed_review_draft(store: JsonObjectStore, *, draft_id: str = "review-draft-doc-1") -> str:
    proposal = {
        "proposal_type": "document_revision_proposal",
        "draft_type": "document_revision",
        "project_id": "default",
        "target_id": "document-whitebox-notes",
        "summary": "外部 Agent 建议更新白盒笔记文档",
        "requires_user_review": True,
        "suggested_changes": [
            {
                "op": "replace",
                "path": "/markdown",
                "value": "## 更新内容\n白盒导出流程已验证。",
            },
        ],
        "proposed_content": {
            "document_id": "document-whitebox-notes",
            "revision": 2,
            "title": "白盒笔记文档（更新）",
            "markdown": "## 更新内容\n白盒导出流程已验证。",
        },
        "source_refs": [{"locator": "source:whitebox-1", "quote": "白盒导出流程"}],
        "evidence_refs": [{"locator": "memory_layers.json#atom-whitebox-1"}],
    }
    result = ImportExternalAgentProposal(store, namespace_id="default").execute(
        proposal=proposal,
        project_id="default",
    )
    return result.draft_ids[0]


def test_whitebox_export_route_returns_files_and_manifest(tmp_path) -> None:
    client = _client(tmp_path)
    _seed_memory_data(_store(tmp_path))

    response = client.post("/api/rebuild/whitebox-memory/export", json={"project_id": "default"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ready", "created"}
    assert payload["export_id"]
    assert payload["memory_publication_state"] == "not_published"
    assert isinstance(payload["files"], list)
    assert len(payload["files"]) > 0
    logical_paths = {file["logical_path"] for file in payload["files"]}
    assert "manifest.json" in logical_paths
    encoded = str(payload)
    import re

    assert not re.search(r"sk-[A-Za-z0-9_-]{8,}", encoded), "no API key material in export response"
    assert not re.search(r"[A-Za-z]:\\\\", encoded), "no Windows absolute path in export response"


def test_whitebox_export_files_route_writes_to_local_dir(tmp_path) -> None:
    client = _client(tmp_path)
    _seed_memory_data(_store(tmp_path))

    export_response = client.post("/api/rebuild/whitebox-memory/export", json={"project_id": "default"})
    export_id = export_response.json()["export_id"]

    write_response = client.post(
        f"/api/rebuild/whitebox-memory/exports/{export_id}/files",
        json={},
    )

    assert write_response.status_code == 200
    payload = write_response.json()
    assert payload["status"] == "written"
    assert payload["export_id"] == export_id
    assert payload["file_count"] > 0
    assert isinstance(payload["written_paths"], list)
    assert payload["memory_publication_state"] == "not_published"


def test_whitebox_export_open_folder_route_requires_existing_export(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/rebuild/whitebox-memory/exports/nonexistent-export/open-folder",
        json={},
    )

    assert response.status_code == 404
    payload = response.json()
    assert payload["detail"] == "whitebox memory export folder rejected"
    assert payload["memory_publication_state"] == "not_published"


def test_external_agent_review_drafts_list_returns_seeded_drafts(tmp_path) -> None:
    client = _client(tmp_path)
    store = _store(tmp_path)
    _seed_memory_data(store)
    draft_id = _seed_review_draft(store)

    response = client.get("/api/rebuild/external-agent/review-drafts")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["count"] >= 1
    assert payload["read_only"] is True
    assert payload["memory_publication_state"] == "not_published"
    draft_ids = {item["id"] for item in payload["items"]}
    assert draft_id in draft_ids
    for item in payload["items"]:
        assert item["read_only"] is True
        assert "apply_without_user_confirmation" in item["forbidden_operations"]
        assert "direct_long_term_memory_write" in item["forbidden_operations"]


def test_external_agent_review_draft_preview_returns_draft_payload(tmp_path) -> None:
    client = _client(tmp_path)
    store = _store(tmp_path)
    _seed_memory_data(store)
    draft_id = _seed_review_draft(store)

    response = client.get(f"/api/rebuild/external-agent/review-drafts/{draft_id}/preview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    encoded = str(payload).lower()
    assert "sk-" not in encoded
    assert "api_key=" not in encoded


def test_external_agent_review_draft_apply_rejects_missing_confirm(tmp_path) -> None:
    client = _client(tmp_path)
    store = _store(tmp_path)
    _seed_memory_data(store)
    draft_id = _seed_review_draft(store)

    response = client.post(
        f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
        json={"expected_revision": 2},
    )

    assert response.status_code == 400
    payload = response.json()
    assert "confirm" in payload["reason"].lower()


def test_external_agent_review_draft_apply_rejects_missing_revision(tmp_path) -> None:
    client = _client(tmp_path)
    store = _store(tmp_path)
    _seed_memory_data(store)
    draft_id = _seed_review_draft(store)

    response = client.post(
        f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
        json={"confirm": True},
    )

    assert response.status_code == 400
    payload = response.json()
    assert "expected_revision" in payload["reason"]


def test_external_agent_review_draft_apply_returns_not_published_for_unknown_draft(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/rebuild/external-agent/review-drafts/nonexistent-draft/apply",
        json={"confirm": True, "expected_revision": 1},
    )

    assert response.status_code == 404
    payload = response.json()
    assert payload["detail"] == "external agent review draft not found"
