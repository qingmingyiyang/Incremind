from __future__ import annotations

import pytest

from core.storage_provider.project_skill_review_staging_saga import (
    ProjectSkillReviewStagingEvidence,
    ProjectSkillReviewStagingSagaConflict,
    SQLiteProjectSkillReviewStagingSagaStore,
)
from core.storage_provider import SQLiteStructuredRecordStore


def _evidence() -> ProjectSkillReviewStagingEvidence:
    return ProjectSkillReviewStagingEvidence("default", "candidate-alpha", 1, "a" * 64, "project-alpha", "skill-project-alpha", 0, "project-skill-publication-draft-aaaaaaaaaaaaaaaaaaaaaaaa", "b" * 64)


def _draft() -> dict[str, object]:
    return {
        "id": "project-skill-publication-draft-aaaaaaaaaaaaaaaaaaaaaaaa",
        "draft_digest": "b" * 64,
        "nested": {"label": "immutable"},
    }


def test_review_staging_saga_requires_ordered_cas_and_replays_prepare(tmp_path) -> None:
    store = SQLiteProjectSkillReviewStagingSagaStore(SQLiteStructuredRecordStore(tmp_path / "records.sqlite3"))
    first = store.prepare(operation_id="review-stage-alpha", evidence=_evidence(), draft=_draft(), now="2026-07-12T15:00:00+08:00")
    assert store.prepare(operation_id="review-stage-alpha", evidence=_evidence(), draft=_draft()) == first
    with pytest.raises(ProjectSkillReviewStagingSagaConflict, match="evidence drifted"):
        store.prepare(operation_id="review-stage-alpha", evidence=ProjectSkillReviewStagingEvidence("default", "candidate-alpha", 2, "a" * 64, "project-alpha", "skill-project-alpha", 0, "project-skill-publication-draft-aaaaaaaaaaaaaaaaaaaaaaaa", "b" * 64), draft=_draft())
    drifted_draft = _draft()
    drifted_draft["nested"] = {"label": "drifted"}
    with pytest.raises(ProjectSkillReviewStagingSagaConflict, match="evidence drifted"):
        store.prepare(operation_id="review-stage-alpha", evidence=_evidence(), draft=drifted_draft)
    staged = store.mark_sqlite_draft_staged("review-stage-alpha", expected_revision=1)
    reviewed = store.mark_candidate_reviewed("review-stage-alpha", expected_revision=staged.revision)
    finalized = store.finalize("review-stage-alpha", expected_revision=reviewed.revision)
    assert finalized.state == "finalized"
    assert store.list_recoverable() == ()
    with pytest.raises(ProjectSkillReviewStagingSagaConflict, match="illegal"):
        store.finalize("review-stage-alpha", expected_revision=finalized.revision)
