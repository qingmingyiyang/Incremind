from __future__ import annotations

import pytest

from core.job_runner import (
    JobAuthorityConflict,
    ObjectStoreJobRepository,
    RoutedJobRepository,
    SQLiteLegacyMediaExecutionBlocked,
    SQLiteJobStore,
)
from core.storage_provider import JsonObjectStore


def _job(job_id: str, job_type: str = "extract_memory", status: str = "pending") -> dict[str, object]:
    return {"id": job_id, "job_type": job_type, "status": status, "steps": [], "lease": None}


def _repositories(tmp_path, *, sqlite_job_types=frozenset({"extract_memory"})):
    legacy = ObjectStoreJobRepository(JsonObjectStore(tmp_path / "json", legacy_root=tmp_path / "library"))
    sqlite = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    return legacy, sqlite, RoutedJobRepository(legacy=legacy, sqlite=sqlite, sqlite_job_types=sqlite_job_types)


def test_all_new_jobs_use_effect_projection_independent_of_job_type(tmp_path) -> None:
    legacy, sqlite, routed = _repositories(tmp_path)

    routed.save(_job("job-sqlite", job_type="capture", status="completed"))
    routed.save(_job("job-json", job_type="workbench_auto_intake", status="completed"))

    assert sqlite.read("job-sqlite") is not None
    assert legacy.get("job-sqlite") is None
    assert legacy.get("job-json") is None
    assert sqlite.read("job-json") is not None
    assert {job["id"] for job in routed.all()} == {"job-sqlite", "job-json"}


def test_existing_legacy_job_is_readonly_and_cannot_be_retried_by_save(tmp_path) -> None:
    legacy, sqlite, routed = _repositories(tmp_path)
    legacy.save(_job("job-legacy", status="failed"))

    retried = _job("job-legacy", status="pending")
    retried["attempt"] = 1
    with pytest.raises(JobAuthorityConflict, match="read-only"):
        routed.save(retried)

    assert legacy.get("job-legacy") is not None
    assert sqlite.read("job-legacy") is None
    assert routed.get("job-legacy")["execution_version"] == "legacy-v1-readonly"


def test_divergent_legacy_and_effect_projection_fail_closed(tmp_path) -> None:
    legacy, sqlite, routed = _repositories(tmp_path)
    legacy.save(_job("job-conflict", status="failed"))
    sqlite.create(_job("job-conflict", job_type="capture", status="completed"))

    with pytest.raises(JobAuthorityConflict, match="conflicts with executable Effect"):
        routed.import_legacy_history(
            migration_id="object-store-job-history-v1",
            imported_at="2026-08-30T00:00:00Z",
        )
    assert routed.get("job-conflict")["status"] == "completed"
    assert legacy.get("job-conflict") is not None


def test_empty_legacy_allowlist_cannot_restore_old_write_authority(tmp_path) -> None:
    legacy, sqlite, routed = _repositories(tmp_path, sqlite_job_types=frozenset())

    routed.save(_job("job-rollback", job_type="capture", status="completed"))

    assert legacy.get("job-rollback") is None
    assert sqlite.read("job-rollback") is not None


def test_media_hands_identity_requires_effect_v2_admission_when_legacy_is_absent(tmp_path) -> None:
    legacy, sqlite, routed = _repositories(tmp_path, sqlite_job_types=frozenset())
    job = _job("media_hands:xhs-source:analyze_source", job_type="media_hands")

    assert routed.get(job["id"]) is None
    with pytest.raises(SQLiteLegacyMediaExecutionBlocked, match="legacy Media Job execution is read-only"):
        routed.save(job)

    assert routed.get(job["id"]) is None
    assert routed.is_sqlite_authority(job["id"]) is False
    assert legacy.all() == ()


def test_media_hands_legacy_logical_identity_imports_as_readonly_without_delete(tmp_path) -> None:
    legacy, sqlite, routed = _repositories(tmp_path, sqlite_job_types=frozenset())
    job = _job("media_hands:xhs-source:analyze_source", job_type="media_hands")
    legacy.object_store.write("jobs", "safe-legacy-key", job, expected_revision=None)

    live = routed.get(job["id"])
    assert live["status"] == "legacy_unknown"
    assert live["execution_version"] == "legacy-v1-readonly"
    assert routed.import_legacy_history(
        migration_id="object-store-job-history-v1",
        imported_at="2026-08-30T00:00:00Z",
    ) == (job["id"],)
    frozen = routed.get(job["id"])
    assert frozen["status"] == "legacy_unknown"
    assert frozen["history_source"] == "legacy_job_history"
    assert routed.is_sqlite_authority(job["id"]) is False
    assert legacy.all() == (job,)
    assert sqlite.read(job["id"]) is not None


def test_duplicate_legacy_source_cannot_be_imported_over_executable_projection(tmp_path) -> None:
    legacy, sqlite, routed = _repositories(tmp_path, sqlite_job_types=frozenset())
    job = _job("media_hands:xhs-source:analyze_source", job_type="media_hands")
    legacy.object_store.write("jobs", "safe-legacy-key", job, expected_revision=None)
    sqlite.create(_job(job["id"], job_type="capture", status="completed"))

    with pytest.raises(JobAuthorityConflict, match="conflicts with executable Effect"):
        routed.import_legacy_history(
            migration_id="object-store-job-history-v1",
            imported_at="2026-08-30T00:00:00Z",
        )
    assert routed.get(job["id"])["status"] == "completed"
    assert legacy.all() == (job,)
    assert routed.is_sqlite_authority(job["id"])


def test_legacy_job_id_is_normalized_for_read_and_list(tmp_path) -> None:
    legacy, _sqlite, routed = _repositories(tmp_path)
    legacy.object_store.write(
        "jobs",
        "job-pre-canonical",
        {"job_id": "job-pre-canonical", "job_type": "extract_memory", "status": "running"},
        expected_revision=None,
    )

    assert routed.get("job-pre-canonical")["id"] == "job-pre-canonical"
    assert routed.list_jobs(status="legacy_unknown") == (
        {
            "job_id": "job-pre-canonical",
            "id": "job-pre-canonical",
            "job_type": "extract_memory",
            "status": "legacy_unknown",
            "lease": None,
            "legacy_status": "running",
            "display_status": "legacy_unknown",
            "execution_version": "legacy-v1-readonly",
            "history_source": "legacy-object-store-live-readonly",
            "execution_action": {
                "kind": "manual_confirmation_required",
                "enabled": False,
                "reason": "legacy_history",
            },
        },
    )


def test_legacy_job_without_any_identity_fails_closed(tmp_path) -> None:
    legacy, _sqlite, routed = _repositories(tmp_path)
    legacy.object_store.write(
        "jobs",
        "invalid",
        {"job_type": "extract_memory", "status": "running"},
        expected_revision=None,
    )

    with pytest.raises(JobAuthorityConflict, match="identity is invalid"):
        routed.all()


def test_explicit_rollback_cannot_restore_legacy_execution_authority(tmp_path) -> None:
    legacy, sqlite, routed = _repositories(tmp_path)
    routed.save(_job("job-rollback-cache", job_type="capture", status="completed"))

    with pytest.raises(JobAuthorityConflict, match="cannot restore"):
        routed.rollback_to_legacy()
    assert legacy.get("job-rollback-cache") is None
    assert sqlite.read("job-rollback-cache") is not None
    assert routed.get("job-rollback-cache")["status"] == "completed"


def test_legacy_history_rejects_cancel_and_exact_import_is_idempotent(tmp_path) -> None:
    legacy, _sqlite, routed = _repositories(tmp_path)
    legacy.save(_job("job-history", status="completed"))

    first = routed.import_legacy_history(
        migration_id="object-store-job-history-v1",
        imported_at="2026-08-30T00:00:00Z",
    )
    second = routed.import_legacy_history(
        migration_id="object-store-job-history-v1",
        imported_at="2026-08-30T00:00:01Z",
    )

    assert first == second == ("job-history",)
    assert routed.get("job-history")["status"] == "completed"
    with pytest.raises(JobAuthorityConflict, match="read-only"):
        routed.request_cancel(
            "job-history", request_id="cancel-history", now="2026-08-30T00:00:02Z",
        )
    assert legacy.get("job-history") is not None
