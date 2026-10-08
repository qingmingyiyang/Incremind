"""HTTP-level provenance coverage using only controlled repository ports.

This exercises the route serialization seam.  It deliberately does not run
admission, upload, DOCX parsing, or the real SQLite receipt reader.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.container import get_container
from backend.api.routes import rebuild
from backend.api.routes.product import (
    document_serialization as product_document_serialization,
    repositories as product_repositories,
)
from backend.api.task_reference_projection import task_ref_for_workbench_content_transform
from core.document_engine import DocumentExpectedRevisionError
from core.storage_provider import JsonObjectStore


_DOCUMENT_ID = "document-controlled-1"
_JOB_ID = "job-controlled-1"
_SOURCE_ID = "source-controlled-1"
_PROJECT_ID = "project-controlled"


class _ControlledDocuments:
    """Minimal in-memory document port; no content digest or admission path."""

    def __init__(self) -> None:
        self._current = {
            "id": _DOCUMENT_ID, "project_id": _PROJECT_ID, "title": "受控来源文档",
            "type": "summary", "status": "draft", "revision": 1,
            "updated_at": "2026-09-06T00:00:00Z", "source_refs": [{"source_id": _SOURCE_ID}],
            "blocks": [],
        }
        self._revisions = {1: deepcopy(self._current)}
        self._markdown = {1: "受控发布正文"}

    def read(self, document_id: str):
        return deepcopy(self._current) if document_id == _DOCUMENT_ID else None

    def revision(self, document_id: str, revision: int):
        if document_id != _DOCUMENT_ID or revision not in self._revisions:
            return None
        return {
            "id": f"document-revision-{document_id}-r{revision}",
            "document_id": document_id, "revision": revision,
            "source_snapshot": {"source_refs": deepcopy(self._revisions[revision]["source_refs"])},
        }

    def markdown(self, document_id: str, *, revision: int | None = None):
        if document_id != _DOCUMENT_ID:
            return None
        return self._markdown.get(self._current["revision"] if revision is None else revision)

    def save_user_edit(self, document_id: str, *, markdown: str, expected_revision: int, title: str | None = None):
        if document_id != _DOCUMENT_ID or expected_revision != self._current["revision"]:
            raise DocumentExpectedRevisionError("expected revision is stale")
        self._current = {
            **self._current, "revision": expected_revision + 1, "updated_at": "2026-09-06T00:01:00Z",
            **({"title": title} if title else {}),
        }
        self._revisions[self._current["revision"]] = deepcopy(self._current)
        self._markdown[self._current["revision"]] = markdown
        return deepcopy(self._current)


class _ControlledJobs:
    def __init__(self, database_path: Path) -> None:
        self.sqlite = SimpleNamespace(database_path=database_path)

    def list_jobs(self, *, job_type: str):
        assert job_type == "workbench_content_transform"
        return ({
            "id": _JOB_ID, "job_type": job_type, "execution_version": "effect-v2", "status": "completed",
            "updated_at": "2026-09-06T00:00:00Z", "transform_items": [{"source_id": _SOURCE_ID}],
            "published_outputs": [{"kind": "document", "status": "published", "object_id": _DOCUMENT_ID}],
        },)


def test_document_detail_routes_preserve_verified_source_task_ref_across_save_and_conflict(
    tmp_path: Path, monkeypatch,
) -> None:
    """Controlled ports provide verified evidence; real admission is outside this unit test."""
    store = JsonObjectStore(tmp_path / "objects", namespace_id="default")
    store.write("sources", _SOURCE_ID, {"id": _SOURCE_ID, "project_id": _PROJECT_ID}, expected_revision=None)
    documents = _ControlledDocuments()
    receipt = {"outputs": [{"source_id": _SOURCE_ID, "document_id": _DOCUMENT_ID, "document_revision": 1}]}
    monkeypatch.setattr(product_repositories, "_object_store", lambda _root: (store, SimpleNamespace(namespace_id="default")))
    monkeypatch.setattr(product_repositories, "_document_repository", lambda _root, _store, _settings: documents)
    monkeypatch.setattr(product_document_serialization, "_job_repository", lambda _root, _store: _ControlledJobs(tmp_path / "controlled.sqlite3"))
    monkeypatch.setattr(product_document_serialization, "read_workbench_transform_receipt", lambda _path, job_id: receipt if job_id == _JOB_ID else None)
    application = FastAPI()
    application.include_router(rebuild.router)
    application.dependency_overrides[get_container] = lambda: SimpleNamespace(root_dir=tmp_path)
    expected_ref = task_ref_for_workbench_content_transform(project_id=_PROJECT_ID, job_id=_JOB_ID)

    with TestClient(application) as client:
        opened = client.get(f"/api/rebuild/documents/{_DOCUMENT_ID}")
        saved = client.put(f"/api/rebuild/documents/{_DOCUMENT_ID}", json={
            "expected_revision": 1, "markdown": "用户保存后的正文",
        })
        conflict = client.put(f"/api/rebuild/documents/{_DOCUMENT_ID}", json={
            "expected_revision": 1, "markdown": "陈旧客户端正文",
        })
        reopened = client.get(f"/api/rebuild/documents/{_DOCUMENT_ID}")

    assert opened.status_code == saved.status_code == reopened.status_code == 200
    assert opened.json()["source_task_ref"] == expected_ref
    assert saved.json()["revision"] == 2
    assert saved.json()["source_task_ref"] == expected_ref
    assert conflict.status_code == 409
    assert conflict.json()["current_document"]["revision"] == 2
    assert conflict.json()["current_document"]["source_task_ref"] == expected_ref
    assert reopened.json()["revision"] == 2
    assert reopened.json()["source_task_ref"] == expected_ref
