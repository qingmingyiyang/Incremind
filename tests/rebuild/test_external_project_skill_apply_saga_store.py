from pathlib import Path

import pytest

from core.storage_provider import (
    ExternalProjectSkillApplyEvidence,
    ExternalProjectSkillApplySagaConflict,
    SQLiteExternalProjectSkillApplySagaStore,
    SQLiteStructuredRecordStore,
)


def _store(root: Path):
    return SQLiteExternalProjectSkillApplySagaStore(SQLiteStructuredRecordStore(root / "records.sqlite3"))


def _evidence(payload="a" * 64):
    return ExternalProjectSkillApplyEvidence(
        namespace_id="default",
        project_id="project-alpha",
        project_skill_id="skill-alpha",
        base_revision=1,
        payload_sha256=payload,
        project_skill_authority_identity="sqlite:structured-records-v1",
    )


def test_prepare_is_durable_idempotent_and_rejects_evidence_drift(tmp_path: Path):
    store = _store(tmp_path)
    first = store.prepare(operation_id="draft-skill-001", evidence=_evidence(), now="2026-07-11T10:00:00Z")
    repeated = _store(tmp_path).prepare(operation_id="draft-skill-001", evidence=_evidence())
    assert repeated == first
    with pytest.raises(ExternalProjectSkillApplySagaConflict, match="evidence drifted"):
        store.prepare(operation_id="draft-skill-001", evidence=_evidence("b" * 64))


def test_transitions_use_cas_and_preserve_applied_revision(tmp_path: Path):
    store = _store(tmp_path)
    prepared = store.prepare(operation_id="draft-skill-001", evidence=_evidence())
    applied = store.mark_skill_applied(
        prepared.operation_id, expected_revision=prepared.revision, applied_skill_revision=2
    )
    finalized = store.finalize(applied.operation_id, expected_revision=applied.revision)
    assert (applied.state, applied.revision, applied.applied_skill_revision) == ("skill_applied", 2, 2)
    assert (finalized.state, finalized.revision, finalized.applied_skill_revision) == ("finalized", 3, 2)
    with pytest.raises(ExternalProjectSkillApplySagaConflict, match="expected revision 1, found 3"):
        store.mark_skill_applied(finalized.operation_id, expected_revision=1, applied_skill_revision=2)


def test_recoverable_list_excludes_finalized_and_keeps_bounded_states(tmp_path: Path):
    store = _store(tmp_path)
    prepared = store.prepare(operation_id="draft-skill-prepared", evidence=_evidence())
    other_evidence = ExternalProjectSkillApplyEvidence(
        "default", "project-beta", "skill-beta", 3, "b" * 64, "json:object-store-v1"
    )
    applied = store.prepare(operation_id="draft-skill-applied", evidence=other_evidence)
    applied = store.mark_skill_applied(applied.operation_id, expected_revision=applied.revision, applied_skill_revision=4)
    final = store.prepare(
        operation_id="draft-skill-finalized",
        evidence=ExternalProjectSkillApplyEvidence("default", "project-gamma", "skill-gamma", 5, "c" * 64, "json:object-store-v1"),
    )
    final = store.mark_skill_applied(final.operation_id, expected_revision=final.revision, applied_skill_revision=6)
    store.finalize(final.operation_id, expected_revision=final.revision)
    assert [(item.operation_id, item.state) for item in store.list_recoverable()] == [
        (applied.operation_id, "skill_applied"),
        (prepared.operation_id, "prepared"),
    ]
