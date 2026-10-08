from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import time

from fastapi import FastAPI
from core.effect_log import (
    EffectClass,
    EffectHandlerAbandoned,
    EffectHandlerRegistration,
    EffectIntent,
    EffectReceipt,
    EffectLog,
    EffectRunner,
    EffectState,
    EFFECT_V2,
    GateDecision,
    GateDecisionFact,
    NOT_APPLICABLE,
    V2_REVISION_KEYS,
)

from backend.api.external_project_skill_apply_saga import (
    ExternalProjectSkillApplyConflict,
    ExternalProjectSkillApplyError,
    ExternalProjectSkillApplySagaService,
)
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.aggregate_repository_factory import (
    STRUCTURED_DATABASE_NAME,
    AggregateRepositoryFactory,
    AggregateRepositoryFactoryError,
)
from core.storage_provider import (
    ExternalProjectSkillApplySagaError,
    SQLiteExternalProjectSkillApplySagaStore,
    SQLiteStructuredRecordStore,
)


DEFAULT_EXTERNAL_PROJECT_SKILL_APPLY_RECOVERY_LIMIT = 100
_INTENT_SCHEMA = "external-project-skill-apply-intent-v2"
_RECEIPT_KIND = "external-project-skill-apply-receipt"
_RECEIPT_SCHEMA = "external-project-skill-apply-receipt-v2"


def register_external_project_skill_apply_handler(runtime_root: Path, effect_runtime) -> None:
    database_path = runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    operations = SQLiteExternalProjectSkillApplySagaStore(
        SQLiteStructuredRecordStore(database_path)
    )

    def handle(effect) -> EffectReceipt:
        operation_id, operation = _validated_operation(effect, operations)
        store, settings = build_rebuild_object_store(runtime_root)
        evidence = operation.evidence
        if evidence.namespace_id != settings.namespace_id:
            raise EffectHandlerAbandoned("external_project_skill_apply.namespace_drift")
        resolution = AggregateRepositoryFactory(
            runtime_root=runtime_root,
            namespace_id=settings.namespace_id,
            json_store=store,
        ).project_skill_repository_resolution()
        if resolution.authority_identity != evidence.project_skill_authority_identity:
            raise EffectHandlerAbandoned("external_project_skill_apply.authority_drift")
        result = ExternalProjectSkillApplySagaService(
            skills=resolution.repository,
            drafts=store,
            operations=operations,
            namespace_id=settings.namespace_id,
            project_skill_authority_identity=resolution.authority_identity,
        ).apply(
            operation_id,
            expected_revision=evidence.base_revision,
        )
        return _receipt(settings.namespace_id, operation.operation_id, result.operation_revision)

    def probe(effect) -> tuple[EffectState, str | None]:
        try:
            operation_id, operation = _validated_operation(effect, operations)
        except EffectHandlerAbandoned:
            return EffectState.PLANNED, None
        if operation.state == "finalized" and operation.applied_skill_revision is not None:
            return EffectState.SETTLED_OK, _receipt(
                operation.evidence.namespace_id, operation_id, operation.revision,
            ).receipt_ref
        return EffectState.PLANNED, None

    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind="external_project_skill_apply",
        effect_class=EffectClass.IDEMPOTENT,
        handler=handle,
    ))
    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind="external_project_skill_apply",
        effect_class=EffectClass.QUERYABLE,
        handler=handle,
        probe=probe,
        contract_version=EFFECT_V2,
        intent_schema_version=_INTENT_SCHEMA,
        receipt_kind=_RECEIPT_KIND,
        receipt_schema_version=_RECEIPT_SCHEMA,
    ))


def plan_external_project_skill_apply(
    service: ExternalProjectSkillApplySagaService,
    effects: EffectLog,
    draft_id: str,
    *,
    expected_revision: int,
) -> str:
    operation = service.prepare(draft_id, expected_revision=expected_revision)
    evidence = operation.evidence
    gate_decision_id, gate_fact, intent = _v2_intent(operation.operation_id, evidence)
    effect, _created = effects.plan_v2(
        intent, gate_decision_id=gate_decision_id, gate_fact=gate_fact, now=int(time.time()),
    )
    return effect.operation_id


def _domain_operation_id(effect) -> str:
    if effect.contract_version != EFFECT_V2:
        return effect.operation_id
    marker = "/external-project-skill-apply-intents/"
    if not isinstance(effect.intent_ref, str) or not effect.intent_ref.startswith("crp://"):
        raise EffectHandlerAbandoned("external_project_skill_apply.intent_ref_invalid")
    _head, separator, operation_id = effect.intent_ref.partition(marker)
    if not separator or not operation_id or "/" in operation_id:
        raise EffectHandlerAbandoned("external_project_skill_apply.intent_ref_invalid")
    return operation_id


def _validated_operation(effect, operations):
    operation_id = _domain_operation_id(effect)
    try:
        operation = operations.get(operation_id)
    except ExternalProjectSkillApplySagaError as error:
        raise EffectHandlerAbandoned("external_project_skill_apply.operation_ref_invalid") from error
    if operation is None:
        raise EffectHandlerAbandoned("external_project_skill_apply.operation_missing")
    if effect.contract_version != EFFECT_V2:
        return operation_id, operation
    evidence = operation.evidence
    expected_intent_ref = (
        f"crp://{evidence.namespace_id}/external-project-skill-apply-intents/{operation_id}"
    )
    if effect.intent_ref != expected_intent_ref:
        raise EffectHandlerAbandoned("external_project_skill_apply.namespace_or_intent_drift")
    if effect.root_id != evidence.project_id:
        raise EffectHandlerAbandoned("external_project_skill_apply.project_drift")
    if effect.rev_set != _revisions(evidence):
        raise EffectHandlerAbandoned("external_project_skill_apply.revision_drift")
    return operation_id, operation


def _receipt(namespace_id: str, operation_id: str, operation_revision: int) -> EffectReceipt:
    return EffectReceipt(
        f"crp://{namespace_id}/external-project-skill-apply-receipts/{operation_id}:r{operation_revision}",
        _RECEIPT_KIND, _RECEIPT_SCHEMA, _INTENT_SCHEMA,
    )


def _review_gate(evidence) -> tuple[str, GateDecisionFact]:
    review_material = "\\0".join((
        evidence.namespace_id, evidence.project_id, evidence.project_skill_id,
        str(evidence.base_revision), evidence.payload_sha256,
        evidence.project_skill_authority_identity,
    ))
    review_digest = hashlib.sha256(review_material.encode("utf-8")).hexdigest()
    return (
        f"gate:external-project-skill-review/{review_digest}",
        GateDecisionFact(
            decision=GateDecision.ALLOW,
            rule_ref="rule:external-project-skill-user-review/v2",
            scope_ref=f"scope:project/{evidence.project_id}",
            budget_after={"review_digest": review_digest},
            secret_scope="scope:local/no-secret",
            policy_revision=evidence.payload_sha256,
        ),
    )


def _revisions(evidence) -> dict[str, str]:
    revisions = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
    revisions.update({
        "policy": evidence.payload_sha256,
        "boundary": "external-project-skill-review-boundary-v2",
        "capability": "project-skill-apply-v2",
        "context_manifest": evidence.payload_sha256,
        "provider": "local-project-skill-authority-v2",
        "bundle": evidence.project_skill_authority_identity,
        "handler": "external-project-skill-apply-handler-v2",
    })
    return revisions


def _v2_intent(operation_id: str, evidence) -> tuple[str, GateDecisionFact, EffectIntent]:
    gate_decision_id, gate_fact = _review_gate(evidence)
    return gate_decision_id, gate_fact, EffectIntent(
        session_id=f"external-project-skill-apply:{evidence.project_id}",
        root_id=evidence.project_id, step_key="apply-reviewed-project-skill",
        kind="external_project_skill_apply", effect_class=EffectClass.QUERYABLE,
        intent_ref=f"crp://{evidence.namespace_id}/external-project-skill-apply-intents/{operation_id}",
        gate_decision_id=gate_decision_id, rev_set=_revisions(evidence),
        payload={
            "review_operation_ref": f"crp://{evidence.namespace_id}/external-project-skill-apply-operations/{operation_id}",
            "review_draft_ref": f"crp://{evidence.namespace_id}/external-project-skill-apply-intents/{operation_id}",
            "payload_digest": evidence.payload_sha256,
            "project_skill_id": evidence.project_skill_id,
            "mode": "user-review-confirmation",
        },
        idem_key=f"external-project-skill:{operation_id}", contract_version=EFFECT_V2,
        intent_schema_version=_INTENT_SCHEMA, expected_receipt_kind=_RECEIPT_KIND,
        expected_receipt_schema_version=_RECEIPT_SCHEMA,
    )




@dataclass(frozen=True, slots=True)
class ExternalProjectSkillApplyStartupRecoveryItem:
    operation_id: str
    initial_state: str
    outcome: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ExternalProjectSkillApplyStartupRecoveryReport:
    scanned: int
    attempted: int
    recovered: int
    failed: int
    deferred: int
    items: tuple[ExternalProjectSkillApplyStartupRecoveryItem, ...]


def backfill_external_project_skill_apply_effects(
    runtime_root: Path, effects: EffectLog, *,
    max_operations: int = DEFAULT_EXTERNAL_PROJECT_SKILL_APPLY_RECOVERY_LIMIT,
) -> tuple[str, ...]:
    database_path = runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    if not database_path.exists():
        return ()
    operations = SQLiteExternalProjectSkillApplySagaStore(SQLiteStructuredRecordStore(database_path))
    now = int(time.time())
    backfilled: list[str] = []
    for operation in operations.list_recoverable()[:max_operations]:
        # A historical v1 Effect has its Saga id as operation id.  Keep it
        # recoverable through the legacy registration; every missing intent is
        # reconstructed as v2, including a crash after Saga preparation.
        try:
            effect = effects.get(operation.operation_id)
        except KeyError:
            gate_decision_id, gate_fact, intent = _v2_intent(
                operation.operation_id, operation.evidence,
            )
            effect, _created = effects.plan_v2(
                intent, gate_decision_id=gate_decision_id, gate_fact=gate_fact, now=now,
            )
        backfilled.append(effect.operation_id)
    return tuple(backfilled)


def dispatch_external_project_skill_apply_effects(
    application: FastAPI,
    runtime_root: Path,
    runner: EffectRunner,
    *,
    max_operations: int = DEFAULT_EXTERNAL_PROJECT_SKILL_APPLY_RECOVERY_LIMIT,
) -> ExternalProjectSkillApplyStartupRecoveryReport:
    if not isinstance(max_operations, int) or isinstance(max_operations, bool) or max_operations < 1:
        raise ValueError("max_operations must be a positive integer")
    effects = runner.log
    database_path = runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    if not database_path.exists():
        return _record_report(application, (), scanned=0, deferred=0)

    records = SQLiteStructuredRecordStore(database_path)
    operations = SQLiteExternalProjectSkillApplySagaStore(records)
    recoverable = operations.list_recoverable()
    selected = tuple(
        (operation, _recovery_effect_id(effects, operation)) for operation in recoverable
    )
    selected = tuple((operation, effect_id) for operation, effect_id in selected if effect_id)[:max_operations]
    store, settings = build_rebuild_object_store(runtime_root)
    items: list[ExternalProjectSkillApplyStartupRecoveryItem] = []

    for operation, effect_id in selected:
        try:
            if operation.evidence.namespace_id != settings.namespace_id:
                raise _RecoveryRejected("namespace_drift")

            def apply(effect):
                domain_operation_id, validated_operation = _validated_operation(effect, operations)
                evidence = validated_operation.evidence
                if evidence.namespace_id != settings.namespace_id:
                    raise EffectHandlerAbandoned("external_project_skill_apply.namespace_drift")
                resolution = AggregateRepositoryFactory(
                    runtime_root=runtime_root,
                    namespace_id=settings.namespace_id,
                    json_store=store,
                ).project_skill_repository_resolution()
                if resolution.authority_identity != evidence.project_skill_authority_identity:
                    raise EffectHandlerAbandoned("external_project_skill_apply.authority_drift")
                result = ExternalProjectSkillApplySagaService(
                    skills=resolution.repository,
                    drafts=store,
                    operations=operations,
                    namespace_id=settings.namespace_id,
                    project_skill_authority_identity=resolution.authority_identity,
                ).apply(domain_operation_id, expected_revision=evidence.base_revision)
                return _receipt(settings.namespace_id, domain_operation_id, result.operation_revision)

            runner.execute_planned(
                effect_id, apply, now=int(time.time()), receipt_kind=_RECEIPT_KIND,
            )
            items.append(ExternalProjectSkillApplyStartupRecoveryItem(
                operation.operation_id, operation.state, "recovered"
            ))
        except Exception as exc:
            items.append(ExternalProjectSkillApplyStartupRecoveryItem(
                operation.operation_id, operation.state, "failed", _stable_error_code(exc)
            ))
    return _record_report(
        application,
        tuple(items),
        scanned=len(recoverable),
        deferred=len(recoverable) - len(selected),
    )


def _recovery_effect_id(effects: EffectLog, operation) -> str | None:
    """Find the historical v1 Effect or the reconstructed v2 intent."""
    try:
        legacy = effects.get(operation.operation_id)
        return legacy.operation_id if legacy.state is EffectState.PLANNED else None
    except KeyError:
        _gate_id, _gate_fact, intent = _v2_intent(operation.operation_id, operation.evidence)
        try:
            effect = effects.get(intent.operation_id)
        except KeyError:
            return None
        return effect.operation_id if effect.state is EffectState.PLANNED else None


class _RecoveryRejected(RuntimeError):
    pass


def _stable_error_code(exc: Exception) -> str:
    if isinstance(exc, _RecoveryRejected):
        return str(exc)
    if isinstance(exc, ExternalProjectSkillApplyConflict):
        return "evidence_conflict"
    if isinstance(exc, ExternalProjectSkillApplyError):
        return "operation_invalid"
    if isinstance(exc, AggregateRepositoryFactoryError):
        return "authority_unavailable"
    if isinstance(exc, ExternalProjectSkillApplySagaError):
        return "operation_store_invalid"
    return "recovery_failed"


def _record_report(application, items, *, scanned, deferred):
    report = ExternalProjectSkillApplyStartupRecoveryReport(
        scanned=scanned,
        attempted=len(items),
        recovered=sum(item.outcome == "recovered" for item in items),
        failed=sum(item.outcome == "failed" for item in items),
        deferred=deferred,
        items=items,
    )
    application.state.external_project_skill_apply_startup_recovery = report
    return report
