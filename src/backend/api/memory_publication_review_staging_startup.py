"""Bounded sidecar-startup recovery for generic Memory review staging."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

from fastapi import FastAPI
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.effect_log import (
    EffectClass, EffectHandlerAbandoned, EffectHandlerRegistration, EffectIntent, EffectLog, EffectRunner,
    backfill_interrupted_effects, is_effect_planned,
)

from core.aggregate_repository_factory import STRUCTURED_DATABASE_NAME
from core.product_core.memory_publication_review_staging_saga_service import (
    MemoryPublicationReviewStagingSagaService,
    MemoryPublicationReviewStagingServiceConflict,
    MemoryPublicationReviewStagingServiceError,
)
from core.storage_provider import (
    MemoryPublicationReviewStagingSagaError,
    SQLiteMemoryPublicationReviewStagingSagaStore,
    SQLiteStructuredRecordStore,
)


DEFAULT_MEMORY_PUBLICATION_REVIEW_STAGING_RECOVERY_LIMIT = 100


def register_memory_review_staging_handler(runtime_root: Path, effect_runtime) -> None:
    database_path = runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    records = SQLiteStructuredRecordStore(database_path)
    operations = SQLiteMemoryPublicationReviewStagingSagaStore(records)

    def handle(effect) -> str:
        operation = operations.get(effect.operation_id)
        if operation is None:
            raise EffectHandlerAbandoned("memory_review_staging.operation_missing")
        candidates, settings = build_rebuild_object_store(runtime_root)
        evidence = operation.evidence
        if evidence.namespace_id != settings.namespace_id:
            raise EffectHandlerAbandoned("memory_review_staging.namespace_drift")
        if evidence.candidate_authority != "json:object-store-v1":
            raise EffectHandlerAbandoned("memory_review_staging.candidate_authority_drift")
        if evidence.staging_authority != "sqlite:structured-records-v1":
            raise EffectHandlerAbandoned("memory_review_staging.staging_authority_drift")
        MemoryPublicationReviewStagingSagaService(
            candidates, records, operations,
        ).resume(operation.operation_id)
        settled = operations.get(operation.operation_id)
        if settled is None:
            raise MemoryPublicationReviewStagingServiceError(
                "review staging terminal record disappeared"
            )
        return (
            f"crp://{settings.namespace_id}/memory-review-staging-receipts/"
            f"{operation.operation_id}:r{settled.revision}"
        )

    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind="memory_review_staging",
        effect_class=EffectClass.IDEMPOTENT,
        handler=handle,
    ))


@dataclass(frozen=True, slots=True)
class MemoryPublicationReviewStagingStartupRecoveryItem:
    operation_id: str
    initial_state: str
    outcome: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryPublicationReviewStagingStartupRecoveryReport:
    scanned: int
    attempted: int
    recovered: int
    failed: int
    deferred: int
    items: tuple[MemoryPublicationReviewStagingStartupRecoveryItem, ...]


def backfill_memory_review_staging_effects(
    runtime_root: Path, effects: EffectLog, *,
    max_operations: int = DEFAULT_MEMORY_PUBLICATION_REVIEW_STAGING_RECOVERY_LIMIT,
) -> tuple[str, ...]:
    database_path = runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    if not database_path.exists():
        return ()
    operations = SQLiteMemoryPublicationReviewStagingSagaStore(SQLiteStructuredRecordStore(database_path))
    intents = (
        EffectIntent(
            session_id=f"memory-review-staging:{op.evidence.candidate_id}",
            root_id=op.evidence.candidate_id, step_key="stage-reviewed-memory-candidate",
            kind="memory_review_staging", effect_class=EffectClass.IDEMPOTENT,
            intent_ref=f"crp://{op.evidence.namespace_id}/memory-review-staging-intents/{op.operation_id}",
            gate_decision_id="memory-review-confirmation",
            rev_set={
                "candidate_revision": op.evidence.candidate_revision,
                "candidate_sha256": op.evidence.candidate_sha256,
                "draft_sha256": op.evidence.draft_sha256,
                "context_sha256": op.evidence.context_sha256,
                "candidate_authority": op.evidence.candidate_authority,
                "staging_authority": op.evidence.staging_authority,
            },
            payload={
                "operation_id": op.operation_id, "candidate_id": op.evidence.candidate_id,
                "layer": op.evidence.layer, "draft_id": op.evidence.draft_id,
                "context_id": op.evidence.context_id,
            },
            operation_id_override=op.operation_id,
        )
        for op in operations.list_recoverable()[:max_operations]
    )
    return backfill_interrupted_effects(
        effects, intents, now=int(time.time()),
        lease_owner="legacy-memory-review-staging",
    )


def dispatch_memory_review_staging_effects(
    application: FastAPI,
    runtime_root: Path,
    runner: EffectRunner,
    *,
    max_operations: int = DEFAULT_MEMORY_PUBLICATION_REVIEW_STAGING_RECOVERY_LIMIT,
) -> MemoryPublicationReviewStagingStartupRecoveryReport:
    """Resume bounded generic review operations without delaying sidecar startup."""

    if not isinstance(max_operations, int) or isinstance(max_operations, bool) or max_operations < 1:
        raise ValueError("max_operations must be a positive integer")
    effects = runner.log
    database_path = runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    if not database_path.exists():
        return _record(application, (), scanned=0, deferred=0)
    candidates, settings = build_rebuild_object_store(runtime_root)
    records = SQLiteStructuredRecordStore(database_path)
    operations = SQLiteMemoryPublicationReviewStagingSagaStore(records)
    recoverable = operations.list_recoverable()
    selected = tuple(
        operation for operation in recoverable
        if is_effect_planned(effects, operation.operation_id)
    )[:max_operations]
    items: list[MemoryPublicationReviewStagingStartupRecoveryItem] = []
    for operation in selected:
        try:
            if operation.evidence.namespace_id != settings.namespace_id:
                raise _RecoveryRejected("namespace_drift")
            if operation.evidence.candidate_authority != "json:object-store-v1":
                raise _RecoveryRejected("candidate_authority_drift")
            if operation.evidence.staging_authority != "sqlite:structured-records-v1":
                raise _RecoveryRejected("staging_authority_drift")
            def stage(_effect):
                MemoryPublicationReviewStagingSagaService(
                    candidates, records, operations,
                ).resume(operation.operation_id)
                settled = operations.get(operation.operation_id)
                if settled is None:
                    raise MemoryPublicationReviewStagingServiceError(
                        "review staging terminal record disappeared"
                    )
                return (
                    f"crp://{settings.namespace_id}/memory-review-staging-receipts/"
                    f"{operation.operation_id}:r{settled.revision}"
                )

            runner.execute_planned(
                operation.operation_id, stage, now=int(time.time()),
                receipt_kind="memory-review-staging-receipt",
            )
            items.append(
                MemoryPublicationReviewStagingStartupRecoveryItem(
                    operation.operation_id,
                    operation.state,
                    "recovered",
                )
            )
        except Exception as exc:  # isolate one durable operation in the bounded batch
            items.append(
                MemoryPublicationReviewStagingStartupRecoveryItem(
                    operation.operation_id,
                    operation.state,
                    "failed",
                    _stable_error_code(exc),
                )
            )
    return _record(
        application,
        tuple(items),
        scanned=len(recoverable),
        deferred=len(recoverable) - len(selected),
    )


class _RecoveryRejected(RuntimeError):
    pass


def _stable_error_code(exc: Exception) -> str:
    if isinstance(exc, _RecoveryRejected):
        return str(exc)
    if isinstance(exc, MemoryPublicationReviewStagingServiceConflict):
        return "evidence_conflict"
    if isinstance(exc, MemoryPublicationReviewStagingServiceError):
        return "operation_invalid"
    if isinstance(exc, MemoryPublicationReviewStagingSagaError):
        return "operation_store_invalid"
    return "recovery_failed"


def _record(
    application: FastAPI,
    items: tuple[MemoryPublicationReviewStagingStartupRecoveryItem, ...],
    *,
    scanned: int,
    deferred: int,
) -> MemoryPublicationReviewStagingStartupRecoveryReport:
    report = MemoryPublicationReviewStagingStartupRecoveryReport(
        scanned=scanned,
        attempted=len(items),
        recovered=sum(item.outcome == "recovered" for item in items),
        failed=sum(item.outcome == "failed" for item in items),
        deferred=deferred,
        items=items,
    )
    application.state.memory_publication_review_staging_startup_recovery = report
    return report
