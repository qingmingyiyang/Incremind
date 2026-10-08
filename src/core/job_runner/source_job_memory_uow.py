from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from core.storage_provider import (
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SQLiteStructuredRecordUnitOfWork,
)

from .sqlite_store import SQLiteJobRecord, SQLiteJobStore, SQLiteJobStoreTransaction


class SQLiteSourceJobMemoryUnitOfWorkError(ValueError):
    """Raised when the isolated aggregate UoW cannot complete safely."""


@dataclass(frozen=True, slots=True)
class SQLiteSourceJobMemoryCommit:
    source: SQLiteStructuredRecord
    job: SQLiteJobRecord
    memory: tuple[SQLiteStructuredRecord, ...]


class SQLiteSourceJobMemoryUnitOfWork:
    """Temporary-fixture aggregate UoW for Source, Job and Memory records.

    The class is deliberately not composed into runtime authority. It exists to
    prove one connection and one SQLite commit can hold the three aggregates
    before a separately authorized migration/cutover task is attempted.
    """

    def __init__(self, database_path: Path) -> None:
        self._records = SQLiteStructuredRecordStore(database_path)
        self._jobs = SQLiteJobStore(database_path)

    def begin(self) -> SQLiteSourceJobMemoryTransaction:
        records = self._records.begin()
        try:
            return SQLiteSourceJobMemoryTransaction(records, self._jobs.bind(records.connection))
        except Exception:
            records.rollback()
            raise


class SQLiteSourceJobMemoryTransaction:
    """Stages Source, Job and Memory mutations on one open transaction."""

    def __init__(
        self,
        records: SQLiteStructuredRecordUnitOfWork,
        jobs: SQLiteJobStoreTransaction,
    ) -> None:
        self._records = records
        self._jobs = jobs
        self._source: SQLiteStructuredRecord | None = None
        self._job: SQLiteJobRecord | None = None
        self._memory: list[SQLiteStructuredRecord] = []

    @property
    def closed(self) -> bool:
        return self._records.closed

    def __enter__(self) -> SQLiteSourceJobMemoryTransaction:
        if self.closed:
            raise SQLiteSourceJobMemoryUnitOfWorkError("aggregate unit of work is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if not self.closed:
            self.rollback()
        return False

    def put_source(
        self,
        source: Mapping[str, object],
        *,
        expected_revision: int,
    ) -> SQLiteStructuredRecord:
        return self._put_source_or_memory("sources", source, expected_revision=expected_revision, target="source")

    def put_memory(
        self,
        collection: str,
        memory: Mapping[str, object],
        *,
        expected_revision: int,
    ) -> SQLiteStructuredRecord:
        if collection not in {
            "staging_atoms",
            "staging_scenarios",
            "staging_series_memory",
            "memory_atoms",
            "memory_scenarios",
            "memory_series_memory",
        }:
            self.rollback()
            raise SQLiteSourceJobMemoryUnitOfWorkError("memory collection is not aggregate-owned")
        return self._put_source_or_memory(collection, memory, expected_revision=expected_revision, target="memory")

    def save_job(self, job: Mapping[str, object], *, expected_revision: int) -> SQLiteJobRecord:
        self._require_open()
        try:
            self._job = self._jobs.save(job, expected_revision=expected_revision)
            return self._job
        except Exception:
            self.rollback()
            raise

    def commit(self) -> SQLiteSourceJobMemoryCommit:
        self._require_open()
        if self._source is None or self._job is None:
            self.rollback()
            raise SQLiteSourceJobMemoryUnitOfWorkError("aggregate commit requires source and job")
        try:
            self._records.commit()
            return SQLiteSourceJobMemoryCommit(self._source, self._job, tuple(self._memory))
        except Exception:
            self.rollback()
            raise

    def rollback(self) -> None:
        self._records.rollback()

    def _put_source_or_memory(
        self,
        collection: str,
        payload: Mapping[str, object],
        *,
        expected_revision: int,
        target: str,
    ) -> SQLiteStructuredRecord:
        self._require_open()
        object_id = payload.get("id")
        if not isinstance(object_id, str) or not object_id:
            self.rollback()
            raise SQLiteSourceJobMemoryUnitOfWorkError(f"{target} requires id")
        try:
            record = self._records.put(collection, object_id, payload, expected_revision=expected_revision)
            if target == "source":
                self._source = record
            else:
                self._memory.append(record)
            return record
        except Exception:
            self.rollback()
            raise

    def _require_open(self) -> None:
        if self.closed:
            raise SQLiteSourceJobMemoryUnitOfWorkError("aggregate unit of work is closed")
