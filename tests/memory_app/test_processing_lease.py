from __future__ import annotations

import pytest

from backend.memory_app.processing_lease import ProcessingLease, ProcessingLeaseConflict
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore


COLLECTION = "workspace_items"


def setup(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    with records.begin() as tx:
        tx.put(COLLECTION, "item-a", {"id": "item-a", "project_id": "project-a",
                                       "status": "staged", "source_text": "original"}, expected_revision=0)
        tx.commit()
    now = [100.0]
    first = ProcessingLease(records, COLLECTION, "instance-a", lambda: now[0], ttl_seconds=10)
    second = ProcessingLease(records, COLLECTION, "instance-b", lambda: now[0], ttl_seconds=10)
    return records, now, first, second


def claim(lease, revision=1, run="run-a"):
    return lease.claim("item-a", "project-a", revision, run,
                       {"run_id": run}, {"status": "processing"})


def test_claim_cas_receipts_and_terminal_clear(tmp_path):
    records, _, first, _ = setup(tmp_path)
    claimed = claim(first)
    assert claimed["revision"] == 2
    assert claimed["processing_instance_id"] == "instance-a"
    assert claimed["remote_processing_receipts"] == [{"run_id": "run-a"}]
    assert first.guard("item-a", "project-a", "run-a")["revision"] == 2
    with pytest.raises(ProcessingLeaseConflict) as exc:
        claim(first)
    assert exc.value.code == "record_revision_conflict"
    ready = first.apply("item-a", "project-a", "run-a", {"status": "ready", "draft": "done"})
    assert ready["revision"] == 3
    assert ready["processing_run_id"] is None
    assert ready["processing_lease_expires_at"] is None
    assert records.read(COLLECTION, "item-a").payload["source_text"] == "original"
    with pytest.raises(ProcessingLeaseConflict):
        first.guard("item-a", "project-a", "run-a")


def test_same_pid_other_instance_is_preserved_until_expiry(tmp_path):
    records, now, first, second = setup(tmp_path)
    claim(first)
    assert second.recover_expired() == 0
    assert records.read(COLLECTION, "item-a").payload["status"] == "processing"
    assert second.heartbeat("item-a", "project-a", "run-a") is False
    now[0] = 111
    assert second.recover_expired() == 1
    failed = records.read(COLLECTION, "item-a")
    assert failed.payload["status"] == "failed"
    assert failed.payload["error"] == "processing_interrupted"
    assert failed.payload["processing_instance_id"] is None
    assert second.recover_expired() == 0


def test_heartbeat_extends_lease_and_old_run_cannot_change_new_run(tmp_path):
    records, now, first, _ = setup(tmp_path)
    claim(first)
    now[0] = 108
    assert first.heartbeat("item-a", "project-a", "run-a") is True
    now[0] = 111
    assert first.recover_expired() == 0
    assert first.interrupt("item-a", "project-a", "run-a") is True
    assert first.interrupt("item-a", "project-a", "run-a") is False
    revision = records.read(COLLECTION, "item-a").revision
    claim(first, revision, "run-b")
    before = records.read(COLLECTION, "item-a").revision
    assert first.heartbeat("item-a", "project-a", "run-a") is False
    assert first.interrupt("item-a", "project-a", "run-a") is False
    with pytest.raises(ProcessingLeaseConflict) as exc:
        first.apply("item-a", "project-a", "run-a", {"status": "ready"})
    assert exc.value.code == "processing_run_changed"
    assert records.read(COLLECTION, "item-a").revision == before
    assert records.read(COLLECTION, "item-a").payload["processing_run_id"] == "run-b"


def test_legacy_and_corrupt_processing_records_are_recovered_once(tmp_path):
    records, _, first, _ = setup(tmp_path)
    with records.begin() as tx:
        tx.put(COLLECTION, "old", {"project_id": "project-a", "status": "processing",
                                   "processing_pid": 123}, expected_revision=0)
        tx.put(COLLECTION, "corrupt", {"project_id": "project-a", "status": "processing",
                                       "processing_instance_id": "instance-z",
                                       "processing_run_id": "run-z",
                                       "processing_heartbeat_at": 99,
                                       "processing_lease_expires_at": "future"}, expected_revision=0)
        tx.put(COLLECTION, "unbounded", {"project_id": "project-a", "status": "processing",
                                         "processing_instance_id": "instance-z",
                                         "processing_run_id": "run-z",
                                         "processing_heartbeat_at": 99,
                                         "processing_lease_expires_at": 100000}, expected_revision=0)
        tx.commit()
    assert first.recover_expired() == 3
    assert first.recover_expired() == 0
    for item_id in ("old", "corrupt", "unbounded"):
        row = records.read(COLLECTION, item_id)
        assert row.revision == 2
        assert row.payload["error"] == "processing_interrupted"


def test_recovery_with_no_expired_candidate_never_begins_write_transaction(tmp_path, monkeypatch):
    records, _, first, _ = setup(tmp_path)
    claim(first)
    def unexpected():
        raise AssertionError("recovery requested a write transaction")
    monkeypatch.setattr(records, "begin", unexpected)
    assert first.recover_expired() == 0


def test_recovery_rechecks_renewal_inside_transaction(tmp_path, monkeypatch):
    records, now, first, _ = setup(tmp_path)
    claim(first)
    now[0] = 105
    assert first.heartbeat('item-a', 'project-a', 'run-a')
    now[0] = 111
    read = records.read
    def stale_discovery(collection, object_id):
        # Discovery raced a committed renewal; the transaction must see it.
        if collection == 'workspace_processing_heartbeats':
            return None
        return read(collection, object_id)
    monkeypatch.setattr(records, 'read', stale_discovery)
    assert first.recover_expired() == 0
    assert first.guard('item-a', 'project-a', 'run-a')['status'] == 'processing'


def test_recovery_rechecks_candidate_after_another_run_claims_it(tmp_path, monkeypatch):
    records, now, first, second = setup(tmp_path)
    claim(first)
    now[0] = 111
    select = records.list_matching
    def select_and_replace(collection, **fields):
        rows = select(collection, **fields)
        row = rows[0]
        with records.begin() as tx:
            updated = tx.put(COLLECTION, row.object_id, {**row.payload, "status": "staged"}, expected_revision=row.revision)
            tx.commit()
        claim(second, updated.revision, "run-b")
        return rows
    monkeypatch.setattr(records, "list_matching", select_and_replace)
    assert first.recover_expired() == 0
    current = records.read(COLLECTION, "item-a")
    assert current.payload["status"] == "processing"
    assert current.payload["processing_run_id"] == "run-b"
