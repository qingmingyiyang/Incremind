from uuid import uuid4

import pytest

from backend.memory_app.migration_bundle import MigrationBundleService
from backend.memory_app.migration_plan import ImportPreviewService
from backend.recognition import RecognitionError, RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


def source_bundle(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "source.sqlite3")
    scope = WorkScope("user", "source")
    service = RecognitionService(records)
    eid = service.stage_experience(scope=scope, experience_id="e-one", content="Source")
    candidate = service.propose(scope=scope, content="First", source_experience_ids=[eid])
    rec = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision,
                          reviewer="user", recognition_id="r-one")
    service.revise(scope=scope, recognition_id=rec.id, expected_revision=rec.revision, content="Second")
    return MigrationBundleService(records).export(
        scope=scope, recognition_ids=[rec.id], allowed_experience_ids=[eid])["bundle"]


def test_preview_is_read_only_and_keeps_source_revision_separate(tmp_path):
    bundle = source_bundle(tmp_path)
    target = SQLiteStructuredRecordStore(tmp_path / "target.sqlite3")
    service = ImportPreviewService(target)
    arguments = dict(scope=WorkScope("user", "target"), bundle=bundle, import_id=str(uuid4()))
    first = service.preview(**arguments)
    assert service.preview(**arguments) == first
    assert first["recognitions"][0]["source_revision"] == 2
    assert first["recognitions"][0]["target_initial_revision"] == 1
    assert first["counts"]["versions"] == 2
    assert first["recognitions"][0]["planned_state"] == "active"
    assert first["recognitions"][0]["reason"] is None
    assert first["conflicts"] == []
    assert target.list("recognitions") == ()
    assert target.list("recognition_migration_imports") == ()


def test_preview_rejects_history_that_claims_a_future_revision(tmp_path):
    bundle = source_bundle(tmp_path)
    old = next(row for row in bundle["versions"] if row["payload"]["version"] == 1)
    old["payload"]["recognition_revision"] = 999
    service = ImportPreviewService(SQLiteStructuredRecordStore(tmp_path / "target.sqlite3"))
    with pytest.raises(RecognitionError, match="history revisions"):
        service.preview(scope=WorkScope("user", "target"), bundle=bundle, import_id=str(uuid4()))


def test_preview_reports_collisions_without_disclosing_foreign_content(tmp_path):
    bundle = source_bundle(tmp_path)
    target = SQLiteStructuredRecordStore(tmp_path / "target.sqlite3")
    with target.begin() as tx:
        tx.put("recognitions", "r-one", {"content": "PRIVATE OTHER PROJECT"}, expected_revision=0)
        tx.put("recognition_tombstones", "e-one", {"scope": {"user_id": "foreign"}}, expected_revision=0)
        tx.commit()
    service = ImportPreviewService(target)
    args = dict(scope=WorkScope("user", "target"), bundle=bundle, import_id=str(uuid4()))
    rejected = service.preview(**args)
    assert {issue["reason"] for issue in rejected["conflicts"]} == {"erased_id", "id_exists"}
    assert "PRIVATE OTHER PROJECT" not in str(rejected)
    copied = service.preview(**args, strategy="copy")
    assert copied["conflicts"] == []
    assert copied["mapping"]["recognitions"]["r-one"] != "r-one"
    assert service.preview(**args, strategy="copy")["mapping"] == copied["mapping"]


def test_preview_changes_when_target_slot_changes_and_rejects_bad_strategy(tmp_path):
    bundle = source_bundle(tmp_path)
    target = SQLiteStructuredRecordStore(tmp_path / "target.sqlite3")
    service = ImportPreviewService(target)
    args = dict(scope=WorkScope("user", "target"), bundle=bundle, import_id=str(uuid4()))
    first = service.preview(**args)
    with target.begin() as tx:
        tx.put("recognition_experiences", "e-one", {"state": "active"}, expected_revision=0)
        tx.commit()
    assert service.preview(**args)["target_snapshot"] != first["target_snapshot"]
    with pytest.raises(RecognitionError):
        service.preview(**args, strategy="overwrite")
    with pytest.raises(RecognitionError):
        service.preview(**{**args, "import_id": "../../other"})


@pytest.mark.parametrize(
    ("source_type", "source_id", "strategy"),
    (("experience", "e-one", "reject"), ("recognition", "r-one", "reject"),
     ("experience", "e-one", "copy"), ("recognition", "r-one", "copy")),
)
def test_preview_blocks_orphaned_typed_policy_at_import_target_slot(tmp_path, source_type, source_id, strategy):
    bundle = source_bundle(tmp_path)
    target = SQLiteStructuredRecordStore(tmp_path / f"target-{source_type}-{strategy}.sqlite3")
    service = ImportPreviewService(target)
    args = dict(scope=WorkScope("user", "target"), bundle=bundle, import_id=str(uuid4()), strategy=strategy)
    initial = service.preview(**args)
    group = "experiences" if source_type == "experience" else "recognitions"
    target_id = initial["mapping"][group][source_id]
    collection = f"source_egress_{source_type}_policies"
    # This is an orphaned policy: before the import there is no source to
    # validate it against, but after an r1 import it would look current.
    with target.begin() as tx:
        tx.put(collection, target_id, {
            "id": target_id, "source_type": source_type, "source_id": target_id,
            "scope": {"user_id": "user", "project_id": "target"}, "user_id": "user",
            "project_id": "target", "source_revision": 1,
            "allowed_purposes": ["generation"],
        }, expected_revision=0)
        tx.commit()
    refreshed = service.preview(**args)
    assert {"collection": collection, "id": target_id, "reason": "id_exists"} in refreshed["conflicts"]
    assert {"collection": collection, "id": target_id, "revision": 1, "tombstone_revision": 0} in refreshed["target_snapshot"]


def test_long_source_id_can_be_previewed_with_explicit_copy_strategy(tmp_path):
    bundle = source_bundle(tmp_path)
    long_id = "r" * 128
    bundle["recognitions"][0]["id"] = long_id
    bundle["recognitions"][0]["payload"]["id"] = long_id
    for row in bundle["versions"]:
        row["id"] = f"{long_id}~v{row['payload']['version']}"
        row["payload"].update(id=row["id"], recognition_id=long_id)
        row["payload"]["snapshot"]["id"] = long_id
    service = ImportPreviewService(SQLiteStructuredRecordStore(tmp_path / "target.sqlite3"))
    args = dict(scope=WorkScope("user", "target"), bundle=bundle, import_id=str(uuid4()))
    assert any(c["reason"] == "target_id_too_long" for c in service.preview(**args)["conflicts"])
    assert service.preview(**args, strategy="copy")["conflicts"] == []
