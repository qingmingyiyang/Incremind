from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI

from backend.api.external_series_apply_saga import (
    ExternalSeriesApplyConflict,
    ExternalSeriesApplyError,
    ExternalSeriesApplySagaService,
)
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.aggregate_repository_factory import STRUCTURED_DATABASE_NAME
from core.storage_provider import (
    ExternalSeriesApplySagaError,
    SQLiteExternalSeriesApplySagaStore,
    SQLiteStructuredRecordStore,
)


DEFAULT_EXTERNAL_SERIES_APPLY_RECOVERY_LIMIT = 100


@dataclass(frozen=True, slots=True)
class ExternalSeriesApplyStartupRecoveryItem:
    operation_id: str
    initial_state: str
    outcome: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ExternalSeriesApplyStartupRecoveryReport:
    scanned: int
    attempted: int
    recovered: int
    failed: int
    deferred: int
    items: tuple[ExternalSeriesApplyStartupRecoveryItem, ...]


def quarantine_legacy_external_series_apply_sagas(
    application: FastAPI,
    runtime_root: Path,
) -> ExternalSeriesApplyStartupRecoveryReport:
    """Record unfinished pre-D-042 direct writes without replaying them on startup."""

    database_path = runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    if not database_path.exists():
        return _record(application, (), scanned=0, deferred=0)
    operations = SQLiteExternalSeriesApplySagaStore(SQLiteStructuredRecordStore(database_path))
    recoverable = operations.list_recoverable()
    items = tuple(
        ExternalSeriesApplyStartupRecoveryItem(
            operation.operation_id,
            operation.state,
            "failed",
            "legacy_direct_series_apply_quarantined",
        )
        for operation in recoverable
    )
    report = _record(application, items, scanned=len(recoverable), deferred=0)
    application.state.external_series_apply_startup_recovery = report
    return report


def recover_external_series_apply_sagas(application: FastAPI, runtime_root: Path, *,
                                        max_operations: int = DEFAULT_EXTERNAL_SERIES_APPLY_RECOVERY_LIMIT):
    if not isinstance(max_operations, int) or isinstance(max_operations, bool) or max_operations < 1:
        raise ValueError("max_operations must be a positive integer")
    database_path = runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    if not database_path.exists():
        return _record(application, (), scanned=0, deferred=0)

    operations = SQLiteExternalSeriesApplySagaStore(SQLiteStructuredRecordStore(database_path))
    recoverable = operations.list_recoverable()
    selected = recoverable[:max_operations]
    objects, settings = build_rebuild_object_store(runtime_root)
    items: list[ExternalSeriesApplyStartupRecoveryItem] = []
    for operation in selected:
        try:
            if operation.evidence.namespace_id != settings.namespace_id:
                raise _RecoveryRejected("namespace_drift")
            if operation.evidence.authority_identity != "json:object-store-v1":
                raise _RecoveryRejected("authority_drift")
            ExternalSeriesApplySagaService(
                objects=objects, operations=operations, namespace_id=settings.namespace_id,
                authority_identity="json:object-store-v1",
            ).apply(operation.operation_id, expected_object_revision=operation.evidence.base_object_revision)
            items.append(ExternalSeriesApplyStartupRecoveryItem(operation.operation_id, operation.state, "recovered"))
        except Exception as exc:
            items.append(ExternalSeriesApplyStartupRecoveryItem(
                operation.operation_id, operation.state, "failed", _stable_error_code(exc)
            ))
    return _record(application, tuple(items), scanned=len(recoverable), deferred=len(recoverable) - len(selected))


class _RecoveryRejected(RuntimeError):
    pass


def _stable_error_code(exc):
    if isinstance(exc, _RecoveryRejected):
        return str(exc)
    if isinstance(exc, ExternalSeriesApplyConflict):
        return "evidence_conflict"
    if isinstance(exc, ExternalSeriesApplyError):
        return "operation_invalid"
    if isinstance(exc, ExternalSeriesApplySagaError):
        return "operation_store_invalid"
    return "recovery_failed"


def _record(application, items, *, scanned, deferred):
    report = ExternalSeriesApplyStartupRecoveryReport(
        scanned=scanned, attempted=len(items), recovered=sum(item.outcome == "recovered" for item in items),
        failed=sum(item.outcome == "failed" for item in items), deferred=deferred, items=items,
    )
    application.state.external_series_apply_startup_recovery = report
    return report
