import os
from pathlib import Path
import subprocess
import sys

import pytest

from backend.memory_app.document_visibility import recognition_document_visible
from backend.recognition import WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.mark.parametrize("collection,state,kind,visible", [
    ("workspace_items", "confirmed", None, True),
    ("workspace_items", "ready", None, False),
    ("workspace_review_intents", "confirmed", None, True),
    ("workspace_review_intents", "confirming", None, False),
    ("recognition_tasks", "completed", "write", True),
    ("recognition_tasks", "completed", "restructure", False),
    ("recognition_tasks", "running", "write", False),
])
@pytest.mark.parametrize("project_id", ["qa", None])
def test_recognition_requires_a_published_current_project_document(tmp_path, collection, state, kind, visible, project_id):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    with records.begin() as tx:
        tx.put("documents", "document-one", {"id": "document-one", "project_id": project_id, "status": "active"}, expected_revision=0)
        publication = {"document_id": "document-one", "project_id": project_id,
                       "status" if collection == "workspace_items" else "state": state}
        if kind is not None:
            publication["kind"] = kind
        tx.put(collection, "publication", publication, expected_revision=0)
        # A publication for another project cannot make this document visible.
        tx.put("workspace_items", "foreign-publication", {
            "document_id": "document-one", "project_id": "other", "status": "confirmed",
        }, expected_revision=0)
        tx.commit()
    assert recognition_document_visible(records, WorkScope("local-user", project_id), "document-one") is visible
    assert not recognition_document_visible(records, WorkScope("local-user", "other"), "document-one")
    assert not recognition_document_visible(records, WorkScope("local-user", project_id), "missing")
    with records.begin() as tx:
        tx.put("documents", "document-one", {"id": "document-one", "project_id": project_id, "status": "archived"}, expected_revision=1)
        tx.commit()
    assert not recognition_document_visible(records, WorkScope("local-user", project_id), "document-one")


def test_publication_queries_do_not_decode_unrelated_collections(tmp_path, monkeypatch):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    with records.begin() as tx:
        tx.put("documents", "one", {"project_id": "qa"}, expected_revision=0)
        tx.put("recognition_tasks", "task", {"project_id": "qa", "document_id": "one", "state": "completed"}, expected_revision=0)
        tx.commit()
    def whole_collection(_collection):
        raise AssertionError("publication lookup must be scoped")
    monkeypatch.setattr(records, "list", whole_collection)
    assert recognition_document_visible(records, WorkScope("local-user", "qa"), "one")


@pytest.mark.parametrize("module", ["backend.shared.document_visibility", "backend.memory_app.document_visibility"])
def test_visibility_import_does_not_construct_an_application(tmp_path, module):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[4] / "src"),
           "CHRIPTMAS_APP_ROOT": str(tmp_path / "must-not-be-created")}
    result = subprocess.run([sys.executable, "-c", (
        f"import sys; from {module} import recognition_document_visible; "
        "assert 'backend.memory_app.app' not in sys.modules; assert 'backend.api.app' not in sys.modules"
    )], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "must-not-be-created").exists()
