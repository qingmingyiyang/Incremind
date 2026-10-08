from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.document_engine import DocumentDraft, ObjectStoreDocumentRepository
from core.storage_provider import JsonObjectStore


def _document(root: Path, project_id: str | None, title: str) -> dict[str, object]:
    store = JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library")
    return dict(ObjectStoreDocumentRepository(store).create(DocumentDraft(
        title=title,
        document_type="summary",
        markdown=f"# {title}\n\n正文",
        source_refs=({"source_id": "source-1", "locator": "char:0-2", "quote": "正文"},),
        project_id=project_id,
    )))


def test_document_routes_reject_other_project_before_revision_or_content_is_read(tmp_path: Path) -> None:
    default = _document(tmp_path, None, "旧项目文档")
    alpha = _document(tmp_path, "alpha", "Alpha 文档")
    document_id = str(alpha["id"])
    path = f"/api/rebuild/documents/{document_id}"
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        assert client.get(path, params={"project_id": "alpha"}).status_code == 200
        assert client.get(path).status_code == 404
        assert client.get(path, params={"project_id": "beta"}).status_code == 404
        assert client.put(path, params={"project_id": "beta"}, json={
            "expected_revision": 1, "markdown": "越权改写",
        }).status_code == 404
        assert client.patch(path, params={"project_id": "beta"}, json={
            "expected_revision": 1, "blocks": [{"id": "block-1"}],
        }).status_code == 404
        assert client.post(f"{path}/archive", params={"project_id": "beta"}, json={
            "expected_revision": 1,
        }).status_code == 404
        assert client.post(f"{path}/restore", params={"project_id": "beta"}, json={
            "expected_revision": 1,
        }).status_code == 404
        assert client.post(f"{path}/template-memory-candidate", params={"project_id": "beta"}, json={
            "document_revision": 1,
        }).status_code == 404
        for suffix in ("/revisions", "/html"):
            assert client.get(path + suffix, params={"project_id": "beta"}).status_code == 404
        assert client.post(f"{path}/html-export", params={"project_id": "beta"}).status_code == 404
        assert client.get(f"/api/rebuild/documents/{default['id']}").status_code == 200
        assert client.get(f"/api/rebuild/documents/{default['id']}", params={"project_id": "alpha"}).status_code == 404
        current = client.get(path, params={"project_id": "alpha"}).json()
        assert current["revision"] == 1
        assert current["markdown"] == "# Alpha 文档\n\n正文"


def test_archives_deliveries_and_pdf_operations_keep_project_scope(tmp_path: Path) -> None:
    alpha = _document(tmp_path, "alpha", "可导出文档")
    beta = _document(tmp_path, "beta", "另一项目文档")
    alpha_id = str(alpha["id"])
    beta_id = str(beta["id"])
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        assert client.post(f"/api/rebuild/documents/{alpha_id}/archive", params={"project_id": "alpha"}, json={
            "expected_revision": 1,
        }).status_code == 200
        assert client.post(f"/api/rebuild/documents/{beta_id}/archive", params={"project_id": "beta"}, json={
            "expected_revision": 1,
        }).status_code == 200
        archived = client.get("/api/rebuild/documents-archived", params={"project_id": "alpha"}).json()["items"]
        assert [item["document_id"] for item in archived] == [alpha_id]
        assert client.get("/api/rebuild/documents-archived").json()["items"] == []
        delivery_path = "/api/rebuild/document-deliveries"
        payload = {"document_id": alpha_id, "expected_document_revision": 2, "formats": ["html"]}
        assert client.post(delivery_path, params={"project_id": "beta"}, json=payload).status_code == 404
        created = client.post(delivery_path, params={"project_id": "alpha"}, json=payload)
        assert created.status_code == 200
        delivery_id = created.json()["delivery_id"]
        artifact_path = f"{delivery_path}/{delivery_id}/artifacts/html"
        assert client.get(artifact_path, params={"project_id": "beta"}).status_code == 404
        assert client.get(artifact_path).status_code == 404
        assert client.get(artifact_path, params={"project_id": "alpha"}).status_code == 200
        pdf_path = f"{delivery_path}/{delivery_id}/pdf-operations"
        assert client.post(pdf_path, params={"project_id": "beta"}, json={
            "profile_id": "builtin.a4-document",
        }).status_code == 404
        prepared = client.post(pdf_path, params={"project_id": "alpha"}, json={
            "profile_id": "builtin.a4-document",
        })
        assert prepared.status_code == 200
        operation_path = f"/api/rebuild/document-pdf-operations/{prepared.json()['operation_id']}"
        assert client.get(operation_path, params={"project_id": "beta"}).status_code == 404
        assert client.get(operation_path + "/artifact", params={"project_id": "beta"}).status_code == 404
        assert client.get(operation_path, params={"project_id": "alpha"}).status_code == 200


def test_document_project_parameter_rejects_invalid_duplicates_and_conflict(tmp_path: Path) -> None:
    document = _document(tmp_path, "alpha", "项目参数测试")
    path = f"/api/rebuild/documents/{document['id']}"
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        for query in ("?project_id=", "?project_id=alpha&project_id=beta", "?project_id=../beta"):
            assert client.get(path + query).status_code == 400
        assert client.put(path + "?project_id=alpha", json={
            "project_id": "beta", "expected_revision": 1, "markdown": "冲突",
        }).status_code == 400
        assert client.post(path + "/html-export?project_id=alpha", json={
            "project_id": "beta",
        }).status_code == 400
        assert client.post(path + "/archive?project_id=alpha", json={
            "project_id": None, "expected_revision": 1,
        }).status_code == 400
