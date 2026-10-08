from __future__ import annotations

from copy import deepcopy

import pytest

from backend.memory_app.migration_bundle import (
    MigrationBundleError,
    MigrationBundleService,
    inspect_bundle,
    validate_bundle,
)
from backend.memory_app.relations import RelationProposalService
from backend.recognition import RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def records(tmp_path):
    return SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3")


@pytest.fixture
def scope():
    return WorkScope("user-1", "project-1")


def _published(service, scope, recognition_id, experience_id, content, *, provenance=None):
    experience = service.stage_experience(
        scope=scope, experience_id=experience_id, content=f"evidence: {content}", provenance=provenance,
    )
    candidate = service.propose(
        scope=scope, candidate_id=f"candidate-{recognition_id[:32]}",
        content=content, source_experience_ids=[experience],
    )
    return service.publish(
        scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision,
        reviewer="user-1", recognition_id=recognition_id,
    )


def test_export_is_bounded_read_only_and_keeps_only_selected_provenance(records, scope):
    service = RecognitionService(records)
    first = _published(
        service, scope, "recognition-one", "experience-one", "first conclusion",
        provenance={
            "kind": "user_statement", "actor": "user", "source_refs": [{"type": "document", "id": "external-note"}],
            "recorded_at": "2026-09-16T00:00:00+00:00",
        },
    )
    revised = service.revise(
        scope=scope, recognition_id=first.id, expected_revision=first.revision,
        content="first conclusion, clarified",
    )
    second = _published(service, scope, "recognition-two", "experience-two", "second conclusion")
    approved = RelationProposalService(records).propose(
        scope, revised.id, second.id, "supports", "I checked both conclusions."
    )
    RelationProposalService(records).review(scope, approved["id"], approved["revision"], "approved")
    before = {
        collection: tuple((item.object_id, item.revision, item.payload) for item in records.list(collection))
        for collection in ("recognitions", "recognition_experiences", "recognition_versions", "recognition_relation_proposals")
    }

    result = MigrationBundleService(records).export(scope, [revised.id, second.id], ["experience-one"])

    bundle = result["bundle"]
    assert bundle["schema"] == "recognition-migration-v1"
    assert [item["id"] for item in bundle["recognitions"]] == [revised.id, second.id]
    assert [item["id"] for item in bundle["experiences"]] == ["experience-one"]
    assert {item["payload"]["version"] for item in bundle["versions"] if item["payload"]["recognition_id"] == revised.id} == {1, 2}
    assert bundle["relations"] == []
    assert len(bundle["approved_relation_proposals"]) == 1
    assert result["summary"]["requested_experiences"] == 1
    assert {item["code"] for item in result["issues"]} >= {"missing_source", "external_provenance_reference"}
    after = {
        collection: tuple((item.object_id, item.revision, item.payload) for item in records.list(collection))
        for collection in before
    }
    assert after == before


def test_export_never_auto_adds_unallowed_history_sources(records, scope):
    service = RecognitionService(records)
    first = _published(service, scope, "recognition-one", "experience-one", "first conclusion")
    extra = service.stage_experience(scope=scope, experience_id="experience-two", content="later source")
    revised = service.revise(
        scope=scope, recognition_id=first.id, expected_revision=first.revision,
        content="first conclusion, revised", source_experience_ids=[extra],
    )

    result = MigrationBundleService(records).export(scope, [revised.id], ["experience-one"])

    assert [item["id"] for item in result["bundle"]["experiences"]] == ["experience-one"]
    missing = [item for item in result["issues"] if item["code"] == "missing_source"]
    assert {(item["source_id"], item["location"]) for item in missing} >= {("experience-two", "current")}


def test_validator_rejects_hidden_collections_and_forged_history(records, scope):
    service = RecognitionService(records)
    recognition = _published(service, scope, "recognition-one", "experience-one", "one conclusion")
    valid = MigrationBundleService(records).export(scope, [recognition.id], ["experience-one"])["bundle"]

    hidden = deepcopy(valid)
    hidden["documents"] = []
    with pytest.raises(MigrationBundleError, match="bundle fields"):
        validate_bundle(hidden)

    missing_history = deepcopy(valid)
    missing_history["versions"] = []
    with pytest.raises(MigrationBundleError, match="version history"):
        validate_bundle(missing_history)

    forged = deepcopy(valid)
    forged["recognitions"][0]["revision"] = True
    with pytest.raises(MigrationBundleError, match="revision"):
        validate_bundle(forged)


def test_validator_requires_internal_relation_endpoints_and_current_approved_revisions(records, scope):
    service = RecognitionService(records)
    first = _published(service, scope, "recognition-one", "experience-one", "one conclusion")
    second = _published(service, scope, "recognition-two", "experience-two", "two conclusion")
    proposal = RelationProposalService(records).propose(scope, first.id, second.id, "supports", "manual evidence")
    RelationProposalService(records).review(scope, proposal["id"], proposal["revision"], "approved")
    revised = service.revise(scope=scope, recognition_id=first.id, expected_revision=first.revision, content="one conclusion revised")
    valid = MigrationBundleService(records).export(scope, [revised.id, second.id], ["experience-one", "experience-two"])["bundle"]

    bad_endpoint = deepcopy(valid)
    bad_endpoint["approved_relation_proposals"][0]["payload"]["from_id"] = "recognition-missing"
    with pytest.raises(MigrationBundleError, match="endpoint"):
        validate_bundle(bad_endpoint)

    assert validate_bundle(valid)["approved_relation_proposals"][0]["payload"]["from_revision"] == 1
    assert any(item["code"] == "approved_relation_endpoint_revision_mismatch" for item in inspect_bundle(valid))


def test_inspect_bundle_reports_current_history_and_provenance_without_mutating_status(records, scope):
    service = RecognitionService(records)
    recognition = _published(service, scope, "recognition-one", "experience-one", "one conclusion")
    bundle = MigrationBundleService(records).export(scope, [recognition.id], ["experience-one"])["bundle"]
    bundle = deepcopy(bundle)
    bundle["recognitions"][0]["payload"]["source_recognition_ids"] = ["recognition-external"]
    bundle["recognitions"][0]["payload"]["source_recognition_revisions"] = {"recognition-external": 1}
    current = next(item for item in bundle["versions"] if item["payload"]["version"] == 1)
    current["payload"]["snapshot"] = deepcopy(bundle["recognitions"][0]["payload"])

    issues = inspect_bundle(bundle)

    assert any(item["code"] == "missing_source" and item["location"] == "current" for item in issues)
    assert any(item["code"] == "missing_source" and item["location"] == "history" for item in issues)


def test_validator_accepts_a_legal_composite_version_id_for_a_long_recognition_id(records, scope):
    service = RecognitionService(records)
    recognition = _published(service, scope, "recognition-one", "experience-one", "long ids remain portable")
    bundle = deepcopy(MigrationBundleService(records).export(scope, [recognition.id], ["experience-one"])["bundle"])
    recognition_id = "r" * 128
    bundle["recognitions"][0]["id"] = recognition_id
    bundle["recognitions"][0]["payload"]["id"] = recognition_id
    version = bundle["versions"][0]
    version["id"] = f"{recognition_id}~v1"
    version["payload"]["id"] = version["id"]
    version["payload"]["recognition_id"] = recognition_id
    version["payload"]["snapshot"]["id"] = recognition_id

    assert validate_bundle(bundle) == bundle


def test_history_source_revision_is_traced_through_selected_version_history(records, scope):
    service = RecognitionService(records)
    source = _published(service, scope, "recognition-source", "experience-source", "source one")
    dependent_experience = service.stage_experience(scope=scope, experience_id="experience-dependent", content="dependent evidence")
    candidate = service.propose(
        scope=scope, candidate_id="candidate-dependent", content="dependent conclusion",
        source_experience_ids=[dependent_experience], source_recognition_ids=[source.id],
    )
    dependent = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision,
                                reviewer="user-1", recognition_id="recognition-dependent")
    revised_source = service.revise(scope=scope, recognition_id=source.id, expected_revision=source.revision,
                                    content="source two")
    revoked_source = service.revoke(scope=scope, recognition_id=revised_source.id,
                                    expected_revision=revised_source.revision, reason="withdrawn")

    result = MigrationBundleService(records).export(
        scope, [revoked_source.id, dependent.id], ["experience-source", "experience-dependent"],
    )

    history_issue = [
        item for item in result["issues"]
        if item["record_id"] == f"{dependent.id}~v1" and item["source_id"] == source.id
    ]
    assert history_issue == []
    assert any(item["location"] == "current" and item["source_id"] == source.id for item in result["issues"])
    assert any(item["code"] == "source_not_active" and item["location"] == "current" for item in result["issues"])
