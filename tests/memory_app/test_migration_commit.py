from copy import deepcopy
from uuid import uuid4

import pytest

from backend.memory_app.migration_bundle import MigrationBundleService
from backend.memory_app.migration_commit import ImportService
from backend.memory_app.migration_plan import ImportPreviewService
from backend.memory_app.relations import RelationProposalService
from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteStructuredRecordUnitOfWork


def _bundle(tmp_path, *, allowed=True):
    source = SQLiteStructuredRecordStore(tmp_path / "source.sqlite3")
    scope = WorkScope("user", "source")
    service = RecognitionService(source)
    evidence = service.stage_experience(scope=scope, experience_id="e-one", content="evidence")
    candidate = service.propose(scope=scope, candidate_id="c-one", content="conclusion", source_experience_ids=[evidence])
    recognition = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision,
                                  reviewer="user", recognition_id="r-one")
    return MigrationBundleService(source).export(scope, [recognition.id], [evidence] if allowed else [])["bundle"]


def _arguments(target, bundle):
    scope = WorkScope("user", "target")
    import_id = str(uuid4())
    plan = ImportPreviewService(target).preview(scope=scope, bundle=bundle, import_id=import_id)
    return scope, import_id, plan


def test_commit_writes_live_records_baseline_and_history_pointer(tmp_path):
    target = SQLiteStructuredRecordStore(tmp_path / "target.sqlite3")
    bundle = _bundle(tmp_path)
    scope, import_id, plan = _arguments(target, bundle)

    receipt = ImportService(target).commit(scope=scope, bundle=bundle, import_id=import_id,
                                           strategy="reject", expected_plan=plan)

    recognition_id = receipt["recognition_ids"][0]
    recognition = target.read("recognitions", recognition_id)
    assert recognition is not None and recognition.revision == 1
    assert recognition.payload["state"] == "active"
    assert recognition.payload["source_experience_revisions"] == {receipt["experience_ids"][0]: 1}
    baseline = target.read("recognition_versions", f"{recognition_id}~v1")
    assert baseline is not None and baseline.payload["action"] == "import"
    pointers = target.list("recognition_migration_versions")
    assert len(pointers) == 1
    assert pointers[0].payload["source_group"] == "versions"
    assert target.read("recognition_migration_imports", import_id).payload["bundle"] == bundle


def test_commit_replays_same_request_but_rejects_changed_body_and_scope(tmp_path):
    target = SQLiteStructuredRecordStore(tmp_path / "target.sqlite3")
    bundle = _bundle(tmp_path)
    scope, import_id, plan = _arguments(target, bundle)
    importer = ImportService(target)
    receipt = importer.commit(scope=scope, bundle=bundle, import_id=import_id, strategy="reject", expected_plan=plan)
    with target.begin() as uow:
        record = uow.read("recognitions", receipt["recognition_ids"][0])
        uow.put("recognitions", record.object_id, {**record.payload, "content": "later"}, expected_revision=record.revision)
        uow.commit()
    assert importer.commit(scope=scope, bundle=bundle, import_id=import_id, strategy="reject", expected_plan={}) == receipt
    changed = deepcopy(bundle)
    changed["recognitions"][0]["payload"]["content"] = "changed"
    changed["versions"][-1]["payload"]["snapshot"]["content"] = "changed"
    with pytest.raises(RecognitionConflict):
        importer.commit(scope=scope, bundle=changed, import_id=import_id, strategy="reject", expected_plan=plan)
    with pytest.raises(RecognitionConflict):
        importer.commit(scope=WorkScope("user", "other"), bundle=bundle, import_id=import_id,
                        strategy="reject", expected_plan=plan)


def test_missing_current_source_stays_stale_and_is_not_retrievable(tmp_path):
    target = SQLiteStructuredRecordStore(tmp_path / "target.sqlite3")
    bundle = _bundle(tmp_path, allowed=False)
    scope, import_id, plan = _arguments(target, bundle)
    assert plan["recognitions"][0]["planned_state"] == "stale"
    assert plan["recognitions"][0]["reason"] == "migration source is unavailable, changed, or cyclic"
    receipt = ImportService(target).commit(scope=scope, bundle=bundle, import_id=import_id,
                                           strategy="reject", expected_plan=plan)
    recognition = target.read("recognitions", receipt["recognition_ids"][0])
    assert recognition.payload["state"] == "stale"
    assert receipt["recognition_ids"] == receipt["not_recallable_ids"]
    assert RecognitionService(target).list_recognitions(scope=scope) == ()


def test_cycle_preview_matches_committed_not_recallable_state(tmp_path):
    source = SQLiteStructuredRecordStore(tmp_path / "cycle-source.sqlite3")
    scope = WorkScope("user", "source")
    service = RecognitionService(source)
    evidence = service.stage_experience(scope=scope, experience_id="e-one", content="evidence")
    first = service.propose(scope=scope, candidate_id="c-one", content="first", source_experience_ids=[evidence])
    first = service.publish(scope=scope, candidate_id=first.id, expected_revision=first.revision, reviewer="user", recognition_id="r-one")
    second = service.propose(scope=scope, candidate_id="c-two", content="second", source_experience_ids=[evidence])
    second = service.publish(scope=scope, candidate_id=second.id, expected_revision=second.revision, reviewer="user", recognition_id="r-two")
    bundle = MigrationBundleService(source).export(scope, [first.id, second.id], [evidence])["bundle"]
    rows = {row["id"]: row for row in bundle["recognitions"]}
    for source_id, other_id in ((first.id, second.id), (second.id, first.id)):
        payload = rows[source_id]["payload"]
        payload["source_recognition_ids"] = [other_id]
        payload["source_recognition_revisions"] = {other_id: 1}
        version = next(row for row in bundle["versions"] if row["payload"]["recognition_id"] == source_id)
        version["payload"]["snapshot"] = deepcopy(payload)
    target = SQLiteStructuredRecordStore(tmp_path / "cycle-target.sqlite3")
    target_scope, import_id, plan = _arguments(target, bundle)
    assert {row["planned_state"] for row in plan["recognitions"]} == {"stale"}
    receipt = ImportService(target).commit(scope=target_scope, bundle=bundle, import_id=import_id,
                                           strategy="reject", expected_plan=plan)
    assert set(receipt["recognition_ids"]) == set(receipt["not_recallable_ids"])
    assert {target.read("recognitions", item).payload["state"] for item in receipt["recognition_ids"]} == {"stale"}


def test_reimported_history_is_pointed_to_its_original_archive_once(tmp_path):
    first_target = SQLiteStructuredRecordStore(tmp_path / "first.sqlite3")
    bundle = _bundle(tmp_path)
    first_scope, first_import, first_plan = _arguments(first_target, bundle)
    first_receipt = ImportService(first_target).commit(scope=first_scope, bundle=bundle, import_id=first_import,
                                                       strategy="reject", expected_plan=first_plan)
    exported = MigrationBundleService(first_target).export(first_scope, first_receipt["recognition_ids"], first_receipt["experience_ids"])["bundle"]
    assert len(exported["prior_versions"]) == 1

    second_target = SQLiteStructuredRecordStore(tmp_path / "second.sqlite3")
    second_scope, second_import, second_plan = _arguments(second_target, exported)
    ImportService(second_target).commit(scope=second_scope, bundle=exported, import_id=second_import,
                                        strategy="reject", expected_plan=second_plan)
    pointers = second_target.list("recognition_migration_versions")
    assert {row.payload["source_group"] for row in pointers} == {"versions", "prior_versions"}
    archive = second_target.read("recognition_migration_imports", second_import)
    assert archive is not None and archive.payload["bundle"] == exported


def test_approved_relation_with_old_source_revision_remains_expired(tmp_path):
    source = SQLiteStructuredRecordStore(tmp_path / "relation-source.sqlite3")
    scope = WorkScope("user", "source")
    service = RecognitionService(source)
    evidence = service.stage_experience(scope=scope, experience_id="e-one", content="evidence")
    first = service.propose(scope=scope, candidate_id="c-one", content="first", source_experience_ids=[evidence])
    first = service.publish(scope=scope, candidate_id=first.id, expected_revision=first.revision, reviewer="user", recognition_id="r-one")
    second = service.propose(scope=scope, candidate_id="c-two", content="second", source_experience_ids=[evidence])
    second = service.publish(scope=scope, candidate_id=second.id, expected_revision=second.revision, reviewer="user", recognition_id="r-two")
    proposal = RelationProposalService(source).propose(scope, first.id, second.id, "supports", "manual evidence")
    RelationProposalService(source).review(scope, proposal["id"], proposal["revision"], "approved")
    service.revise(scope=scope, recognition_id=first.id, expected_revision=first.revision, content="first revised")
    bundle = MigrationBundleService(source).export(scope, [first.id, second.id], [evidence])["bundle"]
    target = SQLiteStructuredRecordStore(tmp_path / "relation-target.sqlite3")
    target_scope, import_id, plan = _arguments(target, bundle)
    receipt = ImportService(target).commit(scope=target_scope, bundle=bundle, import_id=import_id,
                                           strategy="reject", expected_plan=plan)
    proposal = target.list("recognition_relation_proposals")[0]
    assert proposal.payload["from_id"] not in receipt["recognition_ids"]
    assert any(item["code"] == "approved_relation_endpoint_revision_mismatch" for item in receipt["issues"])


def test_plan_must_match_current_target_and_failed_write_rolls_back(tmp_path, monkeypatch):
    target = SQLiteStructuredRecordStore(tmp_path / "target.sqlite3")
    bundle = _bundle(tmp_path)
    scope, import_id, plan = _arguments(target, bundle)
    with target.begin() as uow:
        uow.put("recognition_questions", "r-one", {"scope": {"user_id": "user", "project_id": "target"}, "project_id": "target"}, expected_revision=0)
        uow.commit()
    with pytest.raises(RecognitionConflict, match="preview changed"):
        ImportService(target).commit(scope=scope, bundle=bundle, import_id=import_id, strategy="reject", expected_plan=plan)

    target = SQLiteStructuredRecordStore(tmp_path / "target-fault.sqlite3")
    scope, import_id, plan = _arguments(target, bundle)
    original_put = SQLiteStructuredRecordUnitOfWork.put
    def fault(self, *args, **kwargs):
        if args and args[0] == "recognition_migration_versions":
            raise RuntimeError("injected history write failure")
        return original_put(self, *args, **kwargs)
    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", fault)
    with pytest.raises(RuntimeError, match="injected history"):
        ImportService(target).commit(scope=scope, bundle=bundle, import_id=import_id, strategy="reject", expected_plan=plan)
    assert target.list("recognitions") == ()
    assert target.list("recognition_migration_imports") == ()


def test_policy_added_after_preview_rejects_atomically_without_importing_records(tmp_path):
    target = SQLiteStructuredRecordStore(tmp_path / "target.sqlite3")
    bundle = _bundle(tmp_path)
    scope, import_id, plan = _arguments(target, bundle)
    with target.begin() as uow:
        uow.put("source_egress_experience_policies", "e-one", {
            "id": "e-one", "source_type": "experience", "source_id": "e-one",
            "scope": {"user_id": "user", "project_id": "target"}, "user_id": "user",
            "project_id": "target", "source_revision": 1,
            "allowed_purposes": ["generation"],
        }, expected_revision=0)
        uow.commit()
    with pytest.raises(RecognitionConflict, match="preview changed"):
        ImportService(target).commit(scope=scope, bundle=bundle, import_id=import_id,
                                     strategy="reject", expected_plan=plan)
    assert target.read("recognition_experiences", "e-one") is None
    assert target.read("recognitions", "r-one") is None
    assert target.read("recognition_migration_imports", import_id) is None
    assert target.read("source_egress_experience_policies", "e-one") is not None


def test_imported_sources_are_nonprivate_until_explicitly_marked_private(tmp_path):
    target = SQLiteStructuredRecordStore(tmp_path / "target.sqlite3")
    bundle = _bundle(tmp_path)
    scope, import_id, plan = _arguments(target, bundle)
    receipt = ImportService(target).commit(scope=scope, bundle=bundle, import_id=import_id,
                                           strategy="reject", expected_plan=plan)
    experience_id = receipt["experience_ids"][0]
    recognition_id = receipt["recognition_ids"][0]
    assert target.read("source_egress_experience_policies", experience_id) is None
    assert target.read("source_egress_recognition_policies", recognition_id) is None
    authority = SourceEgressService(target)
    refs = [{
        "type": "experience", "id": experience_id, "revision": 1,
    }]
    snapshot = authority.snapshot(scope, refs)
    for purpose in ("generation", "embedding", "rerank"):
        authority.require(snapshot, purpose)
    authority.set_policy(scope, "experience", experience_id, 1, 0, [])
    with pytest.raises(RecognitionConflict):
        authority.validate_snapshot(scope, snapshot)
    private = authority.snapshot(scope, refs)
    for purpose in ("generation", "embedding", "rerank"):
        with pytest.raises(RecognitionConflict, match="not authorized"):
            authority.require(private, purpose)
