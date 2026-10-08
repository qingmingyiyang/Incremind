"""Portable history must not acquire current evidence eligibility on import."""
from uuid import uuid4

import pytest

from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from backend.memory_app.migration_bundle import MigrationBundleService
from backend.memory_app.migration_plan import ImportPreviewService
from backend.memory_app.migration_commit import ImportService
from core.storage_provider import SQLiteStructuredRecordStore
from tests.recognition.test_artifact_dependencies import env, _chain, _publish


def _import(env, tmp_path, bundle):
    records = SQLiteStructuredRecordStore(tmp_path / "target.sqlite3")
    scope = WorkScope("user", "target")
    args = dict(scope=scope, bundle=bundle, import_id=str(uuid4()), strategy="copy")
    plan = ImportPreviewService(records).preview(**args)
    ImportService(records).commit(**args, expected_plan=plan)
    return records, RecognitionService(records), scope, plan


@pytest.mark.parametrize("kind", ["model_generated_artifact", "user_statement"])
def test_missing_artifact_evidence_stays_unavailable_after_import(env, tmp_path, kind):
    chain = _chain(env)
    bundle = MigrationBundleService(env.records).export(env.scope,
        [chain.recognitions[-1].id], [chain.artifacts[-1].id])["bundle"]
    if kind == "user_statement":
        # Changing the descriptive kind cannot resolve retained missing refs.
        provenance = bundle["experiences"][0]["payload"]["provenance"]
        provenance.update(kind=kind, epistemic_status="user_asserted")
        provenance.pop("artifact_status"); provenance.pop("outcome_status")
    records, service, scope, plan = _import(env, tmp_path, bundle)
    assert plan["recognitions"][0]["planned_state"] == "stale"
    assert any(issue["code"] == "not_recallable_after_import" for issue in plan["issues"])
    rid = plan["recognitions"][0]["target_id"]
    assert service.get_recognition(scope=scope, recognition_id=rid).state == "stale"
    assert service.retrieval_entries(scope=scope) == ()
    eid = next(iter(plan["mapping"]["experiences"].values()))
    experience = records.read("recognition_experiences", eid)
    assert experience.payload["state"] == "active"
    assert experience.payload["provenance"]["kind"] == kind
    assert experience.payload["provenance"]["source_refs"]
    before = records.list_all()
    with pytest.raises(RecognitionConflict):
        service.propose(scope=scope, content="claim from unresolved imported evidence", source_experience_ids=[eid])
    assert records.list_all() == before
    restarted = RecognitionService(SQLiteStructuredRecordStore(records.database_path))
    with pytest.raises(RecognitionConflict):
        restarted.propose(scope=scope, content="after restart", source_experience_ids=[eid])
    if kind == "model_generated_artifact":
        # Even relabelling a known imported history row cannot erase the
        # durable receipt's unresolved evidence. No normal edit API does this.
        with records.begin() as tx:
            row = tx.read("recognition_experiences", eid)
            provenance = dict(row.payload["provenance"])
            provenance.update(kind="user_statement", epistemic_status="user_asserted")
            provenance.pop("artifact_status"); provenance.pop("outcome_status")
            tx.put("recognition_experiences", eid, {**row.payload, "provenance": provenance},
                   expected_revision=row.revision)
            tx.commit()
        with pytest.raises(RecognitionConflict):
            restarted.propose(scope=scope, content="relabeled after import", source_experience_ids=[eid])


@pytest.mark.parametrize("provenance", [None, {"kind": "user_statement"}])
def test_plain_statement_and_legacy_import_remain_eligible(env, tmp_path, provenance):
    eid = env.service.stage_experience(scope=env.scope, content="ordinary retained statement", provenance=provenance)
    recognition = _publish(env, "ordinary", experiences=[eid])
    bundle = MigrationBundleService(env.records).export(env.scope, [recognition.id], [eid])["bundle"]
    records, service, scope, plan = _import(env, tmp_path, bundle)
    assert plan["recognitions"][0]["planned_state"] == "active"
    assert service.list_recognitions(scope=scope)[0].authorized
