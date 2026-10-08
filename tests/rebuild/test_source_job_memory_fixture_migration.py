from __future__ import annotations

from pathlib import Path

import pytest

import core.job_runner.source_job_memory_migration as migration
from core.job_runner import (
    SQLiteJobStore,
    SQLiteSourceJobMemoryTransaction,
    SourceJobMemoryMigrationExecutionError,
    SourceJobMemoryMigrationInventoryError,
    execute_source_job_memory_fixture_migration,
    plan_source_job_memory_migration_dry_run,
    scan_source_job_memory_migration_inventory,
)
from core.storage_provider import JsonObjectStore, SQLiteMigrationLedger, SQLiteStructuredRecordStore


def _fixture(tmp_path: Path):
    rebuild_root = tmp_path / "fixture" / ".rebuild-data"
    store = JsonObjectStore(rebuild_root, legacy_root=tmp_path / "fixture" / "library")
    source_id = "source-migration-001"
    source = {
        "id": source_id,
        "title": "Fixture Source",
        "storage_uri": f"crp://default/sources/{source_id}",
        "content_hash": "sha256:source-fixture",
    }
    store.write("sources", source_id, source, expected_revision=0)
    source["title"] = "Fixture Source current"
    store.write("sources", source_id, source, expected_revision=1)

    _write_memory_records(store, source_id)
    jobs = SQLiteJobStore(rebuild_root / "jobs.sqlite3")
    job_id = "job-extract-migration-001"
    job = {
        "id": job_id,
        "source_id": source_id,
        "job_type": "extract_memory",
        "status": "running",
        "lease": {
            "worker_id": "fixture-worker",
            "lease_token": "fixture-token",
            "acquired_at": "2026-07-12T00:00:00Z",
            "expires_at": "2026-07-12T00:05:00Z",
        },
        "checkpoint": {"resume_step": "publish_atom", "state_hash": "sha256:fixture"},
        "steps": [{"name": "extract", "status": "completed"}],
        "staged_outputs": [{"kind": "atom", "object_id": "atom-staged-001"}],
        "published_outputs": [{"kind": "atom", "object_id": "atom-published-001"}],
    }
    jobs.create(job)
    job["checkpoint"] = {"resume_step": "publish_atom", "state_hash": "sha256:fixture-current"}
    jobs.save(job, expected_revision=1)
    job["updated_at"] = "2026-07-12T00:01:00Z"
    jobs.save(job, expected_revision=2)

    inventory = scan_source_job_memory_migration_inventory(
        rebuild_root,
        jobs_database_path=rebuild_root / "jobs.sqlite3",
        namespace_id="default",
    )
    assert inventory.issues == ()
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")
    dry_run = plan_source_job_memory_migration_dry_run(
        ledger=ledger,
        migration_id="fixture-source-job-memory-v1",
        target_schema_version=1,
        inventory=inventory,
        rollback_pointer="snapshot:fixture-source-job-memory-v1",
    )
    return rebuild_root, store, jobs, source_id, job_id, inventory, ledger, dry_run


def _write_memory_records(store: JsonObjectStore, source_id: str) -> None:
    atom = {
        "id": "atom-published-001",
        "source_id": source_id,
        "source_refs": [{"source_id": source_id, "locator": "char:0-7", "quote": "Fixture"}],
        "content": "Fixture",
        "revision": 1,
    }
    store.write("memory_atoms", atom["id"], atom, expected_revision=0)
    atom["content"] = "Fixture current"
    store.write("memory_atoms", atom["id"], atom, expected_revision=1)
    staged = {
        "id": "atom-staged-001",
        "source_id": source_id,
        "source_refs": [{"source_id": source_id, "locator": "char:8-14", "quote": "staged"}],
        "content": "staged",
        "revision": 1,
    }
    store.write("staging_atoms", staged["id"], staged, expected_revision=0)
    scenario = {
        "id": "scenario-migration-001",
        "source_refs": [{"source_id": source_id, "locator": "char:0-14", "quote": "Fixture staged"}],
        "atom_ids": [atom["id"]],
        "revision": 1,
    }
    store.write("memory_scenarios", scenario["id"], scenario, expected_revision=0)
    series_memory = {
        "id": "series-memory-migration-001",
        "source_refs": [{"source_id": source_id, "locator": "char:0-14", "quote": "Fixture staged"}],
        "scenario_ids": [scenario["id"]],
        "revision": 1,
    }
    store.write("memory_series_memory", series_memory["id"], series_memory, expected_revision=0)


def _target(rebuild_root: Path) -> Path:
    return rebuild_root / "structured-records.sqlite3"


def test_fixture_executor_copies_current_payloads_and_preserves_cas_revisions(tmp_path: Path) -> None:
    rebuild_root, store, source_jobs, source_id, job_id, inventory, ledger, dry_run = _fixture(tmp_path)
    target = _target(rebuild_root)

    result = execute_source_job_memory_fixture_migration(
        object_store_root=rebuild_root,
        jobs_database_path=rebuild_root / "jobs.sqlite3",
        target_database_path=target,
        ledger=ledger,
        dry_run=dry_run,
    )

    records = SQLiteStructuredRecordStore(target)
    source = records.read("sources", source_id)
    assert result.source_count == 1
    assert result.job_count == 1
    assert result.memory_count == 4
    assert result.object_count == inventory.inventory.object_count
    assert result.input_fingerprint == inventory.inventory.fingerprint
    assert source is not None
    assert source.payload == store.read("sources", source_id)
    assert source.revision == store.revision("sources", source_id)
    for collection in ("memory_atoms", "memory_scenarios", "memory_series_memory", "staging_atoms"):
        for payload in store.list(collection):
            record = records.read(collection, str(payload["id"]))
            assert record is not None
            assert record.payload == payload
            assert record.revision == store.revision(collection, str(payload["id"]))

    source_job = source_jobs.read(job_id)
    target_job = SQLiteJobStore(target).read(job_id)
    assert source_job is not None
    assert target_job is not None
    assert target_job.payload == source_job.payload
    assert target_job.revision == source_job.revision
    assert target_job.payload["checkpoint"] == source_job.payload["checkpoint"]
    assert target_job.payload["lease"] == source_job.payload["lease"]
    assert ledger.list_records() == (dry_run,)


def test_executor_rejects_input_drift_before_target_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rebuild_root, store, _jobs, source_id, _job_id, _inventory, ledger, dry_run = _fixture(tmp_path)
    target = _target(rebuild_root)
    original = migration._read_fixture_records
    reads = 0

    def _drift_after_read(*args, **kwargs):
        nonlocal reads
        reads += 1
        records = original(*args, **kwargs)
        if reads == 2:
            source = store.read("sources", source_id)
            assert source is not None
            source["title"] = "drift after dry run"
            store.write("sources", source_id, source, expected_revision=2)
        return records

    monkeypatch.setattr(migration, "_read_fixture_records", _drift_after_read)
    with pytest.raises(SourceJobMemoryMigrationExecutionError, match="input changed while preparing copy"):
        execute_source_job_memory_fixture_migration(
            object_store_root=rebuild_root,
            jobs_database_path=rebuild_root / "jobs.sqlite3",
            target_database_path=target,
            ledger=ledger,
            dry_run=dry_run,
        )
    assert target.exists() is False


def test_executor_rolls_back_and_removes_new_target_after_memory_copy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuild_root, store, _jobs, _source_id, _job_id, inventory, ledger, dry_run = _fixture(tmp_path)
    target = _target(rebuild_root)
    original = SQLiteSourceJobMemoryTransaction.put_memory
    calls = 0

    def _fail_second_memory(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected memory copy failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SQLiteSourceJobMemoryTransaction, "put_memory", _fail_second_memory)
    with pytest.raises(SourceJobMemoryMigrationExecutionError, match="copy failed"):
        execute_source_job_memory_fixture_migration(
            object_store_root=rebuild_root,
            jobs_database_path=rebuild_root / "jobs.sqlite3",
            target_database_path=target,
            ledger=ledger,
            dry_run=dry_run,
        )
    assert target.exists() is False
    assert Path(f"{target}-wal").exists() is False
    assert Path(f"{target}-shm").exists() is False
    assert scan_source_job_memory_migration_inventory(
        rebuild_root,
        jobs_database_path=rebuild_root / "jobs.sqlite3",
        namespace_id="default",
    ) == inventory
    assert store.read("sources", "source-migration-001") is not None


def test_inventory_fails_closed_for_unowned_transition_and_json_duplicate_job(tmp_path: Path) -> None:
    rebuild_root, store, _jobs, source_id, _job_id, _inventory, ledger, _dry_run = _fixture(tmp_path)
    store.write(
        "memory_transitions",
        "transition-001",
        {"object_id": "atom-published-001", "trust_status": "system_generated"},
        expected_revision=0,
    )
    store.write(
        "jobs",
        "legacy-extract-memory-001",
        {"id": "legacy-extract-memory-001", "source_id": source_id, "job_type": "extract_memory"},
        expected_revision=0,
    )

    inventory = scan_source_job_memory_migration_inventory(
        rebuild_root,
        jobs_database_path=rebuild_root / "jobs.sqlite3",
        namespace_id="default",
    )

    assert {issue.code for issue in inventory.issues} == {
        "legacy_json_extract_memory_job",
        "memory_transitions_not_owned",
    }
    with pytest.raises(SourceJobMemoryMigrationInventoryError, match="legacy_json_extract_memory_job"):
        plan_source_job_memory_migration_dry_run(
            ledger=ledger,
            migration_id="fixture-source-job-memory-unowned-v1",
            target_schema_version=1,
            inventory=inventory,
            rollback_pointer="snapshot:fixture-source-job-memory-unowned-v1",
        )


def test_inventory_fails_closed_for_orphan_memory_and_source_without_extract_job(tmp_path: Path) -> None:
    rebuild_root, store, _jobs, _source_id, _job_id, _inventory, _ledger, _dry_run = _fixture(tmp_path)
    store.write(
        "memory_atoms",
        "atom-orphan-001",
        {
            "id": "atom-orphan-001",
            "source_id": "source-missing-001",
            "source_refs": [{"source_id": "source-missing-001", "locator": "char:0-1"}],
        },
        expected_revision=0,
    )
    store.write(
        "sources",
        "source-without-job-001",
        {"id": "source-without-job-001", "storage_uri": "crp://default/sources/source-without-job-001"},
        expected_revision=0,
    )

    inventory = scan_source_job_memory_migration_inventory(
        rebuild_root,
        jobs_database_path=rebuild_root / "jobs.sqlite3",
        namespace_id="default",
    )

    assert {issue.code for issue in inventory.issues} == {"memory_source_missing", "source_extract_job_missing"}


def test_inventory_fails_closed_for_unsupported_memory_collection(tmp_path: Path) -> None:
    rebuild_root, store, _jobs, _source_id, _job_id, _inventory, _ledger, _dry_run = _fixture(tmp_path)
    store.write(
        "memory_personas",
        "persona-migration-001",
        {"id": "persona-migration-001", "source_refs": []},
        expected_revision=0,
    )

    inventory = scan_source_job_memory_migration_inventory(
        rebuild_root,
        jobs_database_path=rebuild_root / "jobs.sqlite3",
        namespace_id="default",
    )

    assert inventory.issues == (
        migration.SourceJobMemoryMigrationIssue(
            "unsupported_memory_collection", "memory_personas", "*"
        ),
    )


def test_executor_refuses_existing_or_nested_target_without_overwriting_it(tmp_path: Path) -> None:
    rebuild_root, _store, _jobs, _source_id, _job_id, _inventory, ledger, dry_run = _fixture(tmp_path)
    target = _target(rebuild_root)
    target.write_bytes(b"keep")
    with pytest.raises(SourceJobMemoryMigrationExecutionError, match="already exist"):
        execute_source_job_memory_fixture_migration(
            object_store_root=rebuild_root,
            jobs_database_path=rebuild_root / "jobs.sqlite3",
            target_database_path=target,
            ledger=ledger,
            dry_run=dry_run,
        )
    assert target.read_bytes() == b"keep"

    nested = rebuild_root / "objects" / "default" / "structured-records.sqlite3"
    with pytest.raises(SourceJobMemoryMigrationExecutionError, match="target must be"):
        execute_source_job_memory_fixture_migration(
            object_store_root=rebuild_root,
            jobs_database_path=rebuild_root / "jobs.sqlite3",
            target_database_path=nested,
            ledger=ledger,
            dry_run=dry_run,
        )
    assert nested.exists() is False


def test_default_composition_does_not_import_fixture_migration_module() -> None:
    root = Path(__file__).resolve().parents[2]
    composition = (root / "src" / "core" / "composition.py").read_text(encoding="utf-8")

    assert "source_job_memory_migration" not in composition
    assert "execute_source_job_memory_fixture_migration" not in composition
