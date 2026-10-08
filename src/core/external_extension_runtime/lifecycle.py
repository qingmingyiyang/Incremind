from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from core.effect_log import (
    EFFECT_V2,
    NOT_APPLICABLE,
    Effect,
    EffectClass,
    EffectIntent,
    EffectPurpose,
    EffectReceipt,
    EffectState,
)
from core.external_extensions import (
    ArtifactInventory,
    ExtensionContractError,
    ExtensionDetectionError,
    InstallIntent,
    ResolvedSource,
    SourceSpec,
    derive_review_plan,
    inspect_extension,
)

from .artifact_evidence import ArtifactEvidence
from .fact_store import (
    ExternalExtensionFactConflict,
    ExternalExtensionFactStore,
    artifact_ref,
    intent_ref,
    resolution_operation_id,
    resolution_ref,
)


RESOLVE_EFFECT_KIND = "external_extension_source_resolve"
ACQUIRE_EFFECT_KIND = "external_extension_acquire_intake"
RESOLVE_INTENT_SCHEMA = "external-extension-source-resolve/v1"
RESOLVE_RECEIPT_KIND = "external-extension-source-resolution"
RESOLVE_RECEIPT_SCHEMA = "external-extension-source-resolution/v1"
ACQUIRE_INTENT_SCHEMA = "external-extension-acquire-intake/v1"
ACQUIRE_RECEIPT_KIND = "external-extension-intake"
ACQUIRE_RECEIPT_SCHEMA = "external-extension-intake/v1"


class SourceResolver(Protocol):
    """Core-owned read-only resolver; the Handler persists its observation."""

    def resolve(
        self, source: SourceSpec, *, artifact_ref: str, operation_id: str,
    ) -> ResolvedSource: ...


class ArtifactAcquirer(Protocol):
    """Core-owned, read-only fetch port for a pinned immutable source."""

    def acquire(
        self,
        source: ResolvedSource,
        *,
        operation_id: str,
        subpath: str | None,
    ) -> ArtifactInventory: ...


def build_resolve_effect_intent(
    intent: InstallIntent,
    *,
    session_id: str,
    root_id: str,
    step_key: str,
    gate_decision_id: str,
    policy_revision: str,
    boundary_revision: str,
    handler_revision: str,
    turn_id: str | None = None,
    parent_id: str | None = None,
) -> EffectIntent:
    if intent.source_spec is None:
        raise ValueError("source resolution Effect requires an exact SourceSpec")
    return EffectIntent(
        session_id=session_id,
        root_id=root_id,
        step_key=step_key,
        kind=RESOLVE_EFFECT_KIND,
        effect_class=EffectClass.QUERYABLE,
        purpose=EffectPurpose.PRIMARY,
        intent_ref=f"crp://external-extension-install-intents/{intent.intent_id}",
        gate_decision_id=gate_decision_id,
        rev_set=_authority_revisions(policy_revision, boundary_revision, handler_revision),
        payload={
            "install_id": intent.intent_id,
            "descriptor_ref": f"crp://external-extension-install-intents/{intent.intent_id}",
            "requested_revision": intent.source_spec.requested_ref or NOT_APPLICABLE,
        },
        turn_id=turn_id,
        parent_id=parent_id,
        idem_key=f"external-extension-resolve:{intent.intent_id}",
        contract_version=EFFECT_V2,
        intent_schema_version=RESOLVE_INTENT_SCHEMA,
        expected_receipt_kind=RESOLVE_RECEIPT_KIND,
        expected_receipt_schema_version=RESOLVE_RECEIPT_SCHEMA,
    )


def build_acquire_effect_intent(
    *,
    resolution_operation_id: str,
    facts: ExternalExtensionFactStore,
    session_id: str,
    root_id: str,
    step_key: str,
    gate_decision_id: str,
    policy_revision: str,
    boundary_revision: str,
    handler_revision: str,
    turn_id: str | None = None,
    parent_id: str | None = None,
) -> EffectIntent:
    install_id, source = facts.load_resolution(resolution_ref(resolution_operation_id))
    if not source.is_immutable:
        raise ValueError("acquisition Effect requires an immutable resolved source")
    return EffectIntent(
        session_id=session_id,
        root_id=root_id,
        step_key=step_key,
        kind=ACQUIRE_EFFECT_KIND,
        effect_class=EffectClass.QUERYABLE,
        purpose=EffectPurpose.PRIMARY,
        intent_ref=resolution_ref(resolution_operation_id),
        gate_decision_id=gate_decision_id,
        rev_set=_authority_revisions(policy_revision, boundary_revision, handler_revision),
        payload={
            "install_id": install_id,
            "resolution_ref": resolution_ref(resolution_operation_id),
            "source_revision": source.immutable_revision,
            "artifact_ref": source.artifact_ref,
        },
        turn_id=turn_id,
        parent_id=parent_id,
        idem_key=f"external-extension-acquire:{install_id}:{source.immutable_revision}",
        contract_version=EFFECT_V2,
        intent_schema_version=ACQUIRE_INTENT_SCHEMA,
        expected_receipt_kind=ACQUIRE_RECEIPT_KIND,
        expected_receipt_schema_version=ACQUIRE_RECEIPT_SCHEMA,
    )


class ExternalExtensionResolveHandler:
    def __init__(
        self,
        facts: ExternalExtensionFactStore,
        resolver: SourceResolver,
        *,
        frozen_identity_validator: Callable[[Effect], None],
    ) -> None:
        self._facts = facts
        self._resolver = resolver
        self._frozen_identity_validator = frozen_identity_validator

    def __call__(self, effect: Effect) -> EffectReceipt:
        _require_effect(effect, RESOLVE_EFFECT_KIND, RESOLVE_INTENT_SCHEMA, RESOLVE_RECEIPT_KIND, RESOLVE_RECEIPT_SCHEMA)
        self._frozen_identity_validator(effect)
        intent = self._validated_intent(effect)
        existing = self._facts.resolution_receipt(effect.operation_id)
        if existing is not None:
            return EffectReceipt(existing, RESOLVE_RECEIPT_KIND, RESOLVE_RECEIPT_SCHEMA, RESOLVE_INTENT_SCHEMA)
        expected_artifact_ref = artifact_ref(intent.intent_id, effect.operation_id)
        source = self._facts.resolution_observation(
            operation_id=effect.operation_id,
            intent_reference=effect.intent_ref,
        )
        if source is None:
            source = self._resolver.resolve(
                intent.source_spec,
                artifact_ref=expected_artifact_ref,
                operation_id=effect.operation_id,
            )
            self._facts.record_resolution_observation(
                operation_id=effect.operation_id,
                intent_reference=effect.intent_ref,
                source=source,
            )
        receipt = self._facts.record_resolution(
            operation_id=effect.operation_id,
            intent_reference=effect.intent_ref,
            source=source,
        )
        return EffectReceipt(receipt, RESOLVE_RECEIPT_KIND, RESOLVE_RECEIPT_SCHEMA, RESOLVE_INTENT_SCHEMA)

    def probe(self, effect: Effect) -> tuple[EffectState, str | None]:
        _require_effect(effect, RESOLVE_EFFECT_KIND, RESOLVE_INTENT_SCHEMA, RESOLVE_RECEIPT_KIND, RESOLVE_RECEIPT_SCHEMA)
        self._frozen_identity_validator(effect)
        intent = self._validated_intent(effect)
        receipt = self._facts.resolution_receipt(effect.operation_id)
        if receipt is not None:
            return EffectState.SETTLED_OK, receipt
        source = self._facts.resolution_observation(
            operation_id=effect.operation_id,
            intent_reference=effect.intent_ref,
        )
        if source is None:
            return EffectState.PLANNED, None
        recovered = self._facts.record_resolution(
            operation_id=effect.operation_id,
            intent_reference=effect.intent_ref,
            source=source,
        )
        return EffectState.SETTLED_OK, recovered

    def _validated_intent(self, effect: Effect) -> InstallIntent:
        intent = self._facts.load_intent(effect.intent_ref)
        if intent.source_spec is None:
            raise ExternalExtensionFactConflict("resolution Effect points to a search-only intent")
        expected = build_resolve_effect_intent(
            intent,
            session_id=effect.session_id,
            root_id=effect.root_id,
            step_key=effect.step_key,
            gate_decision_id=effect.gate_decision_id,
            policy_revision=_revision(effect, "policy"),
            boundary_revision=_revision(effect, "boundary"),
            handler_revision=_revision(effect, "handler"),
            turn_id=effect.turn_id,
            parent_id=effect.parent_id,
        )
        _require_effect_identity(effect, expected)
        return intent


class ExternalExtensionAcquireHandler:
    def __init__(
        self,
        facts: ExternalExtensionFactStore,
        acquirer: ArtifactAcquirer,
        *,
        frozen_identity_validator: Callable[[Effect], None],
    ) -> None:
        self._facts = facts
        self._acquirer = acquirer
        self._frozen_identity_validator = frozen_identity_validator

    def __call__(self, effect: Effect) -> EffectReceipt:
        _require_effect(effect, ACQUIRE_EFFECT_KIND, ACQUIRE_INTENT_SCHEMA, ACQUIRE_RECEIPT_KIND, ACQUIRE_RECEIPT_SCHEMA)
        self._frozen_identity_validator(effect)
        source, subpath = self._validated_source(effect)
        existing = self._facts.intake_receipt(effect.operation_id)
        if existing is not None:
            return EffectReceipt(existing, ACQUIRE_RECEIPT_KIND, ACQUIRE_RECEIPT_SCHEMA, ACQUIRE_INTENT_SCHEMA)
        evidence = self._facts.artifact_evidence(operation_id=effect.operation_id, source=source)
        if evidence is None:
            inventory = self._acquirer.acquire(
                source,
                operation_id=effect.operation_id,
                subpath=subpath,
            )
            if not isinstance(inventory, ArtifactInventory):
                raise ExternalExtensionFactConflict("artifact acquirer returned an invalid inventory")
            evidence = self._facts.commit_artifact(
                operation_id=effect.operation_id,
                source=source,
                inventory=inventory,
            )
        receipt = self._record_intake(effect, source, evidence)
        return EffectReceipt(receipt, ACQUIRE_RECEIPT_KIND, ACQUIRE_RECEIPT_SCHEMA, ACQUIRE_INTENT_SCHEMA)

    def probe(self, effect: Effect) -> tuple[EffectState, str | None]:
        _require_effect(effect, ACQUIRE_EFFECT_KIND, ACQUIRE_INTENT_SCHEMA, ACQUIRE_RECEIPT_KIND, ACQUIRE_RECEIPT_SCHEMA)
        self._frozen_identity_validator(effect)
        source, _subpath = self._validated_source(effect)
        receipt = self._facts.intake_receipt(effect.operation_id)
        if receipt is not None:
            return EffectState.SETTLED_OK, receipt
        evidence = self._facts.artifact_evidence(operation_id=effect.operation_id, source=source)
        if evidence is None:
            return EffectState.PLANNED, None
        recovered = self._record_intake(effect, source, evidence)
        return EffectState.SETTLED_OK, recovered

    def _record_intake(
        self,
        effect: Effect,
        source: ResolvedSource,
        evidence: ArtifactEvidence,
    ) -> str:
        if not isinstance(evidence, ArtifactEvidence):
            raise ExternalExtensionFactConflict("artifact acquirer returned invalid evidence")
        try:
            result = inspect_extension(evidence.inventory, source)
            plan = derive_review_plan(result.manifest)
            # A second pure parse is the minimum deterministic health check for
            # the immutable artifact before an intake receipt is committed.
            if inspect_extension(evidence.inventory, source) != result:
                raise ExternalExtensionFactConflict("extension manifest reparse drifted")
            receipt = self._facts.record_intake(
                operation_id=effect.operation_id,
                resolution_reference=effect.intent_ref,
                manifest=result.manifest,
                review_plan=plan,
            )
        except (ExtensionDetectionError, ExtensionContractError) as error:
            receipt = self._facts.record_intake(
                operation_id=effect.operation_id,
                resolution_reference=effect.intent_ref,
                manifest=None,
                review_plan=None,
                quarantine_code=_quarantine_code(error),
            )
        return receipt

    def _validated_source(self, effect: Effect) -> tuple[ResolvedSource, str | None]:
        operation = resolution_operation_id(effect.intent_ref)
        install_id, source = self._facts.load_resolution(effect.intent_ref)
        install = self._facts.load_intent(intent_ref(install_id))
        if install.source_spec is None:
            raise ExternalExtensionFactConflict("acquisition resolution lacks an exact source intent")
        expected = build_acquire_effect_intent(
            resolution_operation_id=operation,
            facts=self._facts,
            session_id=effect.session_id,
            root_id=effect.root_id,
            step_key=effect.step_key,
            gate_decision_id=effect.gate_decision_id,
            policy_revision=_revision(effect, "policy"),
            boundary_revision=_revision(effect, "boundary"),
            handler_revision=_revision(effect, "handler"),
            turn_id=effect.turn_id,
            parent_id=effect.parent_id,
        )
        _require_effect_identity(effect, expected)
        return source, install.source_spec.subpath


def _authority_revisions(policy: str, boundary: str, handler: str) -> Mapping[str, object]:
    for value in (policy, boundary, handler):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Effect authority revisions must be non-empty")
    return {
        "policy": policy,
        "boundary": boundary,
        "capability": NOT_APPLICABLE,
        "context_manifest": NOT_APPLICABLE,
        "provider": NOT_APPLICABLE,
        "model_route": NOT_APPLICABLE,
        "bundle": NOT_APPLICABLE,
        "handler": handler,
        "secret": NOT_APPLICABLE,
        "budget": NOT_APPLICABLE,
        "workflow": NOT_APPLICABLE,
    }


def _revision(effect: Effect, name: str) -> str:
    value = effect.rev_set.get(name)
    if not isinstance(value, str) or not value:
        raise ExternalExtensionFactConflict("external extension Effect revision set drifted")
    return value


def _require_effect_identity(effect: Effect, expected: EffectIntent) -> None:
    if (
        effect.operation_id != expected.operation_id
        or effect.intent_ref != expected.intent_ref
        or effect.intent_digest != expected.intent_digest
        or effect.gate_decision_id != expected.gate_decision_id
        or dict(effect.rev_set) != dict(expected.rev_set)
        or effect.idem_key != expected.idem_key
        or effect.authority_set_id != expected.authority_set_id
        or effect.identity_algorithm != expected.identity_algorithm
        or effect.revision_schema_version != expected.revision_schema_version
    ):
        raise ExternalExtensionFactConflict("external extension Effect identity drifted from durable facts")


def _require_effect(
    effect: Effect,
    kind: str,
    intent_schema: str,
    receipt_kind: str,
    receipt_schema: str,
) -> None:
    if (
        effect.kind != kind
        or effect.effect_class is not EffectClass.QUERYABLE
        or effect.contract_version != EFFECT_V2
        or effect.intent_schema_version != intent_schema
        or effect.expected_receipt_kind != receipt_kind
        or effect.expected_receipt_schema_version != receipt_schema
    ):
        raise ExternalExtensionFactConflict("external extension Effect contract drifted")


def _quarantine_code(error: Exception) -> str:
    message = str(error).casefold()
    if "ambiguous" in message:
        return "ambiguous_format"
    if "unknown extension format" in message:
        return "unknown_format"
    return "invalid_extension_descriptor"
