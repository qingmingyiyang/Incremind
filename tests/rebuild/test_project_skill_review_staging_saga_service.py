from __future__ import annotations

import pytest

from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.project_skill_core import (
    ProjectSkillReviewStagingSagaService,
    ProjectSkillReviewStagingServiceConflict,
)
from core.storage_provider import (
    JsonObjectStore,
    SQLiteProjectSkillReviewStagingSagaStore,
    SQLiteStructuredRecordStore,
)


def _candidate(candidate_id: str = "candidate-project-skill-alpha") -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": candidate_id,
        "project_id": "project-alpha",
        "target_layer": "project_skill",
        "candidate_type": "document_takeaway",
        "status": "pending_review",
        "proposed_content": "项目技能必须保留来源，并允许用户确认后再发布。",
        "source_refs": [{"source_id": "source-alpha", "locator": "char:0-48"}],
        "provenance": {
            "model_result_id": None,
            "model_request_id": None,
            "recall_result_id": None,
            "document_id": "document-alpha",
            "document_revision": 1,
            "input_refs": [
                {
                    "kind": "document",
                    "object_id": "document-alpha",
                    "uri": "crp://default/documents/document-alpha.json",
                }
            ],
        },
        "review": {
            "requires_user_confirmation": True,
            "auto_promote_allowed": False,
            "reason": "等待用户确认。",
            "reviewed_by": None,
            "reviewed_at": None,
        },
        "created_at": "2026-07-12T16:00:00+08:00",
        "updated_at": "2026-07-12T16:00:00+08:00",
    }


def _service(tmp_path):
    candidates = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    operations = SQLiteProjectSkillReviewStagingSagaStore(records)
    return candidates, records, operations, ProjectSkillReviewStagingSagaService(candidates, records, operations)


def _saved_candidate(candidates: JsonObjectStore, candidate_id: str = "candidate-project-skill-alpha") -> str:
    ObjectStoreMemoryCandidateRepository(candidates).save(_candidate(candidate_id))
    return candidate_id


def test_review_to_staging_finalizes_once_without_long_term_publication(tmp_path) -> None:
    candidates, records, operations, service = _service(tmp_path)
    candidate_id = _saved_candidate(candidates)

    first = service.review_to_staging(
        candidate_id,
        review_reason="用户确认形成可发布的 Project Skill 草案。",
        reviewed_at="2026-07-12T16:05:00+08:00",
    )
    replay = service.review_to_staging(
        candidate_id,
        review_reason="用户确认形成可发布的 Project Skill 草案。",
        reviewed_at="2026-07-12T16:05:00+08:00",
    )

    candidate = candidates.read("memory_candidates", candidate_id)
    assert first.state == replay.state == "finalized"
    assert candidates.revision("memory_candidates", candidate_id) == 2
    assert candidate is not None
    assert candidate["status"] == "promoted"
    assert candidate["application"] == {
        "operation_id": first.operation_id,
        "draft_id": first.evidence.draft_id,
        "staging_authority": "sqlite:structured-records-v1",
    }
    staged = records.read("staging_project_skills", first.evidence.draft_id)
    assert staged is not None
    assert staged.revision == 1
    assert staged.payload == first.draft
    assert records.read("project_skills", "skill-project-alpha") is None
    assert records.list("memory_publications") == ()
    assert records.list("memory_transitions") == ()
    assert operations.get(first.operation_id).state == "finalized"


def test_prepared_operation_recovers_with_durable_draft_payload(tmp_path) -> None:
    candidates, records, operations, service = _service(tmp_path)
    candidate_id = _saved_candidate(candidates)
    prepared = service.prepare_review(
        candidate_id,
        review_reason="用户确认草案，允许恢复。",
        reviewed_at="2026-07-12T16:10:00+08:00",
    )

    assert prepared.state == "prepared"
    assert records.read("staging_project_skills", prepared.evidence.draft_id) is None
    recovered = ProjectSkillReviewStagingSagaService(candidates, records, operations).resume(
        prepared.operation_id
    )

    assert recovered.state == "finalized"
    assert records.read("staging_project_skills", prepared.evidence.draft_id) is not None
    assert candidates.revision("memory_candidates", candidate_id) == 2


def test_candidate_drift_after_prepare_fails_closed_without_staging(tmp_path) -> None:
    candidates, records, operations, service = _service(tmp_path)
    candidate_id = _saved_candidate(candidates)
    prepared = service.prepare_review(
        candidate_id,
        review_reason="等待后续审核。",
        reviewed_at="2026-07-12T16:15:00+08:00",
    )
    drifted = candidates.read("memory_candidates", candidate_id)
    assert drifted is not None
    drifted["proposed_content"] = "已被并发修改的候选内容。"
    candidates.write("memory_candidates", candidate_id, drifted, expected_revision=1)

    with pytest.raises(ProjectSkillReviewStagingServiceConflict, match="pending candidate evidence drifted"):
        service.resume(prepared.operation_id)
    assert records.read("staging_project_skills", prepared.evidence.draft_id) is None
    assert operations.get(prepared.operation_id).state == "prepared"


def test_current_skill_identity_drift_after_prepare_fails_closed(tmp_path) -> None:
    candidates, records, operations, service = _service(tmp_path)
    candidate_id = _saved_candidate(candidates)
    prepared = service.prepare_review(
        candidate_id,
        review_reason="等待 current Skill 确认。",
        reviewed_at="2026-07-12T16:20:00+08:00",
    )
    with records.begin() as uow:
        uow.put(
            "project_skill_index",
            "project-alpha",
            {"project_id": "project-alpha", "skill_id": "skill-project-alpha"},
            expected_revision=0,
        )
        uow.put(
            "project_skills",
            "skill-project-alpha",
            {"id": "skill-project-alpha", "project_id": "project-alpha", "revision": 1},
            expected_revision=0,
        )
        uow.commit()

    with pytest.raises(ProjectSkillReviewStagingServiceConflict, match="target drifted"):
        service.resume(prepared.operation_id)
    assert records.read("staging_project_skills", prepared.evidence.draft_id) is None
    assert candidates.revision("memory_candidates", candidate_id) == 1
    assert operations.get(prepared.operation_id).state == "prepared"


def test_ai_candidate_generation_baseline_must_match_current_project_skill(tmp_path) -> None:
    candidates, records, _, service = _service(tmp_path)
    candidate = _candidate("candidate-stale-ai-project-skill")
    candidate["expected_project_skill_revision"] = 0
    ObjectStoreMemoryCandidateRepository(candidates).save(candidate)
    with records.begin() as uow:
        uow.put(
            "project_skill_index",
            "project-alpha",
            {"project_id": "project-alpha", "skill_id": "skill-project-alpha"},
            expected_revision=0,
        )
        uow.put(
            "project_skills",
            "skill-project-alpha",
            {"id": "skill-project-alpha", "project_id": "project-alpha", "revision": 1},
            expected_revision=0,
        )
        uow.commit()

    with pytest.raises(ProjectSkillReviewStagingServiceConflict, match="generation baseline is stale"):
        service.prepare_review(
            "candidate-stale-ai-project-skill",
            review_reason="不得覆盖新的用户修改。",
            reviewed_at="2026-07-12T16:22:00+08:00",
        )

    assert records.list("staging_project_skills") == ()
    assert candidates.read("memory_candidates", "candidate-stale-ai-project-skill")["status"] == "pending_review"


def test_matching_staging_replay_is_idempotent_and_candidate_application_drift_fails(tmp_path) -> None:
    candidates, records, _, service = _service(tmp_path)
    candidate_id = _saved_candidate(candidates)
    prepared = service.prepare_review(
        candidate_id,
        review_reason="已有 staging draft 的安全重放。",
        reviewed_at="2026-07-12T16:25:00+08:00",
    )
    with records.begin() as uow:
        uow.put("staging_project_skills", prepared.evidence.draft_id, prepared.draft, expected_revision=0)
        uow.commit()

    finalized = service.resume(prepared.operation_id)
    assert finalized.state == "finalized"
    assert records.read("staging_project_skills", prepared.evidence.draft_id).revision == 1
    drifted = candidates.read("memory_candidates", candidate_id)
    assert drifted is not None
    drifted["application"] = {**drifted["application"], "draft_id": "project-skill-publication-draft-drifted"}
    candidates.write("memory_candidates", candidate_id, drifted, expected_revision=2)

    with pytest.raises(ProjectSkillReviewStagingServiceConflict, match="reviewed candidate revision drifted"):
        service.resume(prepared.operation_id)


def test_candidate_application_contract_requires_promoted_status(tmp_path) -> None:
    candidates, _, _, _ = _service(tmp_path)
    candidate_id = _saved_candidate(candidates)
    repository = ObjectStoreMemoryCandidateRepository(candidates)
    pending = repository.get(candidate_id)
    assert pending is not None
    pending["application"] = {
        "operation_id": "review-stage-alpha",
        "draft_id": "project-skill-publication-draft-alpha",
        "staging_authority": "sqlite:structured-records-v1",
    }

    with pytest.raises(ValueError, match="requires promoted status"):
        repository.update(pending)


def test_compensation_replays_after_draft_delete_before_terminal_write(
    tmp_path, monkeypatch,
) -> None:
    candidates, records, operations, service = _service(tmp_path)
    candidate_id = _saved_candidate(candidates, "candidate-compensation-replay")
    prepared = service.prepare_review(
        candidate_id,
        review_reason="验证跨存储补偿重放。",
        reviewed_at="2026-07-12T16:30:00+08:00",
    )
    service._stage_draft(prepared)
    staged = operations.mark_sqlite_draft_staged(
        prepared.operation_id, expected_revision=prepared.revision
    )
    original = SQLiteProjectSkillReviewStagingSagaStore.mark_compensated
    attempts = 0

    def fail_once(store, operation_id, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated process loss before terminal write")
        return original(store, operation_id, **kwargs)

    monkeypatch.setattr(
        SQLiteProjectSkillReviewStagingSagaStore, "mark_compensated", fail_once
    )
    with pytest.raises(RuntimeError, match="simulated process loss"):
        service.compensate(
            staged.operation_id, compensation_code="review_evidence_conflict"
        )

    assert records.read("staging_project_skills", prepared.evidence.draft_id) is None
    assert operations.get(staged.operation_id).state == "sqlite_draft_staged"
    replayed = service.compensate(
        staged.operation_id, compensation_code="review_evidence_conflict"
    )
    assert replayed.state == "compensated"
    assert replayed.compensation_code == "review_evidence_conflict"


def test_compensation_fails_closed_after_candidate_review(tmp_path) -> None:
    candidates, records, operations, service = _service(tmp_path)
    candidate_id = _saved_candidate(candidates, "candidate-reviewed-boundary")
    prepared = service.prepare_review(
        candidate_id,
        review_reason="验证补偿边界。",
        reviewed_at="2026-07-12T16:35:00+08:00",
    )
    service._stage_draft(prepared)
    staged = operations.mark_sqlite_draft_staged(
        prepared.operation_id, expected_revision=prepared.revision
    )
    service._review_candidate(staged)

    with pytest.raises(ProjectSkillReviewStagingServiceConflict, match="reviewed candidate"):
        service.compensate(
            staged.operation_id, compensation_code="review_evidence_conflict"
        )
    assert records.read("staging_project_skills", prepared.evidence.draft_id) is not None
    assert candidates.read("memory_candidates", candidate_id)["status"] == "promoted"


def test_missing_staging_draft_never_promotes_candidate(tmp_path) -> None:
    candidates, records, operations, service = _service(tmp_path)
    candidate_id = _saved_candidate(candidates, "candidate-missing-staging")
    prepared = service.prepare_review(
        candidate_id,
        review_reason="验证候选副作用前的 staging 边界。",
        reviewed_at="2026-07-12T16:40:00+08:00",
    )
    staged = operations.mark_sqlite_draft_staged(
        prepared.operation_id, expected_revision=prepared.revision
    )

    with pytest.raises(ProjectSkillReviewStagingServiceConflict, match="staging draft is missing"):
        service.resume(staged.operation_id)
    assert records.read("staging_project_skills", prepared.evidence.draft_id) is None
    assert candidates.read("memory_candidates", candidate_id)["status"] == "pending_review"
    assert candidates.revision("memory_candidates", candidate_id) == 1
