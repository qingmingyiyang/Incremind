from __future__ import annotations

from pathlib import Path

import pytest

from core.job_runner import (
    SQLiteJobLeaseConflict,
    SQLiteJobStore,
    SQLiteSourceJobMemoryUnitOfWork,
    SQLiteSourceJobMemoryUnitOfWorkError,
)
from core.storage_provider import SQLiteStructuredRecordStore


ROOT = Path(__file__).resolve().parents[2]


def _database(tmp_path):
    return tmp_path / ".rebuild-data" / "structured-records.sqlite3"


def _source(source_id: str = "source-uow-001") -> dict[str, object]:
    return {"id": source_id, "schema_version": "1.0.0", "title": "temporary source"}


def _job(job_id: str = "job-uow-001") -> dict[str, object]:
    return {
        "id": job_id,
        "job_type": "extract_memory",
        "status": "running",
        "attempt": 1,
        "lease": {"worker_id": "worker-a", "lease_token": "token-a", "expires_at": "2099-01-01T00:00:00Z"},
        "checkpoint": {"resume_step": "publish_atom"},
        "steps": [],
        "staged_outputs": [],
        "published_outputs": [],
    }


def _memory(memory_id: str = "atom-uow-001") -> dict[str, object]:
    return {"id": memory_id, "source_id": "source-uow-001", "trust_status": "system_generated"}


def test_aggregate_uow_commits_source_job_and_memory_in_one_database(tmp_path) -> None:
    database = _database(tmp_path)
    aggregate = SQLiteSourceJobMemoryUnitOfWork(database)

    with aggregate.begin() as uow:
        source = uow.put_source(_source(), expected_revision=0)
        job = uow.save_job(_job(), expected_revision=0)
        memory = uow.put_memory("staging_atoms", _memory(), expected_revision=0)
        committed = uow.commit()

    records = SQLiteStructuredRecordStore(database)
    sqlite_jobs = SQLiteJobStore(database)
    assert committed.source == source
    assert committed.job == job
    assert committed.memory == (memory,)
    assert records.read("sources", "source-uow-001") == source
    assert records.read("staging_atoms", "atom-uow-001") == memory
    reloaded_job = sqlite_jobs.read("job-uow-001")
    assert reloaded_job == job
    assert reloaded_job is not None
    assert reloaded_job.payload["lease"]["lease_token"] == "token-a"
    assert reloaded_job.payload["checkpoint"] == {"resume_step": "publish_atom"}


def test_job_conflict_rolls_back_staged_source_and_memory(tmp_path) -> None:
    database = _database(tmp_path)
    existing_jobs = SQLiteJobStore(database)
    existing_jobs.create(_job("job-existing-001"))
    aggregate = SQLiteSourceJobMemoryUnitOfWork(database)

    uow = aggregate.begin()
    uow.put_source(_source("source-rolled-back-001"), expected_revision=0)
    uow.put_memory("staging_atoms", _memory("atom-rolled-back-001"), expected_revision=0)
    with pytest.raises(SQLiteJobLeaseConflict, match="stale job revision"):
        uow.save_job(_job("job-existing-001"), expected_revision=0)

    records = SQLiteStructuredRecordStore(database)
    assert uow.closed is True
    assert records.read("sources", "source-rolled-back-001") is None
    assert records.read("staging_atoms", "atom-rolled-back-001") is None
    assert existing_jobs.read("job-existing-001").revision == 1


def test_invalid_memory_mutation_rolls_back_source_and_job(tmp_path) -> None:
    database = _database(tmp_path)
    aggregate = SQLiteSourceJobMemoryUnitOfWork(database)

    uow = aggregate.begin()
    uow.put_source(_source("source-invalid-memory-001"), expected_revision=0)
    uow.save_job(_job("job-invalid-memory-001"), expected_revision=0)
    with pytest.raises(SQLiteSourceJobMemoryUnitOfWorkError, match="memory collection"):
        uow.put_memory("memory_transitions", _memory("atom-invalid-memory-001"), expected_revision=0)

    records = SQLiteStructuredRecordStore(database)
    assert uow.closed is True
    assert records.read("sources", "source-invalid-memory-001") is None
    assert SQLiteJobStore(database).read("job-invalid-memory-001") is None


def test_uncommitted_aggregate_transaction_rolls_back_on_context_exit(tmp_path) -> None:
    database = _database(tmp_path)
    aggregate = SQLiteSourceJobMemoryUnitOfWork(database)

    with aggregate.begin() as uow:
        uow.put_source(_source("source-context-rollback-001"), expected_revision=0)
        uow.save_job(_job("job-context-rollback-001"), expected_revision=0)

    assert SQLiteStructuredRecordStore(database).read("sources", "source-context-rollback-001") is None
    assert SQLiteJobStore(database).read("job-context-rollback-001") is None


def test_default_runtime_composition_does_not_activate_aggregate_adapter() -> None:
    composition = (ROOT / "src" / "core" / "composition.py").read_text(encoding="utf-8")

    assert "SQLiteSourceJobMemoryUnitOfWork" not in composition
    assert "source_job_memory_uow" not in composition
