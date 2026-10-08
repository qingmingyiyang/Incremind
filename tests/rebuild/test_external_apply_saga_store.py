from __future__ import annotations

from pathlib import Path

import pytest

from core.storage_provider import (
    ExternalApplyEvidence,
    ExternalApplySagaConflict,
    SQLiteExternalApplySagaStore,
    SQLiteStructuredRecordStore,
)


def _store(tmp_path: Path) -> SQLiteExternalApplySagaStore:
    return SQLiteExternalApplySagaStore(SQLiteStructuredRecordStore(tmp_path / "structured-records.sqlite3"))


def _evidence(*, payload_sha256: str = "a" * 64) -> ExternalApplyEvidence:
    return ExternalApplyEvidence(
        namespace_id="default",
        document_id="document-saga-001",
        base_revision=1,
        payload_sha256=payload_sha256,
    )


def test_prepare_is_durable_and_idempotent_for_identical_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)

    first = store.prepare(
        operation_id="draft-saga-001",
        evidence=_evidence(),
        now="2026-07-11T10:00:00+00:00",
    )
    repeated = store.prepare(
        operation_id="draft-saga-001",
        evidence=_evidence(),
        now="2026-07-11T11:00:00+00:00",
    )

    assert first == repeated
    assert first.state == "prepared"
    assert first.revision == 1
    assert first.created_at == "2026-07-11T10:00:00+00:00"
    assert _store(tmp_path).get(first.operation_id) == first


def test_prepare_rejects_payload_evidence_drift(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.prepare(operation_id="draft-saga-001", evidence=_evidence())

    with pytest.raises(ExternalApplySagaConflict, match="evidence drifted"):
        store.prepare(
            operation_id="draft-saga-001",
            evidence=_evidence(payload_sha256="b" * 64),
        )


def test_operation_transitions_by_cas_and_preserves_application_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    prepared = store.prepare(
        operation_id="draft-saga-001",
        evidence=_evidence(),
        now="2026-07-11T10:00:00+00:00",
    )
    applied = store.mark_document_applied(
        prepared.operation_id,
        expected_revision=prepared.revision,
        applied_document_revision=2,
        now="2026-07-11T10:01:00+00:00",
    )
    finalized = store.finalize(
        applied.operation_id,
        expected_revision=applied.revision,
        now="2026-07-11T10:02:00+00:00",
    )

    assert applied.state == "document_applied"
    assert applied.revision == 2
    assert applied.applied_document_revision == 2
    assert finalized.state == "finalized"
    assert finalized.revision == 3
    assert finalized.applied_document_revision == 2
    assert finalized.evidence == prepared.evidence
    assert finalized.created_at == prepared.created_at


def test_stale_and_illegal_transitions_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    prepared = store.prepare(operation_id="draft-saga-001", evidence=_evidence())
    with pytest.raises(ExternalApplySagaConflict, match="illegal external apply transition"):
        store.finalize(prepared.operation_id, expected_revision=prepared.revision)
    applied = store.mark_document_applied(
        prepared.operation_id,
        expected_revision=prepared.revision,
        applied_document_revision=2,
    )

    with pytest.raises(ExternalApplySagaConflict, match="expected revision 1, found 2"):
        store.mark_document_applied(
            prepared.operation_id,
            expected_revision=prepared.revision,
            applied_document_revision=2,
        )
    with pytest.raises(ExternalApplySagaConflict, match="illegal external apply transition"):
        store.mark_document_applied(
            applied.operation_id,
            expected_revision=applied.revision,
            applied_document_revision=3,
        )


def test_list_recoverable_excludes_finalized_operations(tmp_path: Path) -> None:
    store = _store(tmp_path)
    prepared = store.prepare(operation_id="draft-saga-prepared", evidence=_evidence())
    second = store.prepare(
        operation_id="draft-saga-applied",
        evidence=ExternalApplyEvidence("default", "document-saga-002", 3, "b" * 64),
    )
    store.mark_document_applied(
        second.operation_id,
        expected_revision=second.revision,
        applied_document_revision=4,
    )
    third = store.prepare(
        operation_id="draft-saga-finalized",
        evidence=ExternalApplyEvidence("default", "document-saga-003", 5, "c" * 64),
    )
    third_applied = store.mark_document_applied(
        third.operation_id,
        expected_revision=third.revision,
        applied_document_revision=6,
    )
    store.finalize(third.operation_id, expected_revision=third_applied.revision)

    recoverable = store.list_recoverable()

    assert [(item.operation_id, item.state) for item in recoverable] == [
        ("draft-saga-applied", "document_applied"),
        (prepared.operation_id, "prepared"),
    ]
