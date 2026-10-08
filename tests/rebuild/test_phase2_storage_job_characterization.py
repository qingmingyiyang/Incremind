from __future__ import annotations

from pathlib import Path

import pytest

from core.job_runner import ObjectStoreJobRepository
from core.storage_provider import JsonObjectStore, ObjectStoreRevisionError


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
    )


def test_json_object_store_rejects_stale_single_object_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.write("jobs", "job-phase2-cas", {"id": "job-phase2-cas", "status": "failed"}, expected_revision=0) == 1

    with pytest.raises(ObjectStoreRevisionError, match="expected revision 0, found 1"):
        store.write("jobs", "job-phase2-cas", {"id": "job-phase2-cas", "status": "pending"}, expected_revision=0)

    assert store.read("jobs", "job-phase2-cas") == {"id": "job-phase2-cas", "status": "failed"}


def test_legacy_job_repository_unconditionally_overwrites_newer_object(tmp_path: Path) -> None:
    """Characterize the legacy boundary that a Phase 2 job store must replace.

    JsonObjectStore supports a caller-provided revision, but ObjectStoreJobRepository
    currently always writes with ``expected_revision=None``. The assertion documents
    current behavior rather than treating it as a concurrency guarantee.
    """

    store = _store(tmp_path)
    repository = ObjectStoreJobRepository(store)
    stale_job = {"id": "job-phase2-unconditional", "status": "failed", "attempt": 1}
    repository.save(stale_job)

    # A separate writer makes a newer durable revision first.
    assert store.write(
        "jobs",
        "job-phase2-unconditional",
        {"id": "job-phase2-unconditional", "status": "pending", "attempt": 2},
        expected_revision=1,
    ) == 2

    # The repository has no revision input and silently overwrites the newer record.
    repository.save(stale_job)

    assert store.read("jobs", "job-phase2-unconditional") == stale_job
