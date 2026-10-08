"""Bounded sidecar-startup recovery for Shared Trust Audit activation."""

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

from core.aggregate_repository_factory import (
    AUTHORITY_DATABASE_NAME,
    STRUCTURED_DATABASE_NAME,
    TARGET_IDENTITY,
)
from core.shared_trust_audit_activation_service import (
    SharedTrustAuditActivationSagaService,
    SharedTrustAuditActivationServiceError,
)
from core.storage_provider import (
    SQLiteAggregateAuthorityStore,
    SQLiteSharedTrustAuditActivationSagaStore,
    SQLiteStructuredRecordStore,
    SharedTrustAuditActivationSagaConflict,
    SharedTrustAuditActivationSagaError,
)


DEFAULT_SHARED_TRUST_AUDIT_ACTIVATION_RECOVERY_LIMIT = 100


def register_shared_trust_activation_handler(runtime_root: Path, effect_runtime) -> None:
    rebuild_root = runtime_root / ".rebuild-data"
    records = SQLiteStructuredRecordStore(rebuild_root / STRUCTURED_DATABASE_NAME)
    operations = SQLiteSharedTrustAuditActivationSagaStore(records)

    def handle(effect) -> str:
        operation = operations.get(effect.operation_id)
        if operation is None:
            raise EffectHandlerAbandoned("shared_trust_activation.operation_missing")
        _store, settings = build_rebuild_object_store(runtime_root)
        evidence = operation.evidence
        if evidence.namespace_id != settings.namespace_id:
            raise EffectHandlerAbandoned("shared_trust_activation.namespace_drift")
        if evidence.target_identity != TARGET_IDENTITY:
            raise EffectHandlerAbandoned("shared_trust_activation.target_identity_drift")
        authority_path = rebuild_root / AUTHORITY_DATABASE_NAME
        if not authority_path.exists():
            raise SharedTrustAuditActivationServiceError("authority store is unavailable")
        result = SharedTrustAuditActivationSagaService(
            operations=operations,
            records=records,
            authority=SQLiteAggregateAuthorityStore(authority_path),
        ).activate(evidence)
        return (
            f"crp://{evidence.namespace_id}/shared-trust-activation-receipts/"
            f"{result.attestation_id}:r{result.operation.revision}"
        )

    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind="shared_trust_activation",
        effect_class=EffectClass.IDEMPOTENT,
        handler=handle,
    ))


@dataclass(frozen=True, slots=True)
class SharedTrustAuditActivationStartupRecoveryItem:
    operation_id: str
    initial_state: str
    outcome: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class SharedTrustAuditActivationStartupRecoveryReport:
    scanned: int
    attempted: int
    recovered: int
    failed: int
    deferred: int
    items: tuple[SharedTrustAuditActivationStartupRecoveryItem, ...]


def backfill_shared_trust_activation_effects(
    runtime_root: Path, effects: EffectLog, *,
    max_operations: int = DEFAULT_SHARED_TRUST_AUDIT_ACTIVATION_RECOVERY_LIMIT,
) -> tuple[str, ...]:
    records_path = runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    if not records_path.exists():
        return ()
    operations = SQLiteSharedTrustAuditActivationSagaStore(SQLiteStructuredRecordStore(records_path))
    intents = (
        EffectIntent(
            session_id=f"shared-trust-activation:{op.evidence.namespace_id}",
            root_id=op.evidence.activation_id, step_key="activate-shared-trust-authorities",
            kind="shared_trust_activation", effect_class=EffectClass.IDEMPOTENT,
            intent_ref=f"crp://{op.evidence.namespace_id}/shared-trust-activation-intents/{op.operation_id}",
            gate_decision_id="shared-trust-audit-attestation",
            rev_set={
                "source_fingerprint": op.evidence.source_fingerprint,
                "target_fingerprint": op.evidence.target_fingerprint,
                "target_identity": op.evidence.target_identity,
                "member_migrations": ",".join(
                    f"{key}:{value}" for key, value in sorted(op.evidence.member_migrations.items())
                ),
            },
            payload={
                "operation_id": op.operation_id,
                "activation_id": op.evidence.activation_id,
                "member_migrations": dict(op.evidence.member_migrations),
            },
            operation_id_override=op.operation_id,
        )
        for op in operations.list_recoverable()[:max_operations]
    )
    return backfill_interrupted_effects(
        effects, intents, now=int(time.time()),
        lease_owner="legacy-shared-trust-activation",
    )


def dispatch_shared_trust_activation_effects(
    application: FastAPI,
    runtime_root: Path,
    runner: EffectRunner,
    *,
    max_operations: int = DEFAULT_SHARED_TRUST_AUDIT_ACTIVATION_RECOVERY_LIMIT,
) -> SharedTrustAuditActivationStartupRecoveryReport:
    """Resume a bounded activation batch through the existing durable service."""

    if not isinstance(max_operations, int) or isinstance(max_operations, bool) or max_operations < 1:
        raise ValueError("max_operations must be a positive integer")
    effects = runner.log
    rebuild_root = runtime_root / ".rebuild-data"
    records_path = rebuild_root / STRUCTURED_DATABASE_NAME
    if not records_path.exists():
        return _record(application, (), scanned=0, deferred=0)
    records = SQLiteStructuredRecordStore(records_path)
    operations = SQLiteSharedTrustAuditActivationSagaStore(records)
    recoverable = operations.list_recoverable()
    selected = tuple(
        operation for operation in recoverable
        if is_effect_planned(effects, operation.operation_id)
    )[:max_operations]
    authority_path = rebuild_root / AUTHORITY_DATABASE_NAME
    if not authority_path.exists():
        return _record(
            application,
            tuple(
                SharedTrustAuditActivationStartupRecoveryItem(
                    operation.operation_id,
                    operation.state,
                    "failed",
                    "authority_store_missing",
                )
                for operation in selected
            ),
            scanned=len(recoverable),
            deferred=len(recoverable) - len(selected),
        )

    _store, settings = build_rebuild_object_store(runtime_root)
    service = SharedTrustAuditActivationSagaService(
        operations=operations,
        records=records,
        authority=SQLiteAggregateAuthorityStore(authority_path),
    )
    items: list[SharedTrustAuditActivationStartupRecoveryItem] = []
    for operation in selected:
        try:
            if operation.evidence.namespace_id != settings.namespace_id:
                raise _RecoveryRejected("namespace_drift")
            if operation.evidence.target_identity != TARGET_IDENTITY:
                raise _RecoveryRejected("target_identity_drift")
            def activate(_effect):
                result = service.activate(operation.evidence)
                return (
                    f"crp://{operation.evidence.namespace_id}/shared-trust-activation-receipts/"
                    f"{result.attestation_id}:r{result.operation.revision}"
                )

            runner.execute_planned(
                operation.operation_id, activate, now=int(time.time()),
                receipt_kind="shared-trust-activation-receipt",
            )
            items.append(
                SharedTrustAuditActivationStartupRecoveryItem(
                    operation.operation_id, operation.state, "recovered"
                )
            )
        except Exception as exc:  # one invalid proof cannot block the bounded batch
            items.append(
                SharedTrustAuditActivationStartupRecoveryItem(
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
    if isinstance(exc, SharedTrustAuditActivationServiceError):
        return "activation_proof_invalid"
    if isinstance(exc, SharedTrustAuditActivationSagaConflict):
        return "operation_conflict"
    if isinstance(exc, SharedTrustAuditActivationSagaError):
        return "operation_store_invalid"
    return "recovery_failed"


def _record(
    application: FastAPI,
    items: tuple[SharedTrustAuditActivationStartupRecoveryItem, ...],
    *,
    scanned: int,
    deferred: int,
) -> SharedTrustAuditActivationStartupRecoveryReport:
    report = SharedTrustAuditActivationStartupRecoveryReport(
        scanned=scanned,
        attempted=len(items),
        recovered=sum(item.outcome == "recovered" for item in items),
        failed=sum(item.outcome == "failed" for item in items),
        deferred=deferred,
        items=items,
    )
    application.state.shared_trust_audit_activation_startup_recovery = report
    return report
