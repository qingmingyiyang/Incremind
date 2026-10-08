from __future__ import annotations

import pytest

from backend.memory_app.erasure import ErasureConflict, ErasureService
from backend.recognition import WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


def _scope(project_id="project-a"):
    return WorkScope("local-user", project_id)


def _put(records, collection, object_id, payload):
    with records.begin() as tx:
        tx.put(collection, object_id, payload, expected_revision=0)
        tx.commit()


def _recognition(identifier, scope, *, sources=()):
    return {
        "id": identifier,
        "scope": {"user_id": scope.user_id, "project_id": scope.project_id},
        "project_id": scope.project_id,
        "content": f"recognition {identifier}",
        "state": "active",
        "version": 1,
        "source_experience_ids": list(sources),
        "source_recognition_ids": [],
        "parent_ids": [],
        "source_experience_revisions": {item: 1 for item in sources},
        "source_recognition_revisions": {},
        "conditions": [],
    }


def _experience(identifier, scope):
    return {
        "id": identifier,
        "scope": {"user_id": scope.user_id, "project_id": scope.project_id},
        "project_id": scope.project_id,
        "content": f"experience {identifier}",
        "state": "active",
    }


def _import(scope, recognition_ids, experience_ids, *, secret="migration archive secret"):
    return {
        "scope": {"user_id": scope.user_id, "project_id": scope.project_id},
        "project_id": scope.project_id,
        "bundle": {"history": [{"source_text": secret}]},
        "recognition_ids": list(recognition_ids),
        "experience_ids": list(experience_ids),
        "mapping": {"recognitions": {}},
        "receipt": {"state": "imported"},
    }


def _migration_version(scope, recognition_id, import_id, index):
    return {
        "scope": {"user_id": scope.user_id, "project_id": scope.project_id},
        "project_id": scope.project_id,
        "recognition_id": recognition_id,
        "import_id": import_id,
        "source_group": "versions",
        "source_index": index,
    }


def test_erasure_expands_to_entire_import_batch_and_erases_archive_and_pointers(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    scope = _scope()
    _put(records, "recognitions", "root", _recognition("root", scope))
    _put(records, "recognitions", "same-batch", _recognition("same-batch", scope))
    _put(records, "recognition_experiences", "source-a", _experience("source-a", scope))
    _put(records, "recognition_experiences", "source-b", _experience("source-b", scope))
    _put(records, "recognition_migration_imports", "import-a", _import(
        scope, ["root", "same-batch"], ["source-a", "source-b"]))
    _put(records, "recognition_migration_versions", "pointer-root", _migration_version(scope, "root", "import-a", 0))
    _put(records, "recognition_migration_versions", "pointer-peer", _migration_version(scope, "same-batch", "import-a", 1))

    service = ErasureService(records, tmp_path)
    preview = service.preview(scope=scope, recognition_id="root", expected_revision=1)

    assert set(preview.affected["recognitions"]) == {"root", "same-batch"}
    assert set(preview.affected["recognition_experiences"]) == {"source-a", "source-b"}
    assert preview.affected["recognition_migration_imports"] == ("import-a",)
    assert set(preview.affected["recognition_migration_versions"]) == {"pointer-root", "pointer-peer"}

    service.erase(scope=scope, recognition_id="root", expected_revision=1, expected_plan=preview.revisions)

    for collection in ("recognitions", "recognition_experiences", "recognition_migration_imports", "recognition_migration_versions"):
        assert records.list(collection) == ()
    assert "migration archive secret" not in str(records.list("recognition_erasure_receipts"))


def test_erasure_follows_shared_migration_source_into_another_batch_but_not_other_scope(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    scope, other = _scope(), _scope("project-b")
    _put(records, "recognitions", "root", _recognition("root", scope))
    _put(records, "recognitions", "dependent", _recognition("dependent", scope, sources=["shared-source"]))
    for identifier in ("shared-source", "batch-b-source"):
        _put(records, "recognition_experiences", identifier, _experience(identifier, scope))
    _put(records, "recognition_migration_imports", "import-a", _import(scope, ["root"], ["shared-source"]))
    _put(records, "recognition_migration_imports", "import-b", _import(scope, ["dependent"], ["shared-source", "batch-b-source"]))
    _put(records, "recognition_migration_versions", "pointer-b", _migration_version(scope, "dependent", "import-b", 0))
    _put(records, "recognition_migration_imports", "foreign-import", _import(other, ["root"], ["foreign-source"], secret="foreign secret"))

    preview = ErasureService(records, tmp_path).preview(scope=scope, recognition_id="root", expected_revision=1)

    assert set(preview.affected["recognitions"]) == {"root", "dependent"}
    assert set(preview.affected["recognition_experiences"]) == {"shared-source", "batch-b-source"}
    assert set(preview.affected["recognition_migration_imports"]) == {"import-a", "import-b"}
    assert preview.affected["recognition_migration_versions"] == ("pointer-b",)

    ErasureService(records, tmp_path).erase(scope=scope, recognition_id="root", expected_revision=1, expected_plan=preview.revisions)
    assert records.read("recognition_migration_imports", "foreign-import") is not None
    assert "foreign secret" in str(records.read("recognition_migration_imports", "foreign-import").payload)


def test_erasure_of_normally_selected_imported_experience_expands_its_batch(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    scope = _scope()
    _put(records, "recognitions", "root", _recognition("root", scope))
    _put(records, "recognitions", "batch-peer", _recognition("batch-peer", scope))
    _put(records, "recognition_experiences", "experience-task-r1", _experience("experience-task-r1", scope))
    _put(records, "recognition_migration_imports", "import-a", _import(
        scope, ["batch-peer"], ["experience-task-r1"]))
    _put(records, "recognition_context_packets", "packet", {
        "project_id": scope.project_id, "items": [{"id": "root"}],
    })
    _put(records, "recognition_tasks", "task", {
        "project_id": scope.project_id, "context_packet_id": "packet",
    })

    preview = ErasureService(records, tmp_path).preview(scope=scope, recognition_id="root", expected_revision=1)

    assert set(preview.affected["recognitions"]) == {"root", "batch-peer"}
    assert preview.affected["recognition_migration_imports"] == ("import-a",)
    assert preview.affected["recognition_experiences"] == ("experience-task-r1",)


def test_erasure_rejects_preview_when_migration_archive_changes(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    scope = _scope()
    _put(records, "recognitions", "root", _recognition("root", scope))
    _put(records, "recognition_experiences", "source", _experience("source", scope))
    archive = _import(scope, ["root"], ["source"])
    _put(records, "recognition_migration_imports", "import-a", archive)
    service = ErasureService(records, tmp_path)
    preview = service.preview(scope=scope, recognition_id="root", expected_revision=1)

    with records.begin() as tx:
        tx.put("recognition_migration_imports", "import-a", {**archive, "receipt": {"state": "changed"}}, expected_revision=1)
        tx.commit()

    with pytest.raises(ErasureConflict, match="scope changed"):
        service.erase(scope=scope, recognition_id="root", expected_revision=1, expected_plan=preview.revisions)
    assert records.read("recognitions", "root") is not None
    assert records.read("recognition_migration_imports", "import-a") is not None
