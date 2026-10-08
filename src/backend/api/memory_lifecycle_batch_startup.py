from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI

from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.product_core.memory_lifecycle import GovernedMemoryLifecycle


DEFAULT_MEMORY_LIFECYCLE_BATCH_RECOVERY_LIMIT = 100


@dataclass(frozen=True, slots=True)
class MemoryLifecycleBatchRecoveryReport:
    scanned: int
    resumed: int
    failed: int
    deferred: int
    operation_ids: tuple[str, ...]


def recover_memory_lifecycle_batches(
    application: FastAPI,
    runtime_root: Path,
    *,
    limit: int = DEFAULT_MEMORY_LIFECYCLE_BATCH_RECOVERY_LIMIT,
) -> MemoryLifecycleBatchRecoveryReport:
    """Resume previously confirmed soft-redaction or undo operations."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 through 100")
    store, settings = build_rebuild_object_store(Path(runtime_root))
    pending = [
        record for record in store.list("memory_lifecycle_batches")
        if record.get("action") == "soft_redact"
        and record.get("status") in {"applying", "undoing"}
    ]
    selected = pending[:limit]
    lifecycle = GovernedMemoryLifecycle(store, namespace_id=settings.namespace_id)
    resumed: list[str] = []
    failed = 0
    for record in selected:
        operation_id = str(record.get("id") or "")
        if not operation_id:
            failed += 1
            continue
        try:
            lifecycle.resume_batch_soft_redact(operation_id=operation_id, confirm=True)
            resumed.append(operation_id)
        except Exception:  # noqa: BLE001 - durable operation remains retryable.
            failed += 1
    report = MemoryLifecycleBatchRecoveryReport(
        scanned=len(pending), resumed=len(resumed), failed=failed,
        deferred=len(pending) - len(selected), operation_ids=tuple(resumed),
    )
    application.state.memory_lifecycle_batch_startup_recovery = report
    return report
