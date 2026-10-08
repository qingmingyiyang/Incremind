from pathlib import Path

import pytest

from core.storage_provider import (
    ExternalSeriesApplyEvidence,
    ExternalSeriesApplySagaConflict,
    SQLiteExternalSeriesApplySagaStore,
    SQLiteStructuredRecordStore,
)


def _store(root: Path):
    return SQLiteExternalSeriesApplySagaStore(SQLiteStructuredRecordStore(root / "records.sqlite3"))


def _evidence(**changes):
    values = {
        "namespace_id": "default", "series_id": "series-alpha", "series_memory_id": "series-memory-alpha",
        "base_object_revision": 1, "base_series_revision": 3, "payload_sha256": "a" * 64,
        "authority_identity": "json:object-store-v1",
    }
    values.update(changes)
    return ExternalSeriesApplyEvidence(**values)


def test_prepare_is_durable_idempotent_and_binds_both_revisions(tmp_path: Path):
    store = _store(tmp_path)
    first = store.prepare(operation_id="draft-series-001", evidence=_evidence(), now="2026-07-11T10:00:00Z")
    repeated = _store(tmp_path).prepare(operation_id="draft-series-001", evidence=_evidence())
    assert repeated == first
    assert first.evidence.base_object_revision == 1
    assert first.evidence.base_series_revision == 3


@pytest.mark.parametrize("change", [
    {"base_object_revision": 2}, {"base_series_revision": 4}, {"series_id": "series-beta"},
    {"series_memory_id": "series-memory-beta"}, {"payload_sha256": "b" * 64},
])
def test_prepare_rejects_any_evidence_drift(tmp_path: Path, change):
    store = _store(tmp_path)
    store.prepare(operation_id="draft-series-001", evidence=_evidence())
    with pytest.raises(ExternalSeriesApplySagaConflict, match="evidence drifted"):
        store.prepare(operation_id="draft-series-001", evidence=_evidence(**change))


def test_transitions_use_cas_and_preserve_applied_domain_revision(tmp_path: Path):
    store = _store(tmp_path)
    prepared = store.prepare(operation_id="draft-series-001", evidence=_evidence())
    applied = store.mark_series_applied(prepared.operation_id, expected_revision=1, applied_series_revision=4)
    finalized = store.finalize(applied.operation_id, expected_revision=2)
    assert (applied.state, applied.revision, applied.applied_series_revision) == ("series_applied", 2, 4)
    assert (finalized.state, finalized.revision, finalized.applied_series_revision) == ("finalized", 3, 4)
    with pytest.raises(ExternalSeriesApplySagaConflict, match="expected revision 1, found 3"):
        store.mark_series_applied(finalized.operation_id, expected_revision=1, applied_series_revision=4)


def test_recoverable_list_excludes_finalized(tmp_path: Path):
    store = _store(tmp_path)
    prepared = store.prepare(operation_id="draft-series-prepared", evidence=_evidence())
    applied = store.prepare(operation_id="draft-series-applied", evidence=_evidence(series_id="series-b", series_memory_id="series-memory-b", payload_sha256="b" * 64))
    applied = store.mark_series_applied(applied.operation_id, expected_revision=1, applied_series_revision=4)
    final = store.prepare(operation_id="draft-series-final", evidence=_evidence(series_id="series-c", series_memory_id="series-memory-c", payload_sha256="c" * 64))
    final = store.mark_series_applied(final.operation_id, expected_revision=1, applied_series_revision=4)
    store.finalize(final.operation_id, expected_revision=2)
    assert [(item.operation_id, item.state) for item in store.list_recoverable()] == [
        (applied.operation_id, "series_applied"), (prepared.operation_id, "prepared")
    ]
