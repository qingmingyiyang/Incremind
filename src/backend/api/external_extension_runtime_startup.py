"""Production composition for quarantined external-extension intake.

This module only wires domain handlers into the already-owned Core Effect
runtime.  It creates neither a Runner nor a Reaper and never performs a
network request during startup.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.effect_log import (
    CoordinationTaskRegistration,
    EFFECT_V2,
    EffectBackfillRegistration,
    Effect,
    EffectClass,
    EffectHandlerRegistration,
    EffectRecoveryCoordinator,
    EffectRuntime,
)
from core.external_extensions import InstallIntent
from core.external_extension_runtime.artifact_evidence import ImmutableQuarantineArtifactStore
from core.external_extension_runtime.fact_store import (
    ExternalExtensionFactConflict,
    ExternalExtensionFactError,
    ExternalExtensionFactStore,
    intent_ref,
    resolution_ref,
)
from core.external_extension_runtime.github_acquisition import (
    GitHubArtifactAcquirer,
    GitHubSourceResolver,
)
from core.external_extension_runtime.gate_authority import (
    ExternalExtensionGateAuthority,
    ExternalExtensionGateRequest,
)
from core.external_extension_runtime.lifecycle import (
    ACQUIRE_EFFECT_KIND,
    ACQUIRE_INTENT_SCHEMA,
    ACQUIRE_RECEIPT_KIND,
    ACQUIRE_RECEIPT_SCHEMA,
    RESOLVE_EFFECT_KIND,
    RESOLVE_INTENT_SCHEMA,
    RESOLVE_RECEIPT_KIND,
    RESOLVE_RECEIPT_SCHEMA,
    ExternalExtensionAcquireHandler,
    ExternalExtensionResolveHandler,
    build_acquire_effect_intent,
    build_resolve_effect_intent,
)
from core.external_extension_runtime.outbound_fetch import production_extension_acquisition_fetcher
from core.external_extension_runtime.installation import ExternalExtensionInstallationStore
from core.external_extension_runtime.lifecycle_commands import (
    ExternalExtensionLifecycleCommandService,
)
from core.external_extension_runtime.skill_materializer import (
    ExternalExtensionApplicationSkillMaterializer,
)
from core.external_extension_runtime.terminal_receipts import (
    ExternalExtensionTerminalReceiptStore,
    ExternalExtensionTerminalReceiptVerifier,
    register_external_extension_lifecycle_handlers,
)
from core.application_skill import ApplicationSkillPackage, ApplicationSkillSource
from core.storage_provider import SQLiteStructuredRecordStore

from backend.api.rebuild_storage_runtime import build_rebuild_object_store


_EFFECT_DATABASE_NAME = "jobs.sqlite3"
_QUARANTINE_DIRECTORY_NAME = "external-extension-quarantine"
_MATERIALIZATION_DIRECTORY_NAME = "external-extension-skills"
_INTAKE_POLICY_REVISION = "external-extension-natural-language-policy-v1"
_INTAKE_BOUNDARY_REVISION = "external-extension-natural-language-boundary-v1"
_INTAKE_HANDLER_REVISION = "external-extension-natural-language-handler-v1"
_FULL_LOWERCASE_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def _matches_frozen_effect_identity(current: Effect, requested) -> bool:
    """Match every Core-frozen identity component before accepting replay."""

    scalar_fields = (
        "operation_id", "session_id", "turn_id", "root_id", "parent_id",
        "step_key", "kind", "effect_class", "purpose", "intent_ref",
        "intent_digest", "gate_decision_id", "idem_key", "contract_version",
        "authority_set_id", "intent_schema_version", "expected_receipt_kind",
        "expected_receipt_schema_version", "identity_algorithm",
        "revision_schema_version",
    )
    return (
        all(getattr(current, field) == getattr(requested, field) for field in scalar_fields)
        and dict(current.rev_set) == dict(requested.rev_set)
    )


@dataclass(frozen=True, slots=True)
class ExternalExtensionRuntimeStartupHandles:
    """Public post-startup surface with no raw Effect/Gate/fact authority."""

    install_workflow: object
    # Immutable verified content for production consumers.  This is the
    # post-materialization read boundary; consumers must not reopen source
    # paths after this callback returns.
    active_packages: Callable[[str], tuple[ApplicationSkillPackage, ...]]
    # Compatibility/diagnostic surface only.  New production consumers use
    # active_packages so they receive immutable verified package content.
    active_sources: Callable[[str], tuple[ApplicationSkillSource, ...]]
    _register_recovery: Callable[[EffectRecoveryCoordinator], None]

    def register_recovery(self, coordinator: EffectRecoveryCoordinator) -> None:
        """Attach precomposed Core callbacks; callers cannot obtain raw ports."""

        if not isinstance(coordinator, EffectRecoveryCoordinator):
            raise TypeError("external extension lifecycle recovery requires Core recovery coordinator")
        self._register_recovery(coordinator)


@dataclass(frozen=True, slots=True)
class _ExternalExtensionIntakeExecutor:
    """Startup-private bridge from immutable approval facts into Core Effects.

    This object never leaves composition.  Its public wrapper accepts an
    opaque persisted authorization reference and re-derives the Gate grant;
    raw ``GateDecisionFact`` and ``decision_id`` injection is deliberately
    unavailable to runtime callers.
    """

    _facts: ExternalExtensionFactStore
    _runtime: EffectRuntime
    _gate_authority: ExternalExtensionGateAuthority

    @staticmethod
    def _derived(prefix: str, value: str) -> str:
        digest = hashlib.sha256(f"{prefix}\0{value}".encode("utf-8")).hexdigest()
        return f"external-extension-{prefix}-{digest[:32]}"

    def validate_frozen_intake_effect_identity(self, effect: Effect) -> None:
        """Rebuild and prove intake authority before any Handler-side work.

        This deliberately does not trust the claimed ``Effect`` row, its
        operation id, or a caller supplied revision set.  The immutable
        schema-2 command supplies the authorization reference, the Gate is
        re-derived from that evidence, and Core verifies the Effect row plus
        both immutable v2 facts as one frozen identity.
        """

        try:
            if effect.kind == RESOLVE_EFFECT_KIND:
                command = self._facts.load_v2_command_for_operation(
                    effect.operation_id, operation="resolve_install_v2",
                )
                install = self._facts.load_intent(intent_ref(str(command["intent_id"])))
                project_id = str(command["project_id"])
                authorization_ref = str(command["authorization_ref"])
                if (
                    install.project_id != project_id
                    or effect.intent_ref != intent_ref(install.intent_id)
                ):
                    raise ExternalExtensionFactConflict(
                        "resolve command immutable intent identity drifted",
                    )
                authorization = self._gate_authority.authorize(
                    ExternalExtensionGateRequest(
                        phase="source_resolve",
                        project_id=project_id,
                        subject_ref=intent_ref(install.intent_id),
                        authorization_ref=authorization_ref,
                        policy_revision=_INTAKE_POLICY_REVISION,
                    ),
                )
                if command["gate_decision_id"] != authorization.decision_id:
                    raise ExternalExtensionFactConflict(
                        "resolve command Gate identity drifted",
                    )
                expected = build_resolve_effect_intent(
                    install,
                    session_id=self._derived("session", install.intent_id),
                    root_id=project_id,
                    step_key=self._derived("resolve-step", install.intent_id),
                    gate_decision_id=authorization.decision_id,
                    policy_revision=_INTAKE_POLICY_REVISION,
                    boundary_revision=_INTAKE_BOUNDARY_REVISION,
                    handler_revision=_INTAKE_HANDLER_REVISION,
                )
            elif effect.kind == ACQUIRE_EFFECT_KIND:
                command = self._facts.load_v2_command_for_operation(
                    effect.operation_id, operation="acquire_resolved_v2",
                )
                resolution_operation_id = str(command["resolution_operation_id"])
                resolved_intent_id, _source = self._facts.load_resolution(
                    resolution_ref(resolution_operation_id),
                )
                install = self._facts.load_intent(intent_ref(resolved_intent_id))
                project_id = str(command["project_id"])
                authorization_ref = str(command["authorization_ref"])
                if (
                    command["intent_id"] != install.intent_id
                    or install.project_id != project_id
                    or effect.intent_ref != resolution_ref(resolution_operation_id)
                ):
                    raise ExternalExtensionFactConflict(
                        "acquire command immutable resolution identity drifted",
                    )
                authorization = self._gate_authority.authorize(
                    ExternalExtensionGateRequest(
                        phase="source_acquire",
                        project_id=project_id,
                        subject_ref=resolution_ref(resolution_operation_id),
                        authorization_ref=authorization_ref,
                        policy_revision=_INTAKE_POLICY_REVISION,
                    ),
                )
                if command["gate_decision_id"] != authorization.decision_id:
                    raise ExternalExtensionFactConflict(
                        "acquire command Gate identity drifted",
                    )
                expected = build_acquire_effect_intent(
                    resolution_operation_id=resolution_operation_id,
                    facts=self._facts,
                    session_id=self._derived("session", resolved_intent_id),
                    root_id=project_id,
                    step_key=self._derived("acquire-step", resolution_operation_id),
                    gate_decision_id=authorization.decision_id,
                    policy_revision=_INTAKE_POLICY_REVISION,
                    boundary_revision=_INTAKE_BOUNDARY_REVISION,
                    handler_revision=_INTAKE_HANDLER_REVISION,
                )
            else:
                raise ExternalExtensionFactConflict("unsupported external extension intake Effect kind")
            if command["operation_id"] != expected.operation_id:
                raise ExternalExtensionFactConflict(
                    "external extension command operation identity drifted",
                )
            persisted = self._runtime.log.require_v2_execution_facts(
                expected,
                gate_decision_id=authorization.decision_id,
                gate_fact=authorization.fact,
            )
            if effect != persisted:
                raise ExternalExtensionFactConflict(
                    "external extension presented Effect identity drifted",
                )
        except ExternalExtensionFactConflict:
            raise
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise ExternalExtensionFactConflict(
                "external extension intake frozen identity is invalid",
            ) from error

    def resolve_install(
        self,
        install: InstallIntent,
        *,
        authorization_ref: str,
        now: int,
    ) -> Effect:
        if (
            install.disposition != "AUTO_WITH_NOTICE"
            or install.reason_codes != ("quarantine_preview_only",)
        ):
            raise ValueError("install intent is not authorized for automatic quarantine preview")
        if install.source_spec is None or install.source_spec.kind != "github_repository":
            raise ValueError("automatic quarantine preview requires a GitHub repository source")
        if not isinstance(install.project_id, str) or not install.project_id:
            raise ValueError("automatic quarantine preview requires a project")
        project_id = install.project_id
        authorization = self._gate_authority.authorize(ExternalExtensionGateRequest(
            phase="source_resolve",
            project_id=project_id,
            subject_ref=intent_ref(install.intent_id),
            authorization_ref=authorization_ref,
            policy_revision=_INTAKE_POLICY_REVISION,
        ))
        command_id = self._derived("resolve-command", install.intent_id)
        intent = build_resolve_effect_intent(
            install,
            session_id=self._derived("session", install.intent_id),
            root_id=project_id,
            step_key=self._derived("resolve-step", install.intent_id),
            gate_decision_id=authorization.decision_id,
            policy_revision=_INTAKE_POLICY_REVISION,
            boundary_revision=_INTAKE_BOUNDARY_REVISION,
            handler_revision=_INTAKE_HANDLER_REVISION,
        )
        planned = self._facts._record_intent_and_plan_effect(
            install,
            command_id=command_id,
            command_payload={
                "schema_version": "2.0.0",
                "operation": "resolve_install_v2",
                "intent_id": install.intent_id,
                "project_id": project_id,
                "authorization_ref": authorization_ref,
                "gate_decision_id": authorization.decision_id,
                "operation_id": intent.operation_id,
            },
            effect_log=self._runtime.log,
            effect_intent=intent,
            gate_decision_id=authorization.decision_id,
            gate_fact=authorization.fact,
            now=now,
        )
        return self._runtime.dispatch_operation(planned.operation_id, now=now)

    def backfill_intent_only_resolve_effects(self, *, limit: int = 100) -> int:
        """Plan only authenticated legacy resolve commands for Core recovery.

        This is a migration source for the existing Core coordinator.  It
        never dispatches/probes/settles; Core dispatch follows in the same
        recovery pass after this bounded backfill returns.
        """

        backfilled = 0
        rejected = 0
        for record in self._facts.resolve_intent_only_backfill_batch(limit=limit):
            try:
                if record.payload.get("operation") == "acquire_resolved_v2":
                    # Acquire commands are atomic on creation.  The separate
                    # confirmed-acquire registration can reconstruct only a
                    # fixed full-SHA source confirmation or an exact,
                    # resolution-bound revision confirmation.
                    continue
                if self._backfill_resolve_record(record):
                    backfilled += 1
            except (ExternalExtensionFactError, ValueError, KeyError) as error:
                rejected += 1
                payload = record.payload
                if payload.get("operation") == "record_intent":
                    reason_code = "unsupported_legacy_provenance"
                elif isinstance(error, KeyError):
                    reason_code = "missing_authority"
                elif isinstance(error, ExternalExtensionFactConflict):
                    reason_code = "integrity_conflict"
                else:
                    reason_code = "malformed_command"
                self._facts.record_resolve_intent_only_backfill_failure(
                    record.object_id,
                    reason_code=reason_code,
                    error=error,
                )
            finally:
                self._facts.advance_resolve_intent_only_backfill_cursor(record.object_id)
        if rejected:
            raise ExternalExtensionFactConflict(
                "resolve intent-only backfill rejected durable command records"
            )
        return backfilled

    def _backfill_resolve_record(self, record) -> bool:
        payload = dict(record.payload)
        expected = {
            "schema_version", "operation", "intent_id", "project_id",
            "authorization_ref", "gate_decision_id", "operation_id",
        }
        if set(payload) != expected or payload.get("schema_version") != "2.0.0" or payload.get("operation") != "resolve_install_v2":
            raise ExternalExtensionFactError("legacy resolve command lacks atomic Gate provenance")
        intent_id = payload.get("intent_id")
        project_id = payload.get("project_id")
        authorization_ref = payload.get("authorization_ref")
        if not all(isinstance(value, str) and value for value in (intent_id, project_id, authorization_ref)):
            raise ExternalExtensionFactError("legacy resolve command is malformed")
        install = self._facts.load_intent(intent_ref(intent_id))
        if install.project_id != project_id:
            raise ExternalExtensionFactError("legacy resolve command project drifted")
        authorization = self._gate_authority.authorize(ExternalExtensionGateRequest(
            phase="source_resolve",
            project_id=project_id,
            subject_ref=intent_ref(intent_id),
            authorization_ref=authorization_ref,
            policy_revision=_INTAKE_POLICY_REVISION,
        ))
        if payload["gate_decision_id"] != authorization.decision_id:
            raise ExternalExtensionFactError("legacy resolve command Gate decision drifted")
        effect_intent = build_resolve_effect_intent(
            install,
            session_id=self._derived("session", install.intent_id),
            root_id=project_id,
            step_key=self._derived("resolve-step", install.intent_id),
            gate_decision_id=authorization.decision_id,
            policy_revision=_INTAKE_POLICY_REVISION,
            boundary_revision=_INTAKE_BOUNDARY_REVISION,
            handler_revision=_INTAKE_HANDLER_REVISION,
        )
        if payload["operation_id"] != effect_intent.operation_id:
            raise ExternalExtensionFactError("legacy resolve command Effect identity drifted")
        try:
            current = self._runtime.log.get(effect_intent.operation_id)
        except KeyError:
            current = None
        if current is not None:
            if not _matches_frozen_effect_identity(current, effect_intent):
                raise ExternalExtensionFactConflict(
                    "legacy resolve command Core Effect identity drifted"
                )
            return False
        effect = self._facts._record_intent_and_plan_effect(
            install,
            command_id=record.object_id,
            command_payload=payload,
            effect_log=self._runtime.log,
            effect_intent=effect_intent,
            gate_decision_id=authorization.decision_id,
            gate_fact=authorization.fact,
            now=0,
        )
        return effect.state.name == "PLANNED"

    def backfill_confirmed_acquire_effects(self, *, limit: int = 100) -> int:
        """Plan only acquisition Effects authorized by immutable confirmations.

        This covers the narrow crash window after an immutable resolution and
        its required confirmation were committed but before ``acquire_resolved``
        entered the atomic command/Effect transaction.  It never fetches,
        dispatches, probes, settles, or invents approval for a floating ref.
        """

        backfilled = 0
        rejected = 0
        for record in self._facts.confirmed_acquire_intent_backfill_batch(limit=limit):
            try:
                if self._backfill_confirmed_acquire_record(record):
                    backfilled += 1
            except (ExternalExtensionFactError, ValueError, KeyError) as error:
                rejected += 1
                if isinstance(error, ExternalExtensionFactConflict):
                    reason_code = "integrity_conflict"
                elif isinstance(error, KeyError):
                    reason_code = "malformed_resolution"
                else:
                    reason_code = "invalid_confirmation"
                self._facts.record_confirmed_acquire_intent_backfill_failure(
                    record.object_id,
                    reason_code=reason_code,
                    error=error,
                )
            finally:
                self._facts.advance_confirmed_acquire_intent_backfill_cursor(
                    record.object_id,
                )
        if rejected:
            raise ExternalExtensionFactConflict(
                "confirmed acquire backfill rejected durable resolution records",
            )
        return backfilled

    def _backfill_confirmed_acquire_record(self, record) -> bool:
        """Recreate one missing planned acquire Effect from safe evidence only."""

        resolution_operation_id = record.object_id
        resolved_intent_id, source = self._facts.load_resolution(
            resolution_ref(resolution_operation_id),
        )
        install = self._facts.load_intent(intent_ref(resolved_intent_id))
        if not isinstance(install.project_id, str) or not install.project_id:
            raise ExternalExtensionFactError("resolved extension source requires a project")
        if install.source_spec is None:
            raise ExternalExtensionFactError("resolved extension source requires a source spec")

        requested_ref = install.source_spec.requested_ref
        if isinstance(requested_ref, str) and _FULL_LOWERCASE_COMMIT.fullmatch(requested_ref):
            if requested_ref != source.immutable_revision:
                raise ExternalExtensionFactConflict(
                    "fixed source revision does not match durable resolution",
                )
            authorization_ref = self._facts.source_confirmation_for_intent(
                intent_ref(resolved_intent_id),
            )
        else:
            authorization_ref = self._facts.revision_confirmation_for_resolution(
                resolution_ref(resolution_operation_id),
            )
            # This is intentionally a pending state rather than a terminal
            # rejection: the operator may create the revision confirmation in
            # a later turn and the bounded cursor will revisit the resolution.
            if authorization_ref is None:
                return False

        authorization = self._gate_authority.authorize(ExternalExtensionGateRequest(
            phase="source_acquire",
            project_id=install.project_id,
            subject_ref=resolution_ref(resolution_operation_id),
            authorization_ref=authorization_ref,
            policy_revision=_INTAKE_POLICY_REVISION,
        ))
        effect_intent = build_acquire_effect_intent(
            resolution_operation_id=resolution_operation_id,
            facts=self._facts,
            session_id=self._derived("session", resolved_intent_id),
            root_id=install.project_id,
            step_key=self._derived("acquire-step", resolution_operation_id),
            gate_decision_id=authorization.decision_id,
            policy_revision=_INTAKE_POLICY_REVISION,
            boundary_revision=_INTAKE_BOUNDARY_REVISION,
            handler_revision=_INTAKE_HANDLER_REVISION,
        )
        try:
            current = self._runtime.log.get(effect_intent.operation_id)
        except KeyError:
            current = None
        if current is not None:
            if not _matches_frozen_effect_identity(current, effect_intent):
                raise ExternalExtensionFactConflict(
                    "confirmed acquire Core Effect identity drifted",
                )
            return False

        effect = self._facts._record_intent_and_plan_effect(
            install,
            command_id=self._derived("acquire-command", resolution_operation_id),
            command_payload={
                "schema_version": "2.0.0",
                "operation": "acquire_resolved_v2",
                "intent_id": install.intent_id,
                "project_id": install.project_id,
                "authorization_ref": authorization_ref,
                "gate_decision_id": authorization.decision_id,
                "operation_id": effect_intent.operation_id,
                "resolution_operation_id": resolution_operation_id,
            },
            effect_log=self._runtime.log,
            effect_intent=effect_intent,
            gate_decision_id=authorization.decision_id,
            gate_fact=authorization.fact,
            now=0,
        )
        return effect.state.name == "PLANNED"

    def acquire_resolved(
        self,
        *,
        resolution_operation_id: str,
        authorization_ref: str,
        now: int,
    ) -> Effect:
        resolved_intent_id, _source = self._facts.load_resolution(
            resolution_ref(resolution_operation_id),
        )
        install = self._facts.load_intent(intent_ref(resolved_intent_id))
        if not isinstance(install.project_id, str) or not install.project_id:
            raise ValueError("resolved extension source requires a project")
        project_id = install.project_id
        authorization = self._gate_authority.authorize(ExternalExtensionGateRequest(
            phase="source_acquire",
            project_id=project_id,
            subject_ref=resolution_ref(resolution_operation_id),
            authorization_ref=authorization_ref,
            policy_revision=_INTAKE_POLICY_REVISION,
        ))
        intent = build_acquire_effect_intent(
            resolution_operation_id=resolution_operation_id,
            facts=self._facts,
            session_id=self._derived("session", resolved_intent_id),
            root_id=project_id,
            step_key=self._derived("acquire-step", resolution_operation_id),
            gate_decision_id=authorization.decision_id,
            policy_revision=_INTAKE_POLICY_REVISION,
            boundary_revision=_INTAKE_BOUNDARY_REVISION,
            handler_revision=_INTAKE_HANDLER_REVISION,
        )
        planned = self._facts._record_intent_and_plan_effect(
            install,
            command_id=self._derived("acquire-command", resolution_operation_id),
            command_payload={
                "schema_version": "2.0.0",
                "operation": "acquire_resolved_v2",
                "intent_id": install.intent_id,
                "project_id": project_id,
                "authorization_ref": authorization_ref,
                "gate_decision_id": authorization.decision_id,
                "operation_id": intent.operation_id,
                "resolution_operation_id": resolution_operation_id,
            },
            effect_log=self._runtime.log,
            effect_intent=intent,
            gate_decision_id=authorization.decision_id,
            gate_fact=authorization.fact,
            now=now,
        )
        return self._runtime.dispatch_operation(planned.operation_id, now=now)


def register_external_extension_recovery(
    coordinator: EffectRecoveryCoordinator,
    handles: ExternalExtensionRuntimeStartupHandles,
    *,
    limit: int = 100,
) -> None:
    """Attach legacy intent backfill and projection cleanup to Core recovery.

    Backfill runs before the Core Reaper and may only atomically create a
    missing Effect from an already durable legacy command.  The coordination
    callback runs after Core dispatch and projects only terminal Effects.
    Neither callback owns a Handler or a recovery scheduler.
    """

    if not isinstance(coordinator, EffectRecoveryCoordinator):
        raise TypeError("external extension lifecycle recovery requires Core recovery coordinator")
    if not isinstance(handles, ExternalExtensionRuntimeStartupHandles):
        raise TypeError("external extension recovery requires startup handles")
    handles.register_recovery(coordinator)


def _register_external_extension_recovery_callbacks(
    coordinator: EffectRecoveryCoordinator,
    *,
    intake: _ExternalExtensionIntakeExecutor,
    lifecycle_commands: ExternalExtensionLifecycleCommandService,
    limit: int = 100,
) -> None:
    coordinator.register_backfill(EffectBackfillRegistration(
        kind="external-extension-confirmed-acquire-intents",
        run=lambda: intake.backfill_confirmed_acquire_effects(limit=limit),
    ))
    coordinator.register_backfill(EffectBackfillRegistration(
        kind="external-extension-resolve-intents",
        run=lambda: intake.backfill_intent_only_resolve_effects(limit=limit),
    ))
    coordinator.register_backfill(EffectBackfillRegistration(
        kind="external-extension-lifecycle-intents",
        run=lambda: lifecycle_commands.backfill_intent_only_effects(limit=limit),
    ))
    coordinator.register_coordination(CoordinationTaskRegistration(
        kind="external-extension-terminal-projections",
        run=lambda: lifecycle_commands.reconcile_terminal_projections(limit=limit),
    ))


def register_external_extension_runtime(
    runtime_root: Path,
    effect_runtime: EffectRuntime,
) -> ExternalExtensionRuntimeStartupHandles:
    """Register the two quarantined-intake handlers on an existing Runtime.

    The structured facts and Core Effect log deliberately share jobs.sqlite3;
    quarantine bytes live in a separate non-executable directory beneath the
    same runtime root.  The governed fetcher is constructed only as a port for
    future Effect execution, so startup itself makes no outbound request.
    """

    if not isinstance(effect_runtime, EffectRuntime):
        raise TypeError("effect_runtime must be an EffectRuntime")
    root = Path(runtime_root).resolve(strict=False)
    data_root = root / ".rebuild-data"
    records = SQLiteStructuredRecordStore(data_root / _EFFECT_DATABASE_NAME)
    facts = ExternalExtensionFactStore(
        records,
        ImmutableQuarantineArtifactStore(data_root / _QUARANTINE_DIRECTORY_NAME),
    )
    fetcher = production_extension_acquisition_fetcher()
    gate_authority = ExternalExtensionGateAuthority(facts)
    intake = _ExternalExtensionIntakeExecutor(facts, effect_runtime, gate_authority)
    resolve_handler = ExternalExtensionResolveHandler(
        facts,
        GitHubSourceResolver(fetcher),
        frozen_identity_validator=intake.validate_frozen_intake_effect_identity,
    )
    acquire_handler = ExternalExtensionAcquireHandler(
        facts,
        GitHubArtifactAcquirer(fetcher),
        frozen_identity_validator=intake.validate_frozen_intake_effect_identity,
    )

    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind=RESOLVE_EFFECT_KIND,
        effect_class=EffectClass.QUERYABLE,
        handler=resolve_handler,
        probe=resolve_handler.probe,
        contract_version=EFFECT_V2,
        intent_schema_version=RESOLVE_INTENT_SCHEMA,
        receipt_kind=RESOLVE_RECEIPT_KIND,
        receipt_schema_version=RESOLVE_RECEIPT_SCHEMA,
    ))
    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind=ACQUIRE_EFFECT_KIND,
        effect_class=EffectClass.QUERYABLE,
        handler=acquire_handler,
        probe=acquire_handler.probe,
        contract_version=EFFECT_V2,
        intent_schema_version=ACQUIRE_INTENT_SCHEMA,
        receipt_kind=ACQUIRE_RECEIPT_KIND,
        receipt_schema_version=ACQUIRE_RECEIPT_SCHEMA,
    ))
    terminal_receipts = ExternalExtensionTerminalReceiptStore(records)
    installations = ExternalExtensionInstallationStore(
        records,
        facts,
        ExternalExtensionTerminalReceiptVerifier(effect_runtime.log, terminal_receipts),
    )
    lifecycle_commands = ExternalExtensionLifecycleCommandService(
        records,
        installations,
        terminal_receipts,
        effect_runtime,
        gate_authority=gate_authority,
    )
    binding_store = build_rebuild_object_store(root)[0]
    materializer = ExternalExtensionApplicationSkillMaterializer(
        facts,
        installations,
        data_root / _MATERIALIZATION_DIRECTORY_NAME,
        binding_store=binding_store,
    )
    register_external_extension_lifecycle_handlers(
        effect_runtime.handlers,
        terminal_receipts,
        materializer,
        frozen_identity_validator=lifecycle_commands.validate_frozen_effect_identity,
    )
    # Import lazily to avoid a startup/workflow import cycle.  The resulting
    # object is the sole mutation surface published to API state.
    from backend.api.external_extension_install_workflow import (
        build_external_extension_install_workflow,
    )
    from backend.api.external_extension_mcp_import import (
        ExternalExtensionMCPImportReviewService,
    )

    mcp_import_review = ExternalExtensionMCPImportReviewService(
        root,
        installations.mcp_import_review_context,
    )
    workflow = build_external_extension_install_workflow(
        facts=facts,
        resolve_install=intake.resolve_install,
        acquire_resolved=intake.acquire_resolved,
        installations=installations,
        lifecycle_commands=lifecycle_commands,
        preview_mcp_import=mcp_import_review.preview_disabled_import,
    )
    handles = ExternalExtensionRuntimeStartupHandles(
        install_workflow=workflow,
        active_packages=materializer.active_packages,
        active_sources=materializer.active_sources,
        _register_recovery=lambda coordinator: _register_external_extension_recovery_callbacks(
            coordinator,
            intake=intake,
            lifecycle_commands=lifecycle_commands,
        ),
    )
    return handles
