from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from backend.api.memory_import_batch_startup import (
    MemoryImportBatchStartupRecoveryReport,
    recover_memory_import_batches,
)
from backend.api.routes.product.memory_import_records import (
    _MEMORY_IMPORT_BATCHES_COLLECTION,
    _MEMORY_IMPORT_RUNTIME_SESSION_ID,
)
from backend.api.routes.product.repositories import _object_store


def _write_batch(
    runtime_root: Path,
    batch_id: str,
    *,
    status: str,
    runtime_session_id: str | None = None,
) -> None:
    store, _settings = _object_store(runtime_root)
    payload = {
        "batch_id": batch_id,
        "operation_kind": "memory_asset_package_import",
        "operation_id": batch_id,
        "source_type": "roundtrip",
        "status": status,
        "runtime_session_id": runtime_session_id or _MEMORY_IMPORT_RUNTIME_SESSION_ID,
        "total": 0,
        "succeeded": 0,
        "failed": 0,
        "needs_review": 0,
        "candidate_count": 0,
        "skipped": 0,
        "conflicted": 0,
        "created_at": "2026-08-16T00:00:00Z",
        "completed_at": "",
        "failures": [],
        "conflict_ids": [],
        "series": [],
        "items": [],
    }
    store.write(
        _MEMORY_IMPORT_BATCHES_COLLECTION,
        batch_id,
        payload,
        expected_revision=0,
    )


def _read_batch(runtime_root: Path, batch_id: str) -> dict[str, object]:
    store, _settings = _object_store(runtime_root)
    record = store.read(_MEMORY_IMPORT_BATCHES_COLLECTION, batch_id)
    assert record is not None
    return dict(record)


def test_stranded_processing_batch_marked_interrupted(tmp_path: Path) -> None:
    _write_batch(tmp_path, "roundtrip-stale", status="processing", runtime_session_id="old-session")
    application = FastAPI()

    report = recover_memory_import_batches(application, tmp_path)

    assert isinstance(report, MemoryImportBatchStartupRecoveryReport)
    assert report.scanned == 1
    assert report.interrupted == 1
    assert report.failed == 0
    record = _read_batch(tmp_path, "roundtrip-stale")
    assert record["status"] == "interrupted"
    assert record["runtime_session_id"] == "old-session"
    assert isinstance(record["interrupted_at"], str) and record["interrupted_at"]
    assert application.state.memory_import_batch_startup_recovery is report


def test_current_session_processing_batch_untouched(tmp_path: Path) -> None:
    _write_batch(tmp_path, "roundtrip-live", status="processing")
    application = FastAPI()

    report = recover_memory_import_batches(application, tmp_path)

    assert report.scanned == 0
    assert report.attempted == 0
    record = _read_batch(tmp_path, "roundtrip-live")
    assert record["status"] == "processing"
    assert "interrupted_at" not in record


def test_terminal_batches_untouched(tmp_path: Path) -> None:
    _write_batch(
        tmp_path,
        "roundtrip-done",
        status="completed",
        runtime_session_id="old-session",
    )
    _write_batch(
        tmp_path,
        "roundtrip-partial",
        status="partial",
        runtime_session_id="old-session",
    )
    application = FastAPI()

    report = recover_memory_import_batches(application, tmp_path)

    assert report.scanned == 0
    assert _read_batch(tmp_path, "roundtrip-done")["status"] == "completed"
    assert _read_batch(tmp_path, "roundtrip-partial")["status"] == "partial"


def test_no_batches_empty_report(tmp_path: Path) -> None:
    application = FastAPI()

    report = recover_memory_import_batches(application, tmp_path)

    assert report.scanned == 0
    assert report.items == ()


def test_max_batches_defers_excess(tmp_path: Path) -> None:
    for index in range(3):
        _write_batch(
            tmp_path,
            f"roundtrip-stale-{index}",
            status="processing",
            runtime_session_id="old-session",
        )
    application = FastAPI()

    report = recover_memory_import_batches(application, tmp_path, max_batches=2)

    assert report.scanned == 3
    assert report.interrupted == 2
    assert report.deferred == 1


def test_invalid_max_batches_rejected(tmp_path: Path) -> None:
    application = FastAPI()
    try:
        recover_memory_import_batches(application, tmp_path, max_batches=0)
    except ValueError as error:
        assert str(error) == "max_batches must be a positive integer"
    else:
        raise AssertionError("max_batches=0 must raise ValueError")
