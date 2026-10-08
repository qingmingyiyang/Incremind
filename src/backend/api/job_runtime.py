from __future__ import annotations

from pathlib import Path

from core.job_runner import (
    ObjectStoreJobRepository,
    RoutedJobRepository,
    SQLiteJobStore,
)


_SUPPORTED_SQLITE_JOB_TYPES = {"extract_memory", "extract_memory_candidate"}


def configured_sqlite_job_types() -> frozenset[str]:
    """Compatibility metadata; every production Job is Effect-projected."""

    return frozenset(_SUPPORTED_SQLITE_JOB_TYPES)


def build_rebuild_job_repository(
    runtime_root: Path,
    store: object,
) -> RoutedJobRepository:
    """Compose the routed Job repository without coupling API adapters to storage."""

    runtime_root = Path(runtime_root)
    return RoutedJobRepository(
        legacy=ObjectStoreJobRepository(store),
        sqlite=SQLiteJobStore(runtime_root / ".rebuild-data" / "jobs.sqlite3"),
        sqlite_job_types=configured_sqlite_job_types(),
    )
