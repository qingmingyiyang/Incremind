from pathlib import Path

import pytest

from core.storage_provider import (
    SharedTrustAuditActivationSagaConflict,
    SQLiteSharedTrustAuditActivationSagaStore,
    SQLiteStructuredRecordStore,
)


def _store(tmp_path: Path):
    return SQLiteSharedTrustAuditActivationSagaStore(SQLiteStructuredRecordStore(tmp_path / "records.sqlite3"))


def _evidence(**overrides):
    value = {"namespace_id": "default", "activation_id": "activation-v1", "member_migrations": {key: f"{key}-v1" for key in ("memory_atoms", "memory_publications", "memory_scenarios", "memory_series_memory", "memory_transitions", "project_skills")}, "source_fingerprint": "a" * 64, "target_fingerprint": "b" * 64, "target_identity": "sqlite:structured-records-v1"}
    return {**value, **overrides}


def test_activation_operation_is_idempotent_and_recoverable(tmp_path):
    store = _store(tmp_path)
    prepared = store.prepare(_evidence(), now="2026-07-12T00:00:00Z")
    assert store.prepare(_evidence()) == prepared
    attested = store.advance(prepared.operation_id, prepared.revision, "attestation_written")
    active = store.advance(attested.operation_id, attested.revision, "authorities_activated")
    assert store.list_recoverable() == (active,)
    final = store.advance(active.operation_id, active.revision, "finalized")
    assert final.state == "finalized"
    assert store.list_recoverable() == ()


def test_activation_operation_rejects_drift_and_illegal_transitions(tmp_path):
    store = _store(tmp_path)
    prepared = store.prepare(_evidence())
    with pytest.raises(SharedTrustAuditActivationSagaConflict, match="illegal"):
        store.advance(prepared.operation_id, prepared.revision, "authorities_activated")
    with pytest.raises(SharedTrustAuditActivationSagaConflict, match="drifted"):
        store.prepare(_evidence(target_fingerprint="c" * 64))
