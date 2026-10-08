from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.storage_provider import (
    JsonObjectStore,
    MigrationLedgerConflict,
    MigrationLedgerReadOnlyError,
    SQLiteMigrationLedger,
    scan_json_object_inventory,
)


def _write_object(root: Path, collection: str, object_id: str, payload: dict[str, object]) -> None:
    directory = root / "objects" / "default" / collection
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{object_id}.json").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _inventory_root(tmp_path: Path) -> Path:
    root = tmp_path / "legacy-json-store"
    _write_object(root, "jobs", "job-ledger-1", {"id": "job-ledger-1", "status": "failed"})
    _write_object(root, "sources", "source-ledger-1", {"id": "source-ledger-1", "title": "Ledger fixture"})
    metadata = root / "objects" / "default" / "jobs" / "job-ledger-1.meta.json"
    metadata.write_text('{"revision": 1}', encoding="utf-8")
    return root


def test_inventory_is_deterministic_and_ignores_object_store_metadata(tmp_path: Path) -> None:
    root = _inventory_root(tmp_path)

    first = scan_json_object_inventory(root, namespace_id="default")
    second = scan_json_object_inventory(root, namespace_id="default")

    assert first == second
    assert first.object_count == 2
    assert [(item.collection, item.object_count) for item in first.collections] == [
        ("jobs", 1),
        ("sources", 1),
    ]
    assert len(first.fingerprint) == 64
    assert all(len(item.fingerprint) == 64 for item in first.collections)


def test_inventory_preserves_logical_id_for_short_object_filename(tmp_path: Path) -> None:
    object_id = "job-" + "x" * 124
    for depth in range(121):
        root = tmp_path / ("d" * depth) / ".rebuild-data"
        legacy_meta = root / "objects" / "default" / "jobs" / f"{object_id}.meta.json"
        short_meta = root / "objects" / "default" / "jobs" / (
            "~h-" + hashlib.sha256(object_id.encode("utf-8")).hexdigest() + ".meta.json"
        )
        if len(str(legacy_meta)) > 259 and len(str(short_meta)) <= 259:
            break
    else:  # pragma: no cover - platform fixture guard
        raise AssertionError("could not construct short filename inventory fixture")
    store = JsonObjectStore(root, legacy_root=tmp_path / "library")
    store.write("jobs", object_id, {"id": object_id, "status": "pending"}, expected_revision=0)

    inventory = scan_json_object_inventory(root, namespace_id="default")

    assert inventory.object_count == 1
    assert inventory.collections[0].collection == "jobs"
    assert inventory.collections[0].object_count == 1


def test_dry_run_is_idempotent_and_uses_wal(tmp_path: Path) -> None:
    inventory = scan_json_object_inventory(_inventory_root(tmp_path), namespace_id="default")
    ledger = SQLiteMigrationLedger(tmp_path / "ledger" / "migration-ledger.sqlite3")

    first = ledger.plan_dry_run(
        migration_id="json-to-sqlite-sources-v1",
        target_schema_version=2,
        inventory=inventory,
        rollback_pointer="snapshot:before-json-to-sqlite-sources-v1",
    )
    repeated = ledger.plan_dry_run(
        migration_id="json-to-sqlite-sources-v1",
        target_schema_version=2,
        inventory=inventory,
        rollback_pointer="snapshot:before-json-to-sqlite-sources-v1",
    )

    assert ledger.schema_version == 1
    assert ledger.journal_mode() == "wal"
    assert first == repeated
    assert first.state == "dry_run_ready"
    assert first.input_fingerprint == inventory.fingerprint
    assert first.rollback_pointer == "snapshot:before-json-to-sqlite-sources-v1"
    assert ledger.list_records() == (first,)


def test_dry_run_rejects_input_or_target_version_drift_for_same_migration_id(tmp_path: Path) -> None:
    root = _inventory_root(tmp_path)
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")
    initial = scan_json_object_inventory(root, namespace_id="default")
    ledger.plan_dry_run(
        migration_id="json-to-sqlite-jobs-v1",
        target_schema_version=2,
        inventory=initial,
        rollback_pointer="snapshot:jobs-v1",
    )

    _write_object(root, "jobs", "job-ledger-1", {"id": "job-ledger-1", "status": "pending"})
    changed = scan_json_object_inventory(root, namespace_id="default")
    assert changed.fingerprint != initial.fingerprint

    with pytest.raises(MigrationLedgerConflict, match="input fingerprint"):
        ledger.plan_dry_run(
            migration_id="json-to-sqlite-jobs-v1",
            target_schema_version=2,
            inventory=changed,
            rollback_pointer="snapshot:jobs-v1",
        )

    with pytest.raises(MigrationLedgerConflict, match="target schema version"):
        ledger.plan_dry_run(
            migration_id="json-to-sqlite-jobs-v1",
            target_schema_version=3,
            inventory=initial,
            rollback_pointer="snapshot:jobs-v1",
        )


def test_failed_migration_exposes_ledger_only_read_only_guard(tmp_path: Path) -> None:
    inventory = scan_json_object_inventory(_inventory_root(tmp_path), namespace_id="default")
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")
    ledger.plan_dry_run(
        migration_id="json-to-sqlite-memory-v1",
        target_schema_version=2,
        inventory=inventory,
        rollback_pointer="snapshot:memory-v1",
    )

    failed = ledger.mark_failed("json-to-sqlite-memory-v1", failure_code="fingerprint_mismatch")

    assert failed.state == "failed"
    assert failed.failure_code == "fingerprint_mismatch"
    assert failed.rollback_pointer == "snapshot:memory-v1"
    assert ledger.read_only_reason() == "migration json-to-sqlite-memory-v1 failed: fingerprint_mismatch"
    with pytest.raises(MigrationLedgerReadOnlyError, match="fingerprint_mismatch"):
        ledger.assert_writable()
    with pytest.raises(MigrationLedgerReadOnlyError, match="fingerprint_mismatch"):
        ledger.plan_dry_run(
            migration_id="json-to-sqlite-atoms-v1",
            target_schema_version=2,
            inventory=inventory,
            rollback_pointer="snapshot:atoms-v1",
        )
