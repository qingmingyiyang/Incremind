"""Project HTTP composition and sidecar ownership use the existing stores."""

from copy import deepcopy
from pathlib import Path
from shutil import copyfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2 import privacy
from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from core.document_engine import SQLiteDocumentRepository
from core.document_engine.ports import DocumentDraft
from core.storage_provider import SQLiteStructuredRecordStore


PROJECT_FIELDS = {"id", "name", "scenes", "private", "builtin", "revision"}


@pytest.fixture
def application(tmp_path, monkeypatch):
    # The module's production composition must also use a temporary app root.
    configuration = tmp_path / "config"
    configuration.mkdir()
    copyfile(Path(__file__).resolve().parents[3] / "config/settings.toml.example",
             configuration / "settings.toml")
    monkeypatch.setenv("CHRIPTMAS_APP_ROOT", str(tmp_path))
    from backend.memory_app.app import create_app

    return create_app(runtime_root=tmp_path / "runtime", legacy_app=FastAPI())


@pytest.fixture
def client(application):
    with TestClient(application) as value:
        yield value


def _projects(client):
    response = client.get("/api/v2/projects")
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"items"}
    rows = response.json()["items"]
    assert all(set(row) == PROJECT_FIELDS for row in rows)
    return {row["id"]: row for row in rows}


def test_existing_projects_are_discovered_once_without_mutating_objects(application, client):
    records = application.state.recognition_records
    domains = application.state.workspace_domains
    item = domains.items.create("item-project", "text", "title", "evidence")
    document = application.state.recognition_documents.create(DocumentDraft(
        title="Document", document_type="summary", markdown="# Evidence",
        source_refs=({"source_id": "document-source", "locator": "test://document-source"},),
        project_id="document-project"))
    with records.begin() as tx:
        tx.put("recognitions", "existing-recognition", {
            "scope": {"user_id": "user", "project_id": "recognition-project"},
            "project_id": "recognition-project",
            "status": "active", "content": "recognition evidence"}, expected_revision=0)
        tx.commit()
    before = {collection: deepcopy(records.list(collection)) for collection in
              ("workspace_items", "documents", "recognitions")}
    first = _projects(client)
    assert {"item-project", "document-project", "recognition-project", "inbox", "me"} <= set(first)
    assert first["inbox"]["builtin"] == "inbox"
    assert first["me"]["builtin"] == "me"
    registered = records.list("v2_projects")
    assert _projects(client) == first
    assert records.list("v2_projects") == registered
    assert all(records.list(collection) == previous for collection, previous in before.items())
    assert records.read("workspace_items", item["id"]) is not None
    assert application.state.recognition_documents.read(document["id"]) is not None


def test_default_project_display_name_and_late_discovery(application, client):
    initial = _projects(client)
    application.state.workspace_domains.items.create("default", "text", "Default", "text")
    application.state.workspace_domains.items.create("late-project", "text", "Later", "text")
    discovered = _projects(client)
    assert discovered["default"]["name"] == "默认"
    assert "late-project" in discovered
    assert all(discovered[key] == row for key, row in initial.items())


def test_create_and_patch_project_revision_conflict_contains_current(client):
    created = client.post("/api/v2/projects", json={"name": "研究"})
    assert created.status_code == 200, created.text
    row = created.json()
    assert set(row) == PROJECT_FIELDS
    assert row["name"] == "研究"
    assert row["scenes"] == [] and row["private"] is False and row["builtin"] is None
    assert row["revision"] == 1
    edited = client.patch(f"/api/v2/projects/{row['id']}", json={
        "name": "研究进展", "scenes": ["阅读", "试验"], "expected_revision": row["revision"]})
    assert edited.status_code == 200, edited.text
    current = edited.json()
    assert current["name"] == "研究进展" and current["scenes"] == ["阅读", "试验"]
    assert current["revision"] == row["revision"] + 1
    stale = client.patch(f"/api/v2/projects/{row['id']}", json={
        "name": "过期写入", "expected_revision": row["revision"]})
    assert stale.status_code == 409, stale.text
    assert isinstance(stale.json()["detail"], str)
    assert stale.json()["current"] == current
    assert _projects(client)[row["id"]] == current


def test_project_private_patch_changes_egress_and_invalidates_snapshots(application, client):
    project = client.post("/api/v2/projects", json={"name": "私密研究"}).json()
    records = application.state.recognition_records
    service = application.state.recognition_service
    scope = WorkScope("user", project["id"])
    source_id = service.stage_experience(scope=scope, content="private evidence")
    source = records.read("recognition_experiences", source_id)
    authority = SourceEgressService(records)
    roots = [{"type": "experience", "id": source_id, "revision": source.revision}]
    prior = authority.snapshot(scope, roots)
    models = SimpleNamespace(public=lambda: {"generation": {"allow_remote": True}})
    changed = client.patch(f"/api/v2/projects/{project['id']}", json={
        "private": True, "expected_revision": project["revision"]})
    assert changed.status_code == 200, changed.text
    assert changed.json()["private"] is True
    assert privacy.is_private_project(records, project["id"]) is True
    assert privacy.egress_allowed(records, models, project["id"], "generation") is False
    with pytest.raises(RecognitionConflict):
        authority.validate_snapshot(scope, prior)
    blocked = authority.snapshot(scope, roots)
    restored = client.patch(f"/api/v2/projects/{project['id']}", json={
        "private": False, "expected_revision": changed.json()["revision"]})
    assert restored.status_code == 200, restored.text
    assert restored.json()["private"] is False
    assert privacy.egress_allowed(records, models, project["id"], "generation") is True
    with pytest.raises(RecognitionConflict):
        authority.validate_snapshot(scope, blocked)
    assert records.read("recognition_experiences", source_id) == source


def test_workspace_domains_are_shared_with_existing_routes(application, client):
    from backend.memory_app.workspace import WorkspaceDomains

    domains = application.state.workspace_domains
    assert isinstance(domains, WorkspaceDomains)
    assert domains.intake.items is domains.items
    assert domains.review.items is domains.items
    assert domains.review.confirmations is domains.confirmations
    assert domains.review.legacy_reviews is domains.legacy_reviews
    assert domains.query.records is domains.items.records is application.state.recognition_records
    assert domains.review.documents is domains.query.documents is application.state.recognition_documents
    assert domains.review.service is domains.query.service is application.state.recognition_service
    stored = domains.items.create("shared-project", "text", "Shared", "Shared evidence")
    response = client.get("/api/workspace/v1/items", params={"project_id": "shared-project"})
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["id"] == stored["id"]


def test_installer_skips_nondefault_document_namespace(tmp_path, caplog):
    from backend.memory_app.v2 import install_v2_routes

    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    application = FastAPI()
    with caplog.at_level("INFO", logger="backend.memory_app.v2"):
        install_v2_routes(application, runtime_root=tmp_path, records=records,
                          models=SimpleNamespace(),
                          documents=SQLiteDocumentRepository(records, namespace_id="custom"),
                          service=RecognitionService(records), workspace=SimpleNamespace())
    with TestClient(application) as client:
        assert client.get("/api/v2/projects").status_code == 404
    assert records.list("v2_projects") == ()
    assert any(record.name == "backend.memory_app.v2" and "custom" in record.message
               for record in caplog.records)


@pytest.mark.parametrize("object_type", ["item", "document", "candidate", "recognition"])
def test_scene_assignments_preserve_original_objects_and_revisions(tmp_path, object_type):
    from backend.memory_app.v2.projects import assign_scene, scene_of

    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    collection = {"item": "workspace_items", "document": "documents",
                  "candidate": "recognition_candidates", "recognition": "recognitions"}[object_type]
    # Storage supports 128 character IDs; sidecars must preserve this capacity.
    object_id = "a" * 128
    with records.begin() as tx:
        original = tx.put(collection, object_id, {"project_id": "original", "content": "unchanged"},
                          expected_revision=0)
        tx.commit()
    assert scene_of(records, object_type, object_id) is None
    assign_scene(records, object_type, object_id, "project-a", "阅读")
    assert scene_of(records, object_type, object_id) == {"project_id": "project-a", "scene": "阅读"}
    assign_scene(records, object_type, object_id, "project-b", "试验")
    assert scene_of(records, object_type, object_id) == {"project_id": "project-b", "scene": "试验"}
    assert records.read(collection, object_id) == original


def test_scene_object_types_are_independent(tmp_path):
    from backend.memory_app.v2.projects import assign_scene, scene_of

    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    types = ["item", "document", "candidate", "recognition"]
    for index, object_type in enumerate(types):
        assign_scene(records, object_type, "same-id", f"project-{index}", f"scene-{index}")
    for index, object_type in enumerate(types):
        assert scene_of(records, object_type, "same-id") == {
            "project_id": f"project-{index}", "scene": f"scene-{index}"}


def test_project_writes_reject_remote_origin(client, application):
    before = application.state.recognition_records.list("v2_projects")
    response = client.post("/api/v2/projects", json={"name": "blocked"},
                           headers={"origin": "https://untrusted.example"})
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "local_origin_required"}
    assert application.state.recognition_records.list("v2_projects") == before


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": 17}, {"name": "   "}])
def test_project_creation_rejects_invalid_name(client, body):
    response = client.post("/api/v2/projects", json=body)
    assert response.status_code == 400, response.text
    assert isinstance(response.json()["detail"], str)


def test_stale_private_patch_does_not_change_privacy_state(application, client):
    project = client.post("/api/v2/projects", json={"name": "研究"}).json()
    changed = client.patch(f"/api/v2/projects/{project['id']}", json={
        "name": "研究进展", "expected_revision": project["revision"]}).json()
    records = application.state.recognition_records
    before = privacy.privacy_revision(records)
    stale = client.patch(f"/api/v2/projects/{project['id']}", json={
        "private": True, "expected_revision": project["revision"]})
    assert stale.status_code == 409
    assert stale.json()["current"] == changed
    assert privacy.privacy_revision(records) == before
    assert not privacy.is_private_project(records, project["id"])


def test_project_and_privacy_write_roll_back_together(application, client, monkeypatch):
    from core.storage_provider import SQLiteUnitOfWorkConflict
    from core.storage_provider.sqlite_uow import SQLiteStructuredRecordUnitOfWork

    project = client.post("/api/v2/projects", json={"name": "研究"}).json()
    records = application.state.recognition_records
    original_put = SQLiteStructuredRecordUnitOfWork.put

    def conflict_after_privacy(tx, collection, object_id, payload, *, expected_revision):
        if collection == "v2_projects":
            raise SQLiteUnitOfWorkConflict("injected project write conflict")
        return original_put(tx, collection, object_id, payload, expected_revision=expected_revision)

    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", conflict_after_privacy)
    response = client.patch(f"/api/v2/projects/{project['id']}", json={
        "private": True, "expected_revision": project["revision"]})
    assert response.status_code == 409
    assert records.read("v2_projects", project["id"]).revision == project["revision"]
    assert records.read("v2_private_scopes", project["id"]) is None
    assert privacy.privacy_revision(records) == 0
