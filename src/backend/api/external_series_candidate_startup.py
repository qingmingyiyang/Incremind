from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

from fastapi import FastAPI

from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.effect_log import (
    EffectClass,
    EffectHandlerAbandoned,
    EffectHandlerRegistration,
    EffectIntent,
    EffectLog,
    EffectRunner,
    EffectState,
)

from backend.api.external_series_candidate_saga import (
    ExternalSeriesCandidateConflict,
    ExternalSeriesCandidateError,
    ExternalSeriesCandidateSagaService,
    SQLiteSeriesCurrentProjection,
)
from core.aggregate_repository_factory import (
    STRUCTURED_DATABASE_NAME,
    AggregateRepositoryFactory,
    AggregateRepositoryFactoryError,
)
from core.storage_provider import (
    ExternalSeriesCandidateSagaError,
    SQLiteExternalSeriesCandidateSagaStore,
    SQLiteStructuredRecordStore,
)


DEFAULT_EXTERNAL_SERIES_CANDIDATE_RECOVERY_LIMIT = 100


def register_external_series_candidate_handler(runtime_root: Path, effect_runtime) -> None:
    database_path = runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    operations = SQLiteExternalSeriesCandidateSagaStore(
        SQLiteStructuredRecordStore(database_path)
    )

    def handle(effect) -> str:
        operation = operations.get(effect.operation_id)
        if operation is None:
            raise EffectHandlerAbandoned("external_series_candidate.operation_missing")
        objects, settings = build_rebuild_object_store(runtime_root)
        evidence = operation.evidence
        if evidence.namespace_id != settings.namespace_id:
            raise EffectHandlerAbandoned("external_series_candidate.namespace_drift")
        current = None
        if evidence.authority_identity == "sqlite:structured-records-v1":
            resolution = AggregateRepositoryFactory(
                runtime_root=runtime_root,
                namespace_id=settings.namespace_id,
                json_store=objects,
            ).memory_publication_authority_resolution()
            if (
                resolution.authority_identity != evidence.authority_identity
                or resolution.records is None
            ):
                raise EffectHandlerAbandoned("external_series_candidate.authority_drift")
            current = SQLiteSeriesCurrentProjection(resolution.records)
        elif evidence.authority_identity != "json:object-store-v1":
            raise EffectHandlerAbandoned("external_series_candidate.authority_drift")
        result = ExternalSeriesCandidateSagaService(
            objects=objects,
            operations=operations,
            namespace_id=settings.namespace_id,
            authority_identity=evidence.authority_identity,
            current=current,
        ).apply(
            operation.operation_id,
            expected_object_revision=evidence.base_object_revision,
        )
        return (
            f"crp://{settings.namespace_id}/external-series-candidate-receipts/"
            f"{operation.operation_id}:r{result.operation_revision}"
        )

    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind="external_series_candidate",
        effect_class=EffectClass.IDEMPOTENT,
        handler=handle,
    ))


@dataclass(frozen=True, slots=True)
class ExternalSeriesCandidateStartupRecoveryItem:
    operation_id: str
    initial_state: str
    outcome: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ExternalSeriesCandidateStartupRecoveryReport:
    scanned: int
    attempted: int
    recovered: int
    failed: int
    deferred: int
    items: tuple[ExternalSeriesCandidateStartupRecoveryItem, ...]


def backfill_external_series_candidate_effects(
    runtime_root: Path, effects: EffectLog, *,
    max_operations: int = DEFAULT_EXTERNAL_SERIES_CANDIDATE_RECOVERY_LIMIT,
) -> tuple[str, ...]:
    database_path = runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    if not database_path.exists():
        return ()
    operations = SQLiteExternalSeriesCandidateSagaStore(SQLiteStructuredRecordStore(database_path))
    now = int(time.time())
    backfilled: list[str] = []
    for operation in operations.list_recoverable()[:max_operations]:
        evidence = operation.evidence
        intent = EffectIntent(
            session_id=f"external-series-candidate:{evidence.series_id}",
            root_id=evidence.series_memory_id, step_key="create-review-candidate",
            kind="external_series_candidate", effect_class=EffectClass.IDEMPOTENT,
            intent_ref=f"crp://{evidence.namespace_id}/external-series-candidate-intents/{operation.operation_id}",
            gate_decision_id="external-series-candidate-review",
            rev_set={
                "base_object_revision": evidence.base_object_revision,
                "base_series_revision": evidence.base_series_revision,
                "payload_sha256": evidence.payload_sha256,
                "authority_identity": evidence.authority_identity,
            },
            payload={
                "operation_id": operation.operation_id,
                "series_id": evidence.series_id,
                "series_memory_id": evidence.series_memory_id,
                "candidate_id": evidence.candidate_id,
            },
            operation_id_override=operation.operation_id,
        )
        effect, _created = effects.plan(intent, now=now - 2)
        if effect.state is EffectState.PLANNED:
            effects.transition(
                operation.operation_id, expected=EffectState.PLANNED,
                target=EffectState.INFLIGHT, now=now - 2,
                lease_owner="legacy-external-series-candidate",
                lease_expires_at=now - 1, increment_attempt=True,
            )
        backfilled.append(operation.operation_id)
    return tuple(backfilled)


def dispatch_external_series_candidate_effects(
    application: FastAPI,
    runtime_root: Path,
    runner: EffectRunner,
    *,
    max_operations: int = DEFAULT_EXTERNAL_SERIES_CANDIDATE_RECOVERY_LIMIT,
) -> ExternalSeriesCandidateStartupRecoveryReport:
    if not isinstance(max_operations, int) or isinstance(max_operations, bool) or max_operations < 1:
        raise ValueError("max_operations must be a positive integer")
    effects = runner.log
    database_path = runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    if not database_path.exists():
        return _record(application, (), scanned=0, deferred=0)

    operations = SQLiteExternalSeriesCandidateSagaStore(SQLiteStructuredRecordStore(database_path))
    recoverable = operations.list_recoverable()
    selected = tuple(
        operation for operation in recoverable
        if _planned(effects, operation.operation_id)
    )[:max_operations]
    objects, settings = build_rebuild_object_store(runtime_root)
    items: list[ExternalSeriesCandidateStartupRecoveryItem] = []
    for operation in selected:
        try:
            if operation.evidence.namespace_id != settings.namespace_id:
                raise _RecoveryRejected("namespace_drift")
            current = None
            if operation.evidence.authority_identity == "sqlite:structured-records-v1":
                resolution = AggregateRepositoryFactory(
                    runtime_root=runtime_root,
                    namespace_id=settings.namespace_id,
                    json_store=objects,
                ).memory_publication_authority_resolution()
                if resolution.authority_identity != operation.evidence.authority_identity or resolution.records is None:
                    raise _RecoveryRejected("authority_drift")
                current = SQLiteSeriesCurrentProjection(resolution.records)
            elif operation.evidence.authority_identity != "json:object-store-v1":
                raise _RecoveryRejected("authority_drift")
            def apply(_effect):
                result = ExternalSeriesCandidateSagaService(
                    objects=objects,
                    operations=operations,
                    namespace_id=settings.namespace_id,
                    authority_identity=operation.evidence.authority_identity,
                    current=current,
                ).apply(
                    operation.operation_id,
                    expected_object_revision=operation.evidence.base_object_revision,
                )
                return (
                    f"crp://{settings.namespace_id}/external-series-candidate-receipts/"
                    f"{operation.operation_id}:r{result.operation_revision}"
                )

            runner.execute_planned(
                operation.operation_id, apply, now=int(time.time()),
                receipt_kind="external-series-candidate-receipt",
            )
            items.append(
                ExternalSeriesCandidateStartupRecoveryItem(
                    operation.operation_id,
                    operation.state,
                    "recovered",
                )
            )
        except Exception as error:
            items.append(
                ExternalSeriesCandidateStartupRecoveryItem(
                    operation.operation_id,
                    operation.state,
                    "failed",
                    _stable_error_code(error),
                )
            )
    return _record(application, tuple(items), scanned=len(recoverable), deferred=len(recoverable) - len(selected))


def _planned(effects: EffectLog, operation_id: str) -> bool:
    try:
        return effects.get(operation_id).state is EffectState.PLANNED
    except KeyError:
        return False


class _RecoveryRejected(RuntimeError):
    pass


def _stable_error_code(error: Exception) -> str:
    if isinstance(error, _RecoveryRejected):
        return str(error)
    if isinstance(error, ExternalSeriesCandidateConflict):
        return "evidence_conflict"
    if isinstance(error, ExternalSeriesCandidateError):
        return "operation_invalid"
    if isinstance(error, ExternalSeriesCandidateSagaError):
        return "operation_store_invalid"
    if isinstance(error, AggregateRepositoryFactoryError):
        return "authority_drift"
    return "recovery_failed"


def _record(
    application: FastAPI,
    items: tuple[ExternalSeriesCandidateStartupRecoveryItem, ...],
    *,
    scanned: int,
    deferred: int,
) -> ExternalSeriesCandidateStartupRecoveryReport:
    report = ExternalSeriesCandidateStartupRecoveryReport(
        scanned=scanned,
        attempted=len(items),
        recovered=sum(item.outcome == "recovered" for item in items),
        failed=sum(item.outcome == "failed" for item in items),
        deferred=deferred,
        items=items,
    )
    application.state.external_series_candidate_startup_recovery = report
    return report
