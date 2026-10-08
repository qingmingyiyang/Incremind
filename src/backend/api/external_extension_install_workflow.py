"""Narrow natural-language orchestration for external extension installs.

This module is intentionally a caller-facing facade, not another execution
runtime.  It accepts user language and review confirmations, derives every
Effect identity/Gate internally, and delegates durable work to the existing
quarantine and lifecycle authorities.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from blake3 import blake3

from core.effect_log import Effect, EffectState
from core.external_extension_runtime.fact_store import ExternalExtensionFactStore, intent_ref
from core.external_extension_runtime.installation import (
    ExternalExtensionInstallationStore,
    InstallationRevisionSummary,
    InstallationSnapshot,
)
from core.external_extension_runtime.lifecycle_commands import (
    ExternalExtensionLifecycleCommandService,
)
from core.external_extensions import InstallIntentError, parse_install_intent

if TYPE_CHECKING:
    from backend.api.external_extension_mcp_import import MCPDisabledImportPreview


_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class ExternalExtensionInstallWorkflowError(ValueError):
    """Raised for a malformed caller request or an impossible workflow state."""


@dataclass(frozen=True, slots=True)
class ExternalExtensionEffectProjection:
    """Serialization-safe proof that Core executed (or retained) an Effect."""

    operation_id: str
    state: EffectState
    receipt_ref: str | None


@dataclass(frozen=True, slots=True)
class ExternalExtensionInstallPreview:
    """Safe view of a natural-language install proposal.

    Artifact bytes, local paths and secret-bearing source details are never
    copied into this API surface.  ``preview_id`` is the immutable intake
    receipt when quarantine completed, so it remains usable after restart.
    """

    preview_id: str
    intake_ref: str | None
    status: str
    risks: tuple[str, ...]
    confirmation_ids: tuple[str, ...]
    extension_id: str | None
    effects: tuple[ExternalExtensionEffectProjection, ...] = ()


@dataclass(frozen=True, slots=True)
class ExternalExtensionInstallConfirmation:
    """Safe result of review confirmation and deterministic lifecycle work."""

    preview_id: str
    intake_ref: str | None
    status: str
    snapshot: InstallationSnapshot | None
    effects: tuple[ExternalExtensionEffectProjection, ...]
    pending_action: str | None = None
    resolved_revision: str | None = None


@dataclass(frozen=True, slots=True)
class _ExternalExtensionWorkflowPort:
    """Private, high-level operation port; never published through app state."""

    _record_proposal: Callable[..., object]
    _load_proposal: Callable[..., object]
    _load_intent: Callable[..., object]
    _confirm_source_proposal: Callable[..., object]
    _source_confirmation_for_intent: Callable[..., object]
    _resolve: Callable[..., Effect]
    _resolution_reference: Callable[..., object]
    _load_resolution: Callable[..., object]
    _confirm_resolved_revision: Callable[..., object]
    _acquire: Callable[..., Effect]
    _intake_receipt: Callable[..., object]
    _load_intake: Callable[..., object]
    _confirm_review: Callable[..., object]
    _install_disabled: Callable[..., object]
    _execute_lifecycle: Callable[..., object]
    _load_installation: Callable[..., object]
    _revision_history: Callable[..., object]
    _load_revision: Callable[..., object]
    _confirm_lifecycle_action: Callable[..., object]
    _preview_mcp_import: Callable[..., object]

    def __getattr__(self, name: str):
        # Deliberately no generic raw-object forwarding: each workflow action
        # must be named and auditable at this boundary.
        raise AttributeError(name)

    def record_proposal(self, *args, **kwargs): return self._record_proposal(*args, **kwargs)
    def load_proposal(self, *args, **kwargs): return self._load_proposal(*args, **kwargs)
    def load_intent(self, *args, **kwargs): return self._load_intent(*args, **kwargs)
    def confirm_source_proposal(self, *args, **kwargs): return self._confirm_source_proposal(*args, **kwargs)
    def source_confirmation_for_intent(self, *args, **kwargs): return self._source_confirmation_for_intent(*args, **kwargs)
    def resolve_install(self, *args, **kwargs): return self._resolve(*args, **kwargs)
    def resolution_reference(self, *args, **kwargs): return self._resolution_reference(*args, **kwargs)
    def load_resolution(self, *args, **kwargs): return self._load_resolution(*args, **kwargs)
    def confirm_resolved_revision(self, *args, **kwargs): return self._confirm_resolved_revision(*args, **kwargs)
    def acquire_resolved(self, *args, **kwargs): return self._acquire(*args, **kwargs)
    def intake_receipt(self, *args, **kwargs): return self._intake_receipt(*args, **kwargs)
    def load_intake(self, *args, **kwargs): return self._load_intake(*args, **kwargs)
    def confirm_review(self, *args, **kwargs): return self._confirm_review(*args, **kwargs)
    def install_disabled(self, *args, **kwargs): return self._install_disabled(*args, **kwargs)
    def execute_lifecycle(self, *args, **kwargs): return self._execute_lifecycle(*args, **kwargs)
    def load_installation(self, *args, **kwargs): return self._load_installation(*args, **kwargs)
    def revision_history(self, *args, **kwargs): return self._revision_history(*args, **kwargs)
    def load_revision(self, *args, **kwargs): return self._load_revision(*args, **kwargs)
    def confirm_lifecycle_action(self, *args, **kwargs): return self._confirm_lifecycle_action(*args, **kwargs)
    def preview_mcp_import(self, *args, **kwargs): return self._preview_mcp_import(*args, **kwargs)


class ExternalExtensionInstallWorkflow:
    """Server-owned natural language entry point for quarantined extensions."""

    def __init__(
        self,
        operations: _ExternalExtensionWorkflowPort,
        *,
        clock: Callable[[], int | float] | None = None,
    ) -> None:
        if not isinstance(operations, _ExternalExtensionWorkflowPort):
            raise TypeError("external extension workflow requires private operation port")
        self._operations = operations
        if clock is not None and not callable(clock):
            raise TypeError("external extension workflow clock must be callable")
        self._clock = clock or time.time

    def installation_status(
        self, extension_id: str, *, project_id: str,
    ) -> tuple[InstallationSnapshot, tuple[InstallationRevisionSummary, ...]]:
        """Read one project-scoped installation and its verified history."""

        snapshot = self._operations.load_installation(
            extension_id, root_id=project_id,
        )
        history = self._operations.revision_history(
            extension_id, root_id=project_id,
        )
        return snapshot, history

    def preview_mcp_import(
        self,
        *,
        intake_ref: str,
        project_id: str,
        reviewed_candidate: object,
        confirmations: Iterable[str],
        actor: str = "local-user",
        reason: str = "Reviewed the disabled MCP server candidate.",
    ) -> "MCPDisabledImportPreview":
        """Bind one immutable MCP intake review to a disabled migration preview."""

        return self._operations.preview_mcp_import(
            intake_ref=intake_ref,
            project_id=project_id,
            reviewed_candidate=reviewed_candidate,
            confirmations=tuple(confirmations),
            actor=actor,
            reason=reason,
        )

    def execute_lifecycle_action(
        self,
        extension_id: str,
        *,
        project_id: str,
        action: str,
        expected_state_revision: int,
        actor: str,
        reason: str,
        target_revision_ref: str | None = None,
    ):
        """Execute an exact user lifecycle intent through Core Effect runtime."""

        if action not in {"disable", "rollback", "uninstall"}:
            raise ExternalExtensionInstallWorkflowError("lifecycle action is invalid")
        snapshot = self._operations.load_installation(
            extension_id, root_id=project_id,
        )
        if snapshot.state_revision != expected_state_revision:
            raise ExternalExtensionInstallWorkflowError(
                "installation state revision drifted"
            )
        if action in {"disable", "uninstall"}:
            if target_revision_ref is not None:
                raise ExternalExtensionInstallWorkflowError(
                    f"{action} does not accept a target revision"
                )
            revision_ref = snapshot.active_revision_ref
            if revision_ref is None:
                raise ExternalExtensionInstallWorkflowError(
                    f"{action} requires an active revision"
                )
        else:
            if not isinstance(target_revision_ref, str) or not target_revision_ref:
                raise ExternalExtensionInstallWorkflowError(
                    "rollback requires an exact target revision"
                )
            revision_ref = target_revision_ref
        revision = self._operations.load_revision(revision_ref)
        if revision.root_id != project_id or revision.extension_id != extension_id:
            raise ExternalExtensionInstallWorkflowError(
                "lifecycle target does not belong to the requested project"
            )
        semantic = {
            "project_id": project_id,
            "extension_id": extension_id,
            "action": action,
            "revision_ref": revision_ref,
            "subject_ref": revision.intake_ref,
            "expected_state_revision": expected_state_revision,
            "actor": actor,
            "reason": reason,
        }
        confirmation_ref = self._operations.confirm_lifecycle_action(
            confirmation_id=_derived("lifecycle-confirmation", semantic),
            project_id=project_id,
            action=action,
            revision_ref=revision_ref,
            subject_ref=revision.intake_ref,
            expected_state_revision=expected_state_revision,
            actor=actor,
            reason=reason,
        )
        return self._operations.execute_lifecycle(
            revision_ref,
            action,
            expected_state_revision,
            authorization_ref=confirmation_ref,
        )

    def upgrade_from_intake(
        self,
        extension_id: str,
        *,
        intake_ref: str,
        project_id: str,
        confirmations: Iterable[str],
        expected_state_revision: int,
        actor: str,
        reason: str,
    ) -> ExternalExtensionInstallConfirmation:
        """Promote one reviewed intake as a CAS-bound upgrade, not a new runtime."""

        current = self._operations.load_installation(
            extension_id, root_id=project_id,
        )
        if current.active_revision_ref is None:
            raise ExternalExtensionInstallWorkflowError(
                "upgrade requires an active installation"
            )
        if current.state_revision != expected_state_revision:
            raise ExternalExtensionInstallWorkflowError(
                "installation state revision drifted"
            )
        preview = self._preview_from_intake(intake_ref, ())
        if preview.extension_id != extension_id:
            raise ExternalExtensionInstallWorkflowError(
                "upgrade intake belongs to another extension"
            )
        outcome = self.confirm(
            intake_ref=intake_ref,
            project_id=project_id,
            confirmations=confirmations,
            expected_state_revision=expected_state_revision,
            actor=actor,
            reason=reason,
        )
        if outcome.snapshot is not None and outcome.status == "active":
            if (
                outcome.snapshot.active_revision_ref == current.active_revision_ref
                or outcome.snapshot.active_revision is None
                or current.active_revision is None
                or outcome.snapshot.active_revision <= current.active_revision
            ):
                raise ExternalExtensionInstallWorkflowError(
                    "upgrade did not activate a newer installation revision"
                )
        return outcome

    def preview(
        self,
        prompt: str,
        project_id: str,
        requested_ref: str | None = None,
        subpath: str | None = None,
    ) -> ExternalExtensionInstallPreview:
        """Parse and persist an exact GitHub proposal without network I/O."""

        semantic = {
            "prompt": prompt,
            "project_id": project_id,
            "requested_ref": requested_ref,
            "subpath": subpath,
        }
        proposal_id = _derived("proposal", semantic)
        try:
            install = parse_install_intent(
                prompt,
                intent_id=_derived("intent", semantic),
                project_id=project_id,
                requested_ref=requested_ref,
                subpath=subpath,
            )
        except InstallIntentError:
            return ExternalExtensionInstallPreview(
                preview_id=proposal_id,
                intake_ref=None,
                status="rejected",
                risks=("unsafe_or_unsupported_source",),
                confirmation_ids=(),
                extension_id=None,
            )
        if install.disposition != "AUTO_WITH_NOTICE" or install.source_spec is None:
            return ExternalExtensionInstallPreview(
                preview_id=proposal_id,
                intake_ref=None,
                status="requires_exact_source",
                risks=install.reason_codes,
                confirmation_ids=(),
                extension_id=None,
            )
        if install.source_spec.kind != "github_repository":
            return ExternalExtensionInstallPreview(
                preview_id=proposal_id,
                intake_ref=None,
                status="requires_specialized_adapter",
                risks=("specialized_source_adapter_required",),
                confirmation_ids=(),
                extension_id=None,
            )

        proposal_ref = self._operations.record_proposal(
            install, proposal_id=proposal_id,
        )
        return ExternalExtensionInstallPreview(
            preview_id=proposal_ref,
            intake_ref=None,
            status="source_review_required",
            risks=("initial_network_source",),
            confirmation_ids=("approve_initial_network_source",),
            extension_id=None,
        )

    def _confirm_source_proposal(
        self,
        reference: str,
        *,
        project_id: str,
        confirmations: tuple[str, ...],
        actor: str,
        reason: str,
    ) -> ExternalExtensionInstallConfirmation:
        proposal = self._operations.load_proposal(reference)
        if proposal["project_id"] != project_id:
            raise ExternalExtensionInstallWorkflowError("preview does not belong to the requested project")
        initial_confirmation = ("approve_initial_network_source",)
        revision_confirmation = ("approve_resolved_revision_download",)
        if confirmations not in {initial_confirmation, revision_confirmation}:
            raise ExternalExtensionInstallWorkflowError("confirmation ids do not exactly match the source proposal")
        intent = self._operations.load_intent(str(proposal["intent_ref"]))
        source = intent.source_spec
        if source is None or source.kind != "github_repository":
            raise ExternalExtensionInstallWorkflowError("source proposal is not a supported GitHub repository")
        if confirmations == initial_confirmation:
            source_confirmation_ref = self._operations.confirm_source_proposal(
                reference,
                confirmation_id=_derived(
                    "source-review",
                    {"proposal_ref": reference, "confirmation_ids": confirmations},
                ),
                confirmation_ids=confirmations,
                actor=actor,
                reason=reason,
            )
        else:
            source_confirmation_ref = self._operations.source_confirmation_for_intent(
                intent_ref(intent.intent_id)
            )
        resolved = self._operations.resolve_install(
            intent,
            authorization_ref=source_confirmation_ref,
            now=int(self._clock()),
        )
        if resolved.state is not EffectState.SETTLED_OK:
            return ExternalExtensionInstallConfirmation(
                reference,
                None,
                _incomplete_status((resolved,)),
                None,
                _project_effects((resolved,)),
                "source_resolve",
            )
        resolution_ref = self._operations.resolution_reference(resolved.operation_id)
        if resolution_ref is None:
            raise ExternalExtensionInstallWorkflowError("settled source resolution lacks immutable evidence")
        _intent_id, resolved_source = self._operations.load_resolution(resolution_ref)
        if not isinstance(resolved_source.immutable_revision, str):
            raise ExternalExtensionInstallWorkflowError("settled source resolution is not immutable")
        is_fixed_commit = bool(_COMMIT.fullmatch(source.requested_ref or ""))
        if not is_fixed_commit and confirmations == initial_confirmation:
            return ExternalExtensionInstallConfirmation(
                preview_id=reference,
                intake_ref=None,
                status="pinned_revision_confirmation_required",
                snapshot=None,
                effects=_project_effects((resolved,)),
                pending_action="approve_resolved_revision_download",
                resolved_revision=resolved_source.immutable_revision,
            )
        if is_fixed_commit and confirmations == revision_confirmation:
            raise ExternalExtensionInstallWorkflowError(
                "fixed source revision does not require another download confirmation"
            )
        acquisition_confirmation_ref = source_confirmation_ref
        if not is_fixed_commit:
            acquisition_confirmation_ref = self._operations.confirm_resolved_revision(
                reference,
                resolution_ref,
                confirmation_id=_derived(
                    "revision-review",
                    {
                        "proposal_ref": reference,
                        "resolution_ref": resolution_ref,
                        "confirmation_ids": confirmations,
                    },
                ),
                confirmation_ids=confirmations,
                actor=actor,
                reason=reason,
            )
        acquired = self._operations.acquire_resolved(
            resolution_operation_id=resolved.operation_id,
            authorization_ref=acquisition_confirmation_ref,
            now=int(self._clock()),
        )
        effects = (resolved, acquired)
        if acquired.state is not EffectState.SETTLED_OK:
            return ExternalExtensionInstallConfirmation(
                reference,
                None,
                _incomplete_status(effects),
                None,
                _project_effects(effects),
                "source_acquire",
                resolved_source.immutable_revision,
            )
        intake_ref_value = self._operations.intake_receipt(acquired.operation_id)
        if intake_ref_value is None:
            raise ExternalExtensionInstallWorkflowError("settled intake Effect has no immutable intake receipt")
        preview = self._preview_from_intake(intake_ref_value, effects)
        if preview.status == "quarantined" or preview.confirmation_ids:
            return ExternalExtensionInstallConfirmation(
                preview_id=reference,
                intake_ref=intake_ref_value,
                status=preview.status,
                snapshot=None,
                effects=preview.effects,
                pending_action=(
                    "post_acquisition_review" if preview.confirmation_ids else None
                ),
                resolved_revision=resolved_source.immutable_revision,
            )
        automatic = self.confirm(
            intake_ref=intake_ref_value,
            project_id=project_id,
            confirmations=(),
            expected_state_revision=0,
            actor=actor,
            reason=reason,
        )
        return ExternalExtensionInstallConfirmation(
            preview_id=reference,
            intake_ref=intake_ref_value,
            status=automatic.status,
            snapshot=automatic.snapshot,
            effects=(*preview.effects, *automatic.effects),
            pending_action=automatic.pending_action,
            resolved_revision=resolved_source.immutable_revision,
        )

    def confirm(
        self,
        preview_id: str | None = None,
        *,
        intake_ref: str | None = None,
        project_id: str,
        confirmations: Iterable[str],
        expected_state_revision: int = 0,
        actor: str = "local-user",
        reason: str = "Reviewed the quarantined external extension.",
    ) -> ExternalExtensionInstallConfirmation:
        """Confirm an immutable intake and run health then activation when safe."""

        reference = _single_reference(preview_id, intake_ref)
        if reference.startswith("crp://external-extension-install-proposals/"):
            return self._confirm_source_proposal(
                reference,
                project_id=project_id,
                confirmations=_string_tuple(tuple(confirmations), "confirmation ids"),
                actor=actor,
                reason=reason,
            )
        intake = self._operations.load_intake(reference)
        install = self._operations.load_intent(intent_ref(_required_text(intake, "intent_id")))
        if install.project_id != project_id:
            raise ExternalExtensionInstallWorkflowError("preview does not belong to the requested project")
        source_authorization_ref = self._operations.source_confirmation_for_intent(
            intent_ref(install.intent_id)
        )
        result = intake.get("result")
        if not isinstance(result, Mapping) or result.get("projection") == "quarantined":
            raise ExternalExtensionInstallWorkflowError("quarantined intake cannot be confirmed for installation")
        review = result.get("review_plan")
        if not isinstance(review, Mapping):
            raise ExternalExtensionInstallWorkflowError("immutable intake review plan is invalid")
        required_confirmations = _string_tuple(review.get("confirmation_ids"), "review confirmation ids")
        supplied_confirmations = _string_tuple(tuple(confirmations), "confirmation ids")
        if supplied_confirmations != required_confirmations:
            raise ExternalExtensionInstallWorkflowError("confirmation ids do not exactly match the immutable review plan")

        if (
            not isinstance(expected_state_revision, int)
            or isinstance(expected_state_revision, bool)
            or expected_state_revision < 0
        ):
            raise ExternalExtensionInstallWorkflowError(
                "expected installation state revision is invalid"
            )
        semantic = {
            "intake_ref": reference,
            "project_id": project_id,
            "confirmation_ids": supplied_confirmations,
            "expected_state_revision": expected_state_revision,
            "actor": actor,
            "reason": reason,
        }
        confirmation_ref = None
        if required_confirmations:
            confirmation_ref = self._operations.confirm_review(
                reference,
                confirmation_id=_derived("review", semantic),
                confirmation_ids=supplied_confirmations,
                actor=actor,
                reason=reason,
            )
        installed = self._operations.install_disabled(
            reference,
            command_id=_derived("install-disabled", semantic),
            expected_state_revision=expected_state_revision,
            review_confirmation_ref=confirmation_ref,
        )
        revision_ref = installed.candidate_revision_ref
        if revision_ref is None:
            raise ExternalExtensionInstallWorkflowError("installed candidate revision is missing")
        health = self._operations.execute_lifecycle(
            revision_ref, "health", installed.state_revision,
            authorization_ref=source_authorization_ref,
        )
        effects: list[Effect] = [health.effect]
        if health.effect.state is not EffectState.SETTLED_OK or health.snapshot is None:
            return self._incomplete_confirmation(reference, health.snapshot, effects, "health")
        activation = self._operations.execute_lifecycle(
            revision_ref, "activation", health.snapshot.state_revision,
            authorization_ref=source_authorization_ref,
        )
        effects.append(activation.effect)
        if activation.effect.state is not EffectState.SETTLED_OK or activation.snapshot is None:
            return self._incomplete_confirmation(reference, activation.snapshot, effects, "activation")
        if activation.snapshot.active_revision_ref != revision_ref:
            return self._incomplete_confirmation(reference, activation.snapshot, effects, "activation")
        return ExternalExtensionInstallConfirmation(
            preview_id=reference,
            intake_ref=reference,
            status="active",
            snapshot=activation.snapshot,
            effects=_project_effects(effects),
        )

    def _preview_from_intake(
        self, reference: str, effects: tuple[Effect, ...],
    ) -> ExternalExtensionInstallPreview:
        intake = self._operations.load_intake(reference)
        result = intake.get("result")
        if not isinstance(result, Mapping):
            raise ExternalExtensionInstallWorkflowError("immutable intake result is invalid")
        if result.get("projection") == "quarantined":
            return ExternalExtensionInstallPreview(reference, reference, "quarantined", ("quarantined",), (), None, _project_effects(effects))
        manifest = result.get("manifest")
        review = result.get("review_plan")
        if not isinstance(manifest, Mapping) or not isinstance(review, Mapping):
            raise ExternalExtensionInstallWorkflowError("immutable intake review facts are invalid")
        return ExternalExtensionInstallPreview(
            preview_id=reference,
            intake_ref=reference,
            status="review_required" if review.get("confirmation_ids") else "review_ready",
            risks=_string_tuple(review.get("risk_codes", ()), "review risks"),
            confirmation_ids=_string_tuple(review.get("confirmation_ids", ()), "review confirmation ids"),
            extension_id=_required_text(manifest, "extension_id"),
            effects=_project_effects(effects),
        )

    @staticmethod
    def _incomplete_preview(preview_id: str, *effects: Effect) -> ExternalExtensionInstallPreview:
        status = _incomplete_status(effects)
        return ExternalExtensionInstallPreview(preview_id, None, status, ("quarantine_not_settled",), (), None, _project_effects(effects))

    @staticmethod
    def _incomplete_confirmation(reference, snapshot, effects, action):
        status = _incomplete_status(effects)
        return ExternalExtensionInstallConfirmation(reference, reference, status, snapshot, _project_effects(effects), action)


def build_external_extension_install_workflow(
    *,
    facts: ExternalExtensionFactStore,
    resolve_install: Callable[..., Effect],
    acquire_resolved: Callable[..., Effect],
    installations: ExternalExtensionInstallationStore,
    lifecycle_commands: ExternalExtensionLifecycleCommandService,
    preview_mcp_import: Callable[..., object],
) -> ExternalExtensionInstallWorkflow:
    """Compose the public workflow without publishing raw execution authority."""

    return ExternalExtensionInstallWorkflow(
        _ExternalExtensionWorkflowPort(
            facts.record_proposal,
            facts.load_proposal,
            facts.load_intent,
            facts.confirm_source_proposal,
            facts.source_confirmation_for_intent,
            resolve_install,
            facts.resolution_reference,
            facts.load_resolution,
            facts.confirm_resolved_revision,
            acquire_resolved,
            facts.intake_receipt,
            facts.load_intake,
            installations.confirm_review,
            installations.install_disabled,
            lifecycle_commands.execute,
            installations.load,
            installations.revision_history,
            installations.load_revision,
            facts.confirm_lifecycle_action,
            preview_mcp_import,
        ),
    )


def _single_reference(preview_id: str | None, intake_ref: str | None) -> str:
    if (preview_id is None) == (intake_ref is None):
        raise ExternalExtensionInstallWorkflowError("supply exactly one preview id or intake reference")
    reference = preview_id if preview_id is not None else intake_ref
    if not isinstance(reference, str) or not reference:
        raise ExternalExtensionInstallWorkflowError("preview reference is invalid")
    return reference


def _required_text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ExternalExtensionInstallWorkflowError(f"immutable intake {key} is invalid")
    return value


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or any(not isinstance(item, str) or not item for item in value):
        raise ExternalExtensionInstallWorkflowError(f"{label} are invalid")
    return tuple(value)


def _derived(prefix: str, semantic: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ExternalExtensionInstallWorkflowError("workflow request is not serializable") from error
    return f"nl-{prefix}-{blake3(encoded).hexdigest()[:32]}"


def _project_effects(effects: Iterable[Effect]) -> tuple[ExternalExtensionEffectProjection, ...]:
    return tuple(
        ExternalExtensionEffectProjection(
            operation_id=effect.operation_id,
            state=effect.state,
            receipt_ref=effect.result_ref,
        )
        for effect in effects
    )


def _incomplete_status(effects: Iterable[Effect]) -> str:
    states = {effect.state for effect in effects}
    if EffectState.SETTLED_ERR in states:
        return "failed"
    if EffectState.UNKNOWN in states:
        return "needs_attention"
    return "pending"
