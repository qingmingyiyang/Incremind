from __future__ import annotations

import hashlib
import json

import pytest

from core.storage_provider import (
    MemoryPublicationReviewStagingEvidence,
    MemoryPublicationReviewStagingSagaConflict,
    MemoryPublicationReviewStagingSagaError,
    SQLiteMemoryPublicationReviewStagingSagaStore,
    SQLiteStructuredRecordStore,
    memory_publication_review_staging_operation_id,
)


def _digest(value: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _inputs() -> tuple[MemoryPublicationReviewStagingEvidence, dict[str, object], dict[str, object], dict[str, object]]:
    candidate = {"id": "candidate-alpha", "status": "pending_review"}
    draft = {"id": "atom-draft-alpha", "revision": 1}
    context = {"id": "atom~atom-draft-alpha", "review_reason": "confirm"}
    evidence = MemoryPublicationReviewStagingEvidence(
        namespace_id="default",
        candidate_id="candidate-alpha",
        candidate_revision=1,
        candidate_sha256=_digest(candidate),
        layer="atom",
        draft_id="atom-draft-alpha",
        draft_sha256=_digest(draft),
        context_id="atom~atom-draft-alpha",
        context_sha256=_digest(context),
    )
    return evidence, candidate, draft, context


def test_review_staging_operation_is_durable_cas_state_machine(tmp_path) -> None:
    store = SQLiteMemoryPublicationReviewStagingSagaStore(
        SQLiteStructuredRecordStore(tmp_path / "structured-records.sqlite3")
    )
    evidence, candidate, draft, context = _inputs()
    operation_id = memory_publication_review_staging_operation_id(evidence)

    prepared = store.prepare(
        operation_id=operation_id,
        evidence=evidence,
        candidate=candidate,
        draft=draft,
        context=context,
        now="2026-07-12T19:00:00+08:00",
    )
    replay = store.prepare(
        operation_id=operation_id,
        evidence=evidence,
        candidate=candidate,
        draft=draft,
        context=context,
        now="2026-07-12T19:01:00+08:00",
    )
    staged = store.mark_sqlite_staging_created(operation_id, expected_revision=prepared.revision)
    reviewed = store.mark_candidate_reviewed(operation_id, expected_revision=staged.revision)
    finalized = store.finalize(operation_id, expected_revision=reviewed.revision)

    assert replay == prepared
    assert finalized.state == "finalized"
    assert store.list_recoverable() == ()


def test_review_staging_operation_rejects_payload_or_transition_drift(tmp_path) -> None:
    store = SQLiteMemoryPublicationReviewStagingSagaStore(
        SQLiteStructuredRecordStore(tmp_path / "structured-records.sqlite3")
    )
    evidence, candidate, draft, context = _inputs()
    operation_id = memory_publication_review_staging_operation_id(evidence)
    prepared = store.prepare(
        operation_id=operation_id,
        evidence=evidence,
        candidate=candidate,
        draft=draft,
        context=context,
    )

    with pytest.raises(MemoryPublicationReviewStagingSagaError, match="evidence drifted"):
        store.prepare(
            operation_id=operation_id,
            evidence=evidence,
            candidate={**candidate, "status": "promoted"},
            draft=draft,
            context=context,
        )
    with pytest.raises(MemoryPublicationReviewStagingSagaConflict, match="illegal"):
        store.finalize(operation_id, expected_revision=prepared.revision)
