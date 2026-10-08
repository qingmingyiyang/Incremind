from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI


DEFAULT_MEMORY_IMPORT_BATCH_RECOVERY_LIMIT = 100


@dataclass(frozen=True, slots=True)
class MemoryImportBatchStartupRecoveryItem:
    batch_id: str
    previous_status: str
    outcome: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryImportBatchStartupRecoveryReport:
    scanned: int
    attempted: int
    interrupted: int
    failed: int
    deferred: int
    items: tuple[MemoryImportBatchStartupRecoveryItem, ...]


def recover_memory_import_batches(
    application: FastAPI,
    runtime_root: Path,
    *,
    max_batches: int = DEFAULT_MEMORY_IMPORT_BATCH_RECOVERY_LIMIT,
) -> MemoryImportBatchStartupRecoveryReport:
    """Mark memory import batches stranded as processing by an earlier process run.

    The importer replays safely per item (same-record skip plus revision-0 CAS),
    but the package payload is not persisted, so a crashed import cannot be
    auto-replayed. Stranded batches are therefore marked ``interrupted``
    honestly instead of staying ``processing`` forever.
    """
    if (
        not isinstance(max_batches, int)
        or isinstance(max_batches, bool)
        or max_batches < 1
    ):
        raise ValueError("max_batches must be a positive integer")

    from backend.api.routes.product.memory_import_records import (
        _MEMORY_IMPORT_BATCHES_COLLECTION,
        _MEMORY_IMPORT_RUNTIME_SESSION_ID,
    )

    from backend.api.routes.product.repositories import _object_store

    store, _settings = _object_store(runtime_root)
    stranded = [
        record
        for record in store.list(_MEMORY_IMPORT_BATCHES_COLLECTION)
        if isinstance(record, dict)
        and record.get("status") == "processing"
        and record.get("runtime_session_id") != _MEMORY_IMPORT_RUNTIME_SESSION_ID
    ]
    selected = stranded[:max_batches]
    items: list[MemoryImportBatchStartupRecoveryItem] = []
    interrupted_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for record in selected:
        batch_id = str(record.get("batch_id") or record.get("id") or "")
        if not batch_id:
            continue
        try:
            updated = dict(record)
            updated["status"] = "interrupted"
            updated["interrupted_at"] = interrupted_at
            updated["interrupted_reason"] = (
                "runtime session ended before import batch finalized"
            )
            store.write(
                _MEMORY_IMPORT_BATCHES_COLLECTION,
                batch_id,
                updated,
                expected_revision=store.revision(
                    _MEMORY_IMPORT_BATCHES_COLLECTION, batch_id
                ),
            )
            items.append(
                MemoryImportBatchStartupRecoveryItem(
                    batch_id=batch_id,
                    previous_status="processing",
                    outcome="interrupted",
                )
            )
        except Exception as error:  # noqa: BLE001
            items.append(
                MemoryImportBatchStartupRecoveryItem(
                    batch_id=batch_id,
                    previous_status="processing",
                    outcome="failed",
                    error_code=_stable_error_code(error),
                )
            )
    report = MemoryImportBatchStartupRecoveryReport(
        scanned=len(stranded),
        attempted=len(items),
        interrupted=sum(item.outcome == "interrupted" for item in items),
        failed=sum(item.outcome == "failed" for item in items),
        deferred=len(stranded) - len(selected),
        items=tuple(items),
    )
    application.state.memory_import_batch_startup_recovery = report
    return report


def _stable_error_code(error: Exception) -> str:
    from core.storage_provider import ObjectStoreRevisionError

    if isinstance(error, ObjectStoreRevisionError):
        return "batch_revision_conflict"
    return "interrupt_failed"
