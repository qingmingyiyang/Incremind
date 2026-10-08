from __future__ import annotations

import pytest

from backend.recognition import ExperienceProvenance, Recognition, RecognitionError, RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def service(tmp_path):
    return RecognitionService(SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3"))


@pytest.fixture
def scope():
    return WorkScope("user-1", "project-1")


def test_user_statement_round_trips_with_authoritative_recorded_time(service, scope):
    experience_id = service.stage_experience(
        scope=scope,
        content="用户确认网页优先。",
        provenance={
            "kind": "user_statement",
            "actor": "local-user",
            "occurred_at": "2026-09-15T08:30:00+08:00",
            "recorded_at": "2000-01-01T00:00:00+00:00",
            "source_refs": [{"type": "turn", "id": "turn-1", "revision": 2}],
        },
    )

    item = service.list_experiences(scope=scope)[0]

    assert item.id == experience_id
    assert item.provenance.kind == "user_statement"
    assert item.provenance.epistemic_status == "user_asserted"
    assert item.provenance.recorded_at != "2000-01-01T00:00:00+00:00"
    assert item.provenance.to_payload()["source_refs"] == [{"type": "turn", "id": "turn-1", "revision": 2}]


def test_old_record_without_provenance_reads_as_legacy_without_inferring_content(service, scope):
    with service.records.begin() as uow:
        uow.put(
            "recognition_experiences",
            "experience-old",
            {
                "id": "experience-old", "scope": {"user_id": "user-1", "project_id": "project-1"},
                "project_id": "project-1", "content": "模型说部署成功", "state": "active",
                "created_at": "2026-09-15T00:00:00+00:00", "revoked_at": None,
            },
            expected_revision=0,
        )
        uow.commit()

    item = service.list_experiences(scope=scope)[0]

    assert item.provenance.to_payload() == {
        "kind": "legacy_unspecified", "epistemic_status": "unknown", "actor": "unknown",
        "source_refs": [], "recorded_at": "2026-09-15T00:00:00+00:00",
    }


def test_model_artifact_claiming_deployment_remains_unverified_and_outcome_unknown(service, scope):
    service.stage_experience(
        scope=scope,
        content="模型声称部署成功。",
        provenance={
            "kind": "model_generated_artifact", "actor": "recognition-runner",
            "source_refs": [{"type": "document", "id": "document-result", "revision": 3}],
        },
    )

    provenance = service.list_experiences(scope=scope)[0].provenance

    assert provenance.epistemic_status == "unverified"
    assert provenance.artifact_status == "committed"
    assert provenance.outcome_status == "unknown"


@pytest.mark.parametrize(
    "provenance",
    [
        {"kind": "verified", "source_refs": []},
        {"kind": "user_statement", "source_refs": [{"type": "receipt", "id": "r-1"}]},
        {"kind": "user_statement", "source_refs": [{"type": "task", "id": "task-1", "revision": 0}]},
    ],
)
def test_invalid_kind_or_source_reference_is_rejected(service, scope, provenance):
    with pytest.raises(RecognitionError):
        service.stage_experience(scope=scope, content="保留经历", provenance=provenance)


def test_recognition_projection_includes_known_source_revisions(service, scope):
    experience_id = service.stage_experience(scope=scope, content="来源经历")
    candidate = service.propose(scope=scope, content="来源认识", source_experience_ids=[experience_id])
    recognition = service.publish(
        scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer="user-1"
    )

    assert recognition.source_refs == ({"type": "experience", "id": experience_id, "revision": 1},)
    assert recognition.retrieval_projection()["source_refs"] == [
        {"type": "experience", "id": experience_id, "revision": 1}
    ]


def test_recognition_projection_keeps_missing_source_revision_unknown():
    recognition = Recognition(
        "recognition-1", 1, WorkScope("user-1", "project-1"), "来源认识", "active",
        ("experience-1",), (), (), {}, {}, (),
    )

    assert recognition.source_refs == ({"type": "experience", "id": "experience-1"},)


def test_provenance_class_forces_status_from_kind():
    provenance = ExperienceProvenance.from_payload(
        {
            "kind": "user_statement", "epistemic_status": "verified", "source_refs": [],
            "recorded_at": "2026-09-15T00:00:00+00:00",
        }
    )

    assert provenance.epistemic_status == "user_asserted"
