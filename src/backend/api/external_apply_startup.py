from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

from fastapi import FastAPI
from core.effect_log import (
    EFFECT_V2,
    EffectClass,
    EffectReceipt,
    EffectHandlerAbandoned,
    EffectHandlerRegistration,
    EffectIntent,
    EffectLog,
    EffectRunner,
    EffectState,
    GateDecision,
    GateDecisionFact,
    NOT_APPLICABLE,
)

from backend.api.external_apply_saga import (
    ExternalDocumentApplyConflict,
    ExternalDocumentApplyError,
    ExternalDocumentApplySagaService,
)
from backend.api.rebuild_authority_runtime import (
    build_rebuild_document_repository_resolution,
)
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.aggregate_repository_factory import (
    STRUCTURED_DATABASE_NAME,
    AggregateRepositoryFactoryError,
)
from core.storage_provider import (
    ExternalApplySagaError,
    SQLiteExternalApplySagaStore,
    SQLiteStructuredRecordStore,
)


DEFAULT_EXTERNAL_APPLY_RECOVERY_LIMIT = 100
_V2_INTENT_SCHEMA = "external-document-apply-intent-v2"
_V2_RECEIPT_KIND = "external-document-apply-receipt-v2"
_V2_RECEIPT_SCHEMA = "external-document-apply-receipt-v2"
_V2_POLICY_REVISION = "external-document-user-review-v2"
_V2_HANDLER_REVISION = "external-document-apply-handler-v2"


def register_external_apply_handler(runtime_root: Path, effect_runtime) -> None:
    """Load document apply as a restart-safe Core Handler."""

    database_path = runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    records = SQLiteStructuredRecordStore(database_path)
    operations = SQLiteExternalApplySagaStore(records)

    def _apply(effect, *, operation_id: str) -> str:
        operation = operations.get(operation_id)
        if operation is None:
            raise EffectHandlerAbandoned("external_document_apply.operation_missing")
        store, settings = build_rebuild_object_store(runtime_root)
        evidence = operation.evidence
        if evidence.namespace_id != settings.namespace_id:
            raise EffectHandlerAbandoned("external_document_apply.namespace_drift")
        if evidence.document_authority_identity == "unbound:legacy":
            raise EffectHandlerAbandoned("external_document_apply.authority_unbound")
        resolution = build_rebuild_document_repository_resolution(runtime_root, store, settings)
        if resolution.authority_identity != evidence.document_authority_identity:
            raise EffectHandlerAbandoned("external_document_apply.authority_drift")
        result = ExternalDocumentApplySagaService(
            documents=resolution.repository,
            drafts=store,
            operations=operations,
            namespace_id=settings.namespace_id,
            document_authority_identity=resolution.authority_identity,
        ).apply(
            operation.operation_id,
            expected_revision=evidence.base_revision,
        )
        return (
            f"crp://{settings.namespace_id}/external-document-apply-receipts/"
            f"{operation.operation_id}:r{result.operation_revision}"
        )

    def handle_legacy(effect) -> str:
        return _apply(effect, operation_id=effect.operation_id)

    def handle_v2(effect) -> EffectReceipt:
        operation_id = _domain_operation_id(effect, fallback="")
        operation = operations.get(operation_id)
        _validate_v2_effect(effect, operation)
        receipt_ref = _apply(effect, operation_id=operation_id)
        return _receipt(receipt_ref)

    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind="external_document_apply",
        effect_class=EffectClass.IDEMPOTENT,
        handler=handle_legacy,
    ))
    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind="external_document_apply",
        effect_class=EffectClass.IDEMPOTENT,
        handler=handle_v2,
        contract_version=EFFECT_V2,
        intent_schema_version=_V2_INTENT_SCHEMA,
        receipt_kind=_V2_RECEIPT_KIND,
        receipt_schema_version=_V2_RECEIPT_SCHEMA,
    ))


def plan_external_document_apply(
    service: ExternalDocumentApplySagaService,
    effects: EffectLog,
    draft_id: str,
    *,
    expected_revision: int,
) -> str:
    operation = service.prepare(draft_id, expected_revision=expected_revision)
    evidence = operation.evidence
    intent = _v2_intent(operation)
    effect, _ = effects.plan_v2(
        intent,
        gate_decision_id=intent.gate_decision_id,
        gate_fact=_user_review_gate(intent),
        now=int(time.time()),
    )
    return effect.operation_id


def _v2_intent(operation) -> EffectIntent:
    evidence = operation.evidence
    return EffectIntent(
        session_id=f"external-document-apply:{evidence.document_id}",
        root_id=evidence.document_id,
        step_key="apply-reviewed-document",
        kind="external_document_apply",
        effect_class=EffectClass.IDEMPOTENT,
        intent_ref=f"crp://{evidence.namespace_id}/external-document-apply-intents/{operation.operation_id}",
        gate_decision_id=f"gate:external-document-user-review/{operation.operation_id}",
        rev_set={
            "policy": _V2_POLICY_REVISION,
            "boundary": evidence.document_authority_identity,
            "capability": NOT_APPLICABLE,
            "context_manifest": NOT_APPLICABLE,
            "provider": NOT_APPLICABLE,
            "model_route": NOT_APPLICABLE,
            "bundle": NOT_APPLICABLE,
            "handler": _V2_HANDLER_REVISION,
            "secret": NOT_APPLICABLE,
            "budget": NOT_APPLICABLE,
            "workflow": evidence.payload_sha256,
        },
        payload={
            "saga_operation_id": operation.operation_id,
            "document_id": evidence.document_id,
            "evidence_digest": evidence.payload_sha256,
        },
        contract_version=EFFECT_V2,
        intent_schema_version=_V2_INTENT_SCHEMA,
        expected_receipt_kind=_V2_RECEIPT_KIND,
        expected_receipt_schema_version=_V2_RECEIPT_SCHEMA,
    )


def _user_review_gate(intent: EffectIntent) -> GateDecisionFact:
    return GateDecisionFact(
        decision=GateDecision.ALLOW,
        rule_ref="rule:external-document-user-review/v2",
        scope_ref=f"scope:document/{intent.root_id}",
        budget_after={},
        secret_scope="scope:local-document-store",
        policy_revision=str(intent.rev_set["policy"]),
    )


def _receipt(receipt_ref: str) -> EffectReceipt:
    return EffectReceipt(
        receipt_ref=receipt_ref,
        receipt_kind=_V2_RECEIPT_KIND,
        receipt_schema_version=_V2_RECEIPT_SCHEMA,
        intent_schema_version=_V2_INTENT_SCHEMA,
    )


@dataclass(frozen=True, slots=True)
class ExternalApplyStartupRecoveryItem:
    operation_id: str
    initial_state: str
    outcome: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ExternalApplyStartupRecoveryReport:
    scanned: int
    attempted: int
    recovered: int
    failed: int
    deferred: int
    items: tuple[ExternalApplyStartupRecoveryItem, ...]


def backfill_external_apply_effects(
    runtime_root: Path, effects: EffectLog, *,
    max_operations: int = DEFAULT_EXTERNAL_APPLY_RECOVERY_LIMIT,
) -> tuple[str, ...]:
    database_path = runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    if not database_path.exists():
        return ()
    operations = SQLiteExternalApplySagaStore(SQLiteStructuredRecordStore(database_path))
    now = int(time.time())
    backfilled: list[str] = []
    for operation in operations.list_recoverable()[:max_operations]:
        # Historical v1 Effects use the Saga id as their Effect id and remain
        # executable through the legacy registration. A missing Effect is the
        # crash window after durable Saga preparation; reconstruct it as v2.
        try:
            effect = effects.get(operation.operation_id)
        except KeyError:
            intent = _v2_intent(operation)
            effect, _created = effects.plan_v2(
                intent,
                gate_decision_id=intent.gate_decision_id,
                gate_fact=_user_review_gate(intent),
                now=now,
            )
        backfilled.append(effect.operation_id)
    return tuple(backfilled)


def dispatch_external_apply_effects(
    application: FastAPI,
    runtime_root: Path,
    runner: EffectRunner,
    *,
    max_operations: int = DEFAULT_EXTERNAL_APPLY_RECOVERY_LIMIT,
) -> ExternalApplyStartupRecoveryReport:
    """Recover each durable operation once, bounded and isolated per startup."""

    if not isinstance(max_operations, int) or isinstance(max_operations, bool) or max_operations < 1:
        raise ValueError("max_operations must be a positive integer")
    effects = runner.log
    database_path = runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    if not database_path.exists():
        return _record_report(application, (), scanned=0, deferred=0)

    records = SQLiteStructuredRecordStore(database_path)
    operations = SQLiteExternalApplySagaStore(records)
    recoverable = operations.list_recoverable()
    selected = tuple(
        (operation, effect_id)
        for operation in recoverable
        if (effect_id := _recovery_effect_id(effects, operation)) is not None
    )[:max_operations]
    deferred = len(recoverable) - len(selected)
    store, settings = build_rebuild_object_store(runtime_root)
    items: list[ExternalApplyStartupRecoveryItem] = []

    for operation, effect_id in selected:
        try:
            if operation.evidence.namespace_id != settings.namespace_id:
                raise _RecoveryRejected("namespace_drift")
            if operation.evidence.document_authority_identity == "unbound:legacy":
                raise _RecoveryRejected("authority_unbound")
            resolution = build_rebuild_document_repository_resolution(runtime_root, store, settings)
            if resolution.authority_identity != operation.evidence.document_authority_identity:
                raise _RecoveryRejected("authority_drift")
            def apply(effect):
                domain_operation_id = _domain_operation_id(effect, fallback=operation.operation_id)
                if effect.contract_version == EFFECT_V2:
                    _validate_v2_effect(effect, operation)
                result = ExternalDocumentApplySagaService(
                    documents=resolution.repository,
                    drafts=store,
                    operations=operations,
                    namespace_id=settings.namespace_id,
                    document_authority_identity=resolution.authority_identity,
                ).apply(
                    domain_operation_id,
                    expected_revision=operation.evidence.base_revision,
                )
                receipt_ref = (
                    f"crp://{settings.namespace_id}/external-document-apply-receipts/"
                    f"{domain_operation_id}:r{result.operation_revision}"
                )
                return _receipt(receipt_ref) if effect.contract_version == EFFECT_V2 else receipt_ref

            runner.execute_planned(
                effect_id, apply, now=int(time.time()),
                receipt_kind="external-document-apply-receipt",
            )
            items.append(
                ExternalApplyStartupRecoveryItem(
                    operation_id=operation.operation_id,
                    initial_state=operation.state,
                    outcome="recovered",
                )
            )
        except Exception as exc:  # isolate one durable operation from the remaining bounded scan
            items.append(
                ExternalApplyStartupRecoveryItem(
                    operation_id=operation.operation_id,
                    initial_state=operation.state,
                    outcome="failed",
                    error_code=_stable_error_code(exc),
                )
            )

    return _record_report(
        application,
        tuple(items),
        scanned=len(recoverable),
        deferred=deferred,
    )


def _recovery_effect_id(effects: EffectLog, operation) -> str | None:
    """Resolve a legacy Effect or the v2 intent reconstructed from Saga evidence."""
    try:
        legacy = effects.get(operation.operation_id)
        return legacy.operation_id if legacy.state is EffectState.PLANNED else None
    except KeyError:
        intent = _v2_intent(operation)
        try:
            effect = effects.get(intent.operation_id)
        except KeyError:
            return None
        return effect.operation_id if effect.state is EffectState.PLANNED else None


def _domain_operation_id(effect, *, fallback: str) -> str:
    if effect.contract_version != EFFECT_V2:
        return fallback
    marker = "/external-document-apply-intents/"
    intent_ref = effect.intent_ref
    if not isinstance(intent_ref, str) or not intent_ref.startswith("crp://"):
        raise EffectHandlerAbandoned("external_document_apply.intent_invalid")
    _prefix, separator, operation_id = intent_ref.partition(marker)
    if not separator or not operation_id or "/" in operation_id:
        raise EffectHandlerAbandoned("external_document_apply.intent_invalid")
    return operation_id


def _validate_v2_effect(effect, operation) -> None:
    """Fence every v2 execution against the immutable intent contract."""
    if operation is None:
        raise EffectHandlerAbandoned("external_document_apply.operation_missing")
    expected = _v2_intent(operation)
    if (
        effect.intent_ref != expected.intent_ref
        or effect.root_id != expected.root_id
        or effect.session_id != expected.session_id
        or effect.step_key != expected.step_key
        or effect.kind != expected.kind
        or effect.effect_class != expected.effect_class
        or effect.gate_decision_id != expected.gate_decision_id
        or dict(effect.rev_set) != dict(expected.rev_set)
        or effect.intent_schema_version != expected.intent_schema_version
        or effect.expected_receipt_kind != expected.expected_receipt_kind
        or effect.expected_receipt_schema_version != expected.expected_receipt_schema_version
    ):
        raise EffectHandlerAbandoned("external_document_apply.intent_drift")


class _RecoveryRejected(RuntimeError):
    pass


def _stable_error_code(exc: Exception) -> str:
    if isinstance(exc, _RecoveryRejected):
        return str(exc)
    if isinstance(exc, ExternalDocumentApplyConflict):
        return "evidence_conflict"
    if isinstance(exc, ExternalDocumentApplyError):
        return "operation_invalid"
    if isinstance(exc, AggregateRepositoryFactoryError):
        return "authority_unavailable"
    if isinstance(exc, ExternalApplySagaError):
        return "operation_store_invalid"
    return "recovery_failed"


def _record_report(
    application: FastAPI,
    items: tuple[ExternalApplyStartupRecoveryItem, ...],
    *,
    scanned: int,
    deferred: int,
) -> ExternalApplyStartupRecoveryReport:
    report = ExternalApplyStartupRecoveryReport(
        scanned=scanned,
        attempted=len(items),
        recovered=sum(item.outcome == "recovered" for item in items),
        failed=sum(item.outcome == "failed" for item in items),
        deferred=deferred,
        items=items,
    )
    application.state.external_apply_startup_recovery = report
    return report
