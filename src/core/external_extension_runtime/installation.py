"""Local installation authority for quarantined external extensions.

This module deliberately persists only lifecycle projections and immutable
evidence references.  It neither materializes an artifact nor selects an
activation route; those operations remain Core Effect handler work.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from core.external_extensions import (
    CompatibilityIssue,
    ContributionCandidate,
    ExtensionManifest,
    ExtensionReviewPlan,
    FrozenRuntimeContract,
    MCPImportReviewContext,
    PermissionPlan,
    ResolvedSource,
    derive_review_plan,
    derive_mcp_import_review_inputs,
)
from core.storage_provider import (
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SQLiteUnitOfWorkConflict,
)

from .fact_store import ExternalExtensionFactStore, intent_ref
from .terminal_receipts import (
    ExternalExtensionLifecycleIntent,
    ExternalExtensionReceiptExpectation,
    ExternalExtensionTerminalReceipt,
    ExternalExtensionTerminalReceiptConflict,
    ExternalExtensionTerminalReceiptVerifier,
    terminal_receipt_operation_id,
)


class ExternalExtensionInstallationError(ValueError):
    """Raised when a local installation lifecycle request is invalid."""


class ExternalExtensionInstallationConflict(ExternalExtensionInstallationError):
    """Raised when an immutable fact, command replay, or CAS request drifts."""


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:-]{7,159}$")
_ROOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,159}$")
_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")
_ACTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@~-]{1,159}$")
_SKILL_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_REPOSITORY_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_APPLICATION_SKILL_CONSUMERS = (
    "answer.model-request",
    "document.generate",
    "turn.workbench-question",
)
_INSTALLATIONS = "external_extension_installations"
_REVISIONS = "external_extension_installation_revisions"
_CONFIRMATIONS = "external_extension_review_confirmations"
_HEALTH = "external_extension_health_projections"
_COMMANDS = "external_extension_installation_commands"
LIFECYCLE_RESERVATIONS_COLLECTION = "external_extension_lifecycle_reservations"
LIFECYCLE_COMMANDS_COLLECTION = "external_extension_lifecycle_commands"
UNINSTALL_TOMBSTONES_COLLECTION = "external_extension_uninstall_tombstones"
_CONFIRMATION_REF = "crp://external-extension-review-confirmations/"
_REVISION_REF = "crp://external-extension-installation-revisions/"


@dataclass(frozen=True, slots=True)
class InstallationSnapshot:
    root_id: str
    extension_id: str
    state_revision: int
    latest_allocated_revision: int
    candidate_revision: int | None
    candidate_revision_ref: str | None
    candidate_status: str | None
    active_revision: int | None
    active_revision_ref: str | None
    uninstalled: bool = False
    uninstall_receipt_ref: str | None = None

    @property
    def status(self) -> str:
        return self.candidate_status or (
            "active" if self.active_revision is not None
            else "uninstalled" if self.uninstalled
            else "disabled"
        )


@dataclass(frozen=True, slots=True)
class InstallationRevision:
    root_id: str
    extension_id: str
    revision: int
    revision_ref: str
    intake_ref: str
    artifact_ref: str
    artifact_receipt_ref: str
    artifact_content_sha256: str
    review_confirmation_ref: str | None
    manifest_identity: str
    review_plan_identity: str
    activation_plan_identity: str
    health_plan_identity: str
    projection: str


@dataclass(frozen=True, slots=True)
class InstallationRevisionSummary:
    """Path-free, project-scoped lifecycle history projection."""

    revision: int
    revision_ref: str
    intake_ref: str
    artifact_receipt_ref: str
    review_confirmation_ref: str | None
    projection: str
    health_verified: bool
    active: bool
    candidate: bool


@dataclass(frozen=True, slots=True)
class ExternalSkillActivationBinding:
    """Immutable, host-owned projection from one reviewed Skill contribution."""

    skill_id: str
    source_path: str
    package_layout: str
    description: str
    trigger_boundary: str
    model_invocable: bool = True
    user_invocable: bool = True
    allowed_consumers: tuple[str, ...] = _APPLICATION_SKILL_CONSUMERS
    priority: int = 500
    trigger_terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.skill_id, str) or not _SKILL_ID.fullmatch(self.skill_id):
            raise ExternalExtensionInstallationError("activation Skill id is invalid")
        _text(self.source_path, "activation Skill source path")
        if self.package_layout not in {"package_directory", "flat_markdown"}:
            raise ExternalExtensionInstallationError("activation Skill package layout is invalid")
        for value, label in (
            (self.description, "activation Skill description"),
            (self.trigger_boundary, "activation Skill trigger boundary"),
        ):
            _text(value, label)
            if len(value) > 2048:
                raise ExternalExtensionInstallationError(f"{label} is too long")
        if not isinstance(self.model_invocable, bool) or not isinstance(
            self.user_invocable, bool
        ):
            raise ExternalExtensionInstallationError(
                "activation Skill invocation policy is invalid"
            )
        object.__setattr__(self, "allowed_consumers", tuple(self.allowed_consumers))
        object.__setattr__(self, "trigger_terms", tuple(self.trigger_terms))
        if self.allowed_consumers != _APPLICATION_SKILL_CONSUMERS:
            raise ExternalExtensionInstallationError("activation Skill consumer policy drifted")
        if self.priority != 500 or self.trigger_terms:
            raise ExternalExtensionInstallationError("activation Skill selection policy drifted")


@dataclass(frozen=True, slots=True)
class ExternalExtensionActivationPlan:
    """Derived immutable plan; packages cannot inject binding or runtime policy."""

    root_id: str
    extension_id: str
    source_format: str
    activation_routes: tuple[str, ...]
    skill_bindings: tuple[ExternalSkillActivationBinding, ...]
    manifest_identity: str
    review_plan_identity: str
    materializer_revision: str = "1"

    def __post_init__(self) -> None:
        _root_id(self.root_id)
        _extension_id(self.extension_id)
        _text(self.source_format, "activation source format")
        object.__setattr__(self, "activation_routes", tuple(self.activation_routes))
        object.__setattr__(self, "skill_bindings", tuple(self.skill_bindings))
        if tuple(sorted(set(self.activation_routes))) != self.activation_routes:
            raise ExternalExtensionInstallationError("activation routes are invalid")
        if len({item.skill_id for item in self.skill_bindings}) != len(self.skill_bindings):
            raise ExternalExtensionInstallationError("activation Skill identities collide")
        _sha(self.manifest_identity)
        _sha(self.review_plan_identity)
        if self.materializer_revision != "1":
            raise ExternalExtensionInstallationError("activation materializer revision is invalid")

    @property
    def identity(self) -> str:
        return _digest(asdict(self))

    @property
    def is_pure_application_skill(self) -> bool:
        return self.activation_routes == ("application_skill_import",) and bool(
            self.skill_bindings
        )


class ExternalExtensionInstallationStore:
    """CAS-backed install projections bound to immutable intake evidence."""

    def __init__(
        self,
        records: SQLiteStructuredRecordStore,
        facts: ExternalExtensionFactStore,
        terminal_receipts: ExternalExtensionTerminalReceiptVerifier,
    ) -> None:
        self._records = records
        self._facts = facts
        if not isinstance(terminal_receipts, ExternalExtensionTerminalReceiptVerifier):
            raise TypeError("installation store requires a terminal receipt verifier")
        self._terminal_receipts = terminal_receipts

    def confirm_review(
        self,
        intake_ref: str,
        *,
        confirmation_id: str,
        confirmation_ids: tuple[str, ...],
        actor: str,
        reason: str,
    ) -> str:
        binding = self._binding(intake_ref)
        identity = _id(confirmation_id, "review confirmation id")
        expected_ids = binding.review_plan.confirmation_ids
        if tuple(confirmation_ids) != expected_ids:
            raise ExternalExtensionInstallationError("review confirmation ids do not match the review plan")
        payload = _canonical(
            {
                "schema_version": "1.0.0",
                "confirmation_id": identity,
                "confirmation_ref": confirmation_ref(identity),
                "root_id": binding.root_id,
                "intake_ref": intake_ref,
                "artifact_ref": binding.artifact_ref,
                "artifact_receipt_ref": binding.artifact_receipt_ref,
                "artifact_content_sha256": binding.artifact_content_sha256,
                "manifest": asdict(binding.manifest),
                "review_plan": asdict(binding.review_plan),
                "confirmation_ids": list(expected_ids),
                "actor": _actor(actor),
                "reason": _reason(reason),
            }
        )
        self._put_immutable(_CONFIRMATIONS, _confirmation_key(identity), payload)
        return confirmation_ref(identity)

    def mcp_import_review_context(self, intake_ref: str) -> MCPImportReviewContext:
        """Reconstruct sanitized MCP review facts from one immutable intake."""

        binding = self._binding(intake_ref)
        review_inputs = derive_mcp_import_review_inputs(
            binding.manifest,
            binding.review_plan,
        )
        return MCPImportReviewContext(
            project_id=binding.root_id,
            extension_id=binding.manifest.extension_id,
            intake_ref=binding.intake_ref,
            artifact_ref=binding.artifact_ref,
            artifact_receipt_ref=binding.artifact_receipt_ref,
            artifact_content_sha256=binding.artifact_content_sha256,
            manifest_identity=_digest(asdict(binding.manifest)),
            review_plan_identity=_digest(asdict(binding.review_plan)),
            review_inputs=review_inputs,
        )

    def install_disabled(
        self,
        intake_ref: str,
        *,
        command_id: str,
        expected_state_revision: int,
        review_confirmation_ref: str | None = None,
    ) -> InstallationSnapshot:
        binding = self._binding(intake_ref)
        command = _command_id(command_id)
        confirmation = self._require_confirmation(binding, review_confirmation_ref)
        command_payload = {
            "operation": "install_disabled",
            "root_id": binding.root_id,
            "intake_ref": intake_ref,
            "extension_id": binding.manifest.extension_id,
            "expected_state_revision": _revision(expected_state_revision),
            "review_confirmation_ref": confirmation,
        }
        return self._transition_install(
            binding=binding,
            command=command,
            command_payload=command_payload,
            expected_state_revision=expected_state_revision,
            confirmation_ref_value=confirmation,
        )

    def record_health(
        self,
        revision_reference: str,
        *,
        effect_operation_id: str,
        command_id: str,
        expected_state_revision: int,
    ) -> InstallationSnapshot:
        revision = self.load_revision(revision_reference)
        self.load(revision.extension_id, root_id=revision.root_id)
        binding = self._binding(revision.intake_ref)
        if binding.manifest.extension_id != revision.extension_id:
            raise ExternalExtensionInstallationConflict("installation revision intake identity drifted")
        command = _command_id(command_id)
        receipt = self._verified_receipt(effect_operation_id, action="health", revision=revision)
        assert receipt.passed is not None
        health_payload = _canonical(
            {
                "schema_version": "1.0.0",
                "revision_ref": revision_reference,
                "receipt_ref": receipt.receipt_ref,
            }
        )
        command_payload = {
            "operation": "record_health",
            "root_id": revision.root_id,
            "revision_ref": revision_reference,
            "effect_operation_id": receipt.effect_operation_id,
            "receipt_ref": receipt.receipt_ref,
            "expected_state_revision": _revision(expected_state_revision),
        }
        try:
            with self._records.begin() as uow:
                replay = uow.read(
                    _COMMANDS,
                    _command_key(revision.root_id, revision.extension_id, command),
                )
                if replay is not None:
                    _require_equal(replay.payload.get("request"), command_payload)
                    snapshot = _snapshot_from_payload(replay.payload.get("outcome"))
                    self._require_snapshot_pointers(snapshot, uow.read)
                    uow.rollback()
                    return snapshot
                state = _required(
                    uow.read(
                        _INSTALLATIONS,
                        _installation_key(revision.root_id, revision.extension_id),
                    ),
                    "installation",
                )
                snapshot = self._snapshot_from_record(state)
                self._require_snapshot_pointers(snapshot, uow.read)
                _require_state(snapshot, expected_state_revision)
                if snapshot.candidate_revision != revision.revision or snapshot.candidate_revision_ref != revision_reference:
                    raise ExternalExtensionInstallationConflict("health can only update the current installation revision")
                health_key = _revision_key(
                    revision.root_id, revision.extension_id, revision.revision,
                )
                existing_health = uow.read(_HEALTH, health_key)
                if existing_health is None:
                    uow.put(_HEALTH, health_key, health_payload, expected_revision=0)
                else:
                    _require_equal(existing_health.payload, health_payload)
                status = "health_verified_disabled" if receipt.passed else "disabled"
                next_payload = self._state_payload(snapshot, candidate_status=status)
                updated = uow.put(
                    _INSTALLATIONS,
                    _installation_key(revision.root_id, revision.extension_id),
                    next_payload,
                    expected_revision=state.revision,
                )
                uow.put(
                    _COMMANDS,
                    _command_key(revision.root_id, revision.extension_id, command),
                    _command_outcome(command_payload, self._snapshot_from_record(updated)),
                    expected_revision=0,
                )
                uow.commit()
                return self._snapshot_from_record(updated)
        except SQLiteUnitOfWorkConflict as error:
            raise ExternalExtensionInstallationConflict(str(error)) from error

    def finalize_activation(
        self, revision_reference: str, *, effect_operation_id: str,
        command_id: str, expected_state_revision: int,
    ) -> InstallationSnapshot:
        revision = self.load_revision(revision_reference)
        self._require_health_verified(revision_reference, revision)
        terminal = self._verified_receipt(effect_operation_id, action="activation", revision=revision)
        return self._state_transition(
            root_id=revision.root_id,
            extension_id=revision.extension_id,
            command_id=command_id,
            expected_state_revision=expected_state_revision,
            operation="finalize_activation",
            extra={"revision_ref": revision_reference, "effect_operation_id": terminal.effect_operation_id, "receipt_ref": terminal.receipt_ref},
            mutate=lambda current: self._state_payload(
                current, candidate_revision=None, candidate_revision_ref=None, candidate_status=None,
                active_revision=revision.revision, active_revision_ref=revision_reference
            ),
            require=lambda current: current.candidate_revision == revision.revision and current.candidate_revision_ref == revision_reference,
        )

    def finalize_disable(self, revision_reference: str, *, effect_operation_id: str, command_id: str, expected_state_revision: int) -> InstallationSnapshot:
        revision = self.load_revision(revision_reference)
        terminal = self._verified_receipt(effect_operation_id, action="disable", revision=revision)
        return self._state_transition(
            root_id=revision.root_id,
            extension_id=revision.extension_id,
            command_id=command_id,
            expected_state_revision=expected_state_revision,
            operation="finalize_disable",
            extra={"revision_ref": revision_reference, "effect_operation_id": terminal.effect_operation_id, "receipt_ref": terminal.receipt_ref},
            mutate=lambda current: self._state_payload(current, active_revision=None, active_revision_ref=None),
            require=lambda current: current.active_revision == revision.revision and current.active_revision_ref == revision_reference,
        )

    def finalize_uninstall(
        self, revision_reference: str, *, effect_operation_id: str,
        command_id: str, expected_state_revision: int,
    ) -> InstallationSnapshot:
        revision = self.load_revision(revision_reference)
        terminal = self._verified_receipt(
            effect_operation_id, action="uninstall", revision=revision,
        )
        tombstone = self.load_uninstall_tombstone(effect_operation_id)
        expected_tombstone = {
            "root_id": revision.root_id,
            "extension_id": revision.extension_id,
            "revision_ref": revision.revision_ref,
            "expected_state_revision": expected_state_revision,
        }
        if any(tombstone.get(key) != value for key, value in expected_tombstone.items()):
            raise ExternalExtensionInstallationConflict("uninstall tombstone drifted")
        return self._state_transition(
            root_id=revision.root_id,
            extension_id=revision.extension_id,
            command_id=command_id,
            expected_state_revision=expected_state_revision,
            operation="finalize_uninstall",
            extra={
                "revision_ref": revision_reference,
                "effect_operation_id": terminal.effect_operation_id,
                "receipt_ref": terminal.receipt_ref,
            },
            mutate=lambda current: self._state_payload(
                current,
                candidate_revision=None,
                candidate_revision_ref=None,
                candidate_status=None,
                active_revision=None,
                active_revision_ref=None,
                uninstalled=True,
                uninstall_receipt_ref=terminal.receipt_ref,
            ),
            require=lambda current: (
                current.active_revision == revision.revision
                and current.active_revision_ref == revision_reference
            ),
        )

    def plan_uninstall_in_uow(
        self, revision: InstallationRevision, *, effect_operation_id: str,
        command_id: str, expected_state_revision: int, uow,
    ) -> None:
        """Persist the destructive-operation tombstone before Core dispatch."""

        payload = _canonical({
            "schema_version": "1.0.0",
            "effect_operation_id": _id(effect_operation_id, "effect operation id"),
            "command_id": _command_id(command_id),
            "root_id": revision.root_id,
            "extension_id": revision.extension_id,
            "revision_ref": revision.revision_ref,
            "managed_revision": revision.revision,
            "expected_state_revision": _revision(expected_state_revision),
            "phase": "planned",
        })
        existing = uow.read(UNINSTALL_TOMBSTONES_COLLECTION, effect_operation_id)
        if existing is None:
            uow.put(
                UNINSTALL_TOMBSTONES_COLLECTION, effect_operation_id, payload,
                expected_revision=0,
            )
        else:
            _require_equal(existing.payload, payload)

    def load_uninstall_tombstone(self, effect_operation_id: str) -> dict[str, object]:
        operation = _id(effect_operation_id, "effect operation id")
        record = _required(
            self._records.read(UNINSTALL_TOMBSTONES_COLLECTION, operation),
            "uninstall tombstone",
        )
        payload = _canonical(record.payload)
        if (
            set(payload) != {
                "schema_version", "effect_operation_id", "command_id", "root_id",
                "extension_id", "revision_ref", "managed_revision",
                "expected_state_revision", "phase",
            }
            or payload.get("schema_version") != "1.0.0"
            or payload.get("effect_operation_id") != operation
            or payload.get("phase") != "planned"
        ):
            raise ExternalExtensionInstallationConflict("uninstall tombstone is invalid")
        return payload

    def finalize_rollback(
        self,
        extension_id: str,
        *,
        target_revision_reference: str,
        effect_operation_id: str,
        command_id: str,
        expected_state_revision: int,
    ) -> InstallationSnapshot:
        identity = _extension_id(extension_id)
        target = self.load_revision(target_revision_reference)
        if target.extension_id != identity:
            raise ExternalExtensionInstallationError("rollback target belongs to another extension")
        self._require_health_verified(target_revision_reference, target)
        terminal = self._verified_receipt(effect_operation_id, action="rollback", revision=target)
        return self._state_transition(
            root_id=target.root_id,
            extension_id=identity,
            command_id=command_id,
            expected_state_revision=expected_state_revision,
            operation="finalize_rollback",
            extra={"target_revision_ref": target_revision_reference, "effect_operation_id": terminal.effect_operation_id, "receipt_ref": terminal.receipt_ref},
            mutate=lambda current: self._state_payload(
                current,
                active_revision=target.revision, active_revision_ref=target_revision_reference,
            ),
            require=lambda current: current.active_revision is not None and target.revision < current.active_revision,
        )

    def load(
        self, extension_id: str, *, root_id: str | None = None,
    ) -> InstallationSnapshot:
        """Load one project-scoped installation; reject ambiguous global reads."""

        extension = _extension_id(extension_id)
        if root_id is None:
            matches = [
                self._snapshot_from_record(record)
                for record in self._records.list_all()
                if record.collection == _INSTALLATIONS
                and record.payload.get("extension_id") == extension
            ]
            if len(matches) != 1:
                raise ExternalExtensionInstallationError(
                    "installation root is required when an extension is not unique"
                )
            snapshot = matches[0]
        else:
            root = _root_id(root_id)
            snapshot = self._snapshot_from_record(
                _required(
                    self._records.read(_INSTALLATIONS, _installation_key(root, extension)),
                    "installation",
                )
            )
            if snapshot.root_id != root:
                raise ExternalExtensionInstallationConflict(
                    "installation state crossed its project root"
                )
        self._require_snapshot_pointers(snapshot, self._records.read)
        self._require_snapshot_bindings(snapshot)
        return snapshot

    def active_revisions(self, *, root_id: str) -> tuple[InstallationRevision, ...]:
        """Return verified active revision projections for one project root."""

        root = _root_id(root_id)
        active: list[InstallationRevision] = []
        for record in self._records.list_all():
            if record.collection != _INSTALLATIONS:
                continue
            snapshot = self._snapshot_from_record(record)
            if snapshot.root_id != root or snapshot.active_revision_ref is None:
                continue
            self._require_snapshot_pointers(snapshot, self._records.read)
            revision = self.load_revision(snapshot.active_revision_ref)
            if revision.root_id != root:
                raise ExternalExtensionInstallationConflict(
                    "active installation revision crossed its project root"
                )
            active.append(revision)
        return tuple(sorted(active, key=lambda item: (item.extension_id, item.revision)))

    def revision_history(
        self, extension_id: str, *, root_id: str,
    ) -> tuple[InstallationRevisionSummary, ...]:
        """Return verified, path-free history for one project installation."""

        snapshot = self.load(extension_id, root_id=root_id)
        history: list[InstallationRevisionSummary] = []
        for record in self._records.list_all():
            if record.collection != _REVISIONS:
                continue
            payload = record.payload
            if (
                payload.get("root_id") != snapshot.root_id
                or payload.get("extension_id") != snapshot.extension_id
            ):
                continue
            reference = payload.get("revision_ref")
            if not isinstance(reference, str):
                raise ExternalExtensionInstallationConflict(
                    "installation revision reference is invalid"
                )
            revision = self.load_revision(reference)
            health_verified = False
            health = self._records.read(
                _HEALTH,
                _revision_key(revision.root_id, revision.extension_id, revision.revision),
            )
            if health is not None:
                self._require_health_verified(reference, revision)
                health_verified = True
            history.append(
                InstallationRevisionSummary(
                    revision=revision.revision,
                    revision_ref=revision.revision_ref,
                    intake_ref=revision.intake_ref,
                    artifact_receipt_ref=revision.artifact_receipt_ref,
                    review_confirmation_ref=revision.review_confirmation_ref,
                    projection=revision.projection,
                    health_verified=health_verified,
                    active=snapshot.active_revision_ref == revision.revision_ref,
                    candidate=snapshot.candidate_revision_ref == revision.revision_ref,
                )
            )
        return tuple(sorted(history, key=lambda item: item.revision, reverse=True))

    def load_revision(self, reference: str) -> InstallationRevision:
        identity = _reference_id(reference, _REVISION_REF, "installation revision")
        record = _required(self._records.read(_REVISIONS, identity), "installation revision")
        revision = self._revision_from_record(reference, record)
        self._require_revision_confirmation(revision, self._records.read)
        self._require_revision_binding(revision)
        return revision

    def load_activation_plan(
        self, revision_reference: str,
    ) -> ExternalExtensionActivationPlan:
        """Rebuild the host-owned plan from the immutable intake and verify its digest."""

        revision = self.load_revision(revision_reference)
        plan = _activation_plan(self._binding(revision.intake_ref))
        if (
            plan.manifest_identity != revision.manifest_identity
            or plan.review_plan_identity != revision.review_plan_identity
            or plan.identity != revision.activation_plan_identity
        ):
            raise ExternalExtensionInstallationConflict(
                "installation activation plan drifted from immutable intake evidence"
            )
        return plan

    def build_lifecycle_intent(
        self,
        revision_reference: str,
        *,
        action: str,
        intent_id: str,
    ) -> ExternalExtensionLifecycleIntent:
        """Derive lifecycle work only from the immutable installation revision."""

        revision = self.load_revision(revision_reference)
        binding = self._binding(revision.intake_ref)
        snapshot = self.load(revision.extension_id, root_id=revision.root_id)
        _require_lifecycle_snapshot(snapshot, revision, action)
        if action == "activation":
            self._require_health_verified(revision_reference, revision)
        elif action == "rollback":
            self._require_health_verified(revision_reference, revision)
        return ExternalExtensionLifecycleIntent(
            intent_id=intent_id,
            action=action,
            root_id=revision.root_id,
            revision_ref=revision.revision_ref,
            intake_ref=revision.intake_ref,
            artifact_ref=revision.artifact_ref,
            artifact_receipt_ref=revision.artifact_receipt_ref,
            artifact_content_sha256=revision.artifact_content_sha256,
            manifest_identity=revision.manifest_identity,
            review_plan_identity=revision.review_plan_identity,
            activation_plan_identity=revision.activation_plan_identity,
            health_plan_identity=revision.health_plan_identity,
            health_checks=(binding.review_plan.health_checks if action == "health" else ()),
        )

    def require_lifecycle_state(
        self,
        revision: InstallationRevision,
        *,
        action: str,
        expected_state_revision: int,
        reader,
    ) -> InstallationSnapshot:
        """Verify lifecycle CAS and preconditions through the caller's SQLite UoW."""

        if not isinstance(revision, InstallationRevision) or not callable(reader):
            raise TypeError("lifecycle state verification requires a revision and reader")
        record = _required(
            reader(
                _INSTALLATIONS,
                _installation_key(revision.root_id, revision.extension_id),
            ),
            "installation",
        )
        snapshot = self._snapshot_from_record(record)
        self._require_snapshot_pointers(snapshot, reader)
        _require_state(snapshot, expected_state_revision)
        _require_lifecycle_snapshot(snapshot, revision, action)
        return snapshot

    def _revision_from_record(
        self, reference: str, record: SQLiteStructuredRecord,
    ) -> InstallationRevision:
        identity = _reference_id(reference, _REVISION_REF, "installation revision")
        payload = record.payload
        try:
            confirmation = payload.get("review_confirmation_ref")
            if confirmation is not None:
                _reference_id(confirmation, _CONFIRMATION_REF, "review confirmation")
            revision = InstallationRevision(
                root_id=_root_id(payload["root_id"]),
                extension_id=_extension_id(payload["extension_id"]),
                revision=_revision(payload["revision"]),
                revision_ref=reference,
                intake_ref=_text(payload["intake_ref"], "intake reference"),
                artifact_ref=_text(payload["artifact_ref"], "artifact reference"),
                artifact_receipt_ref=_text(payload["artifact_receipt_ref"], "artifact receipt reference"),
                artifact_content_sha256=_sha(payload["artifact_content_sha256"]),
                review_confirmation_ref=confirmation,
                manifest_identity=_sha(payload["manifest_identity"]),
                review_plan_identity=_sha(payload["review_plan_identity"]),
                activation_plan_identity=_sha(payload["activation_plan_identity"]),
                health_plan_identity=_sha(payload["health_plan_identity"]),
                projection=_projection(payload["projection"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ExternalExtensionInstallationConflict("installation revision is invalid") from error
        if payload.get("revision_ref") != reference or identity != _revision_key(
            revision.root_id, revision.extension_id, revision.revision,
        ):
            raise ExternalExtensionInstallationConflict("installation revision identity drifted")
        return revision

    def _require_revision_confirmation(self, revision: InstallationRevision, reader) -> None:
        if revision.review_confirmation_ref is None:
            return
        identity = _reference_id(
            revision.review_confirmation_ref, _CONFIRMATION_REF, "review confirmation",
        )
        confirmation = _required(
            reader(_CONFIRMATIONS, _confirmation_key(identity)),
            "review confirmation",
        )
        payload = confirmation.payload
        if (
            payload.get("confirmation_ref") != revision.review_confirmation_ref
            or payload.get("root_id") != revision.root_id
            or payload.get("intake_ref") != revision.intake_ref
            or payload.get("artifact_ref") != revision.artifact_ref
            or payload.get("artifact_receipt_ref") != revision.artifact_receipt_ref
            or payload.get("artifact_content_sha256") != revision.artifact_content_sha256
            or _digest(payload.get("review_plan")) != revision.review_plan_identity
        ):
            raise ExternalExtensionInstallationConflict(
                "installation revision review confirmation drifted"
            )

    def _require_revision_binding(self, revision: InstallationRevision) -> None:
        binding = self._binding(revision.intake_ref)
        expected = (
            binding.root_id,
            binding.manifest.extension_id,
            binding.artifact_ref,
            binding.artifact_receipt_ref,
            binding.artifact_content_sha256,
            _digest(asdict(binding.manifest)),
            _digest(asdict(binding.review_plan)),
            _activation_plan(binding).identity,
            _health_identity(binding.review_plan),
            bool(binding.review_plan.confirmation_ids),
        )
        actual = (
            revision.root_id,
            revision.extension_id,
            revision.artifact_ref,
            revision.artifact_receipt_ref,
            revision.artifact_content_sha256,
            revision.manifest_identity,
            revision.review_plan_identity,
            revision.activation_plan_identity,
            revision.health_plan_identity,
            revision.review_confirmation_ref is not None,
        )
        if actual != expected:
            raise ExternalExtensionInstallationConflict(
                "installation revision does not bind immutable intake evidence"
            )
        if revision.review_confirmation_ref is not None:
            confirmation_identity = _reference_id(
                revision.review_confirmation_ref,
                _CONFIRMATION_REF,
                "review confirmation",
            )
            confirmation = _required(
                self._records.read(
                    _CONFIRMATIONS, _confirmation_key(confirmation_identity),
                ),
                "review confirmation",
            )
            confirmation_expected = {
                "confirmation_ref": revision.review_confirmation_ref,
                "root_id": binding.root_id,
                "intake_ref": binding.intake_ref,
                "artifact_ref": binding.artifact_ref,
                "artifact_receipt_ref": binding.artifact_receipt_ref,
                "artifact_content_sha256": binding.artifact_content_sha256,
                "manifest": asdict(binding.manifest),
                "review_plan": asdict(binding.review_plan),
                "confirmation_ids": list(binding.review_plan.confirmation_ids),
            }
            for key, expected_value in confirmation_expected.items():
                if _canonical_value(confirmation.payload.get(key)) != _canonical_value(
                    expected_value
                ):
                    raise ExternalExtensionInstallationConflict(
                        "installation review confirmation no longer binds intake evidence"
                    )

    def _require_snapshot_pointers(self, snapshot: InstallationSnapshot, reader) -> None:
        pointers = (
            (
                snapshot.latest_allocated_revision,
                installation_revision_ref(
                    snapshot.root_id,
                    snapshot.extension_id,
                    snapshot.latest_allocated_revision,
                ),
            ),
            (snapshot.candidate_revision, snapshot.candidate_revision_ref),
            (snapshot.active_revision, snapshot.active_revision_ref),
        )
        seen: set[tuple[int, str]] = set()
        for number, reference in pointers:
            if number is None or reference is None:
                continue
            if (number, reference) in seen:
                continue
            seen.add((number, reference))
            record = _required(
                reader(
                    _REVISIONS,
                    _revision_key(snapshot.root_id, snapshot.extension_id, number),
                ),
                "installation revision",
            )
            revision = self._revision_from_record(reference, record)
            self._require_revision_confirmation(revision, reader)
            if (
                revision.root_id != snapshot.root_id
                or revision.extension_id != snapshot.extension_id
                or revision.revision != number
            ):
                raise ExternalExtensionInstallationConflict(
                    "installation state points to a mismatched revision"
                )

    def _require_snapshot_bindings(self, snapshot: InstallationSnapshot) -> None:
        references = {
            installation_revision_ref(
                snapshot.root_id,
                snapshot.extension_id,
                snapshot.latest_allocated_revision,
            ),
            snapshot.candidate_revision_ref,
            snapshot.active_revision_ref,
        }
        for reference in references:
            if reference is not None:
                self.load_revision(reference)

    def _transition_install(self, *, binding: _IntakeBinding, command: str, command_payload: dict[str, object], expected_state_revision: int, confirmation_ref_value: str | None) -> InstallationSnapshot:
        extension_id = binding.manifest.extension_id
        installation_key = _installation_key(binding.root_id, extension_id)
        if self._records.read(_INSTALLATIONS, installation_key) is not None:
            self.load(extension_id, root_id=binding.root_id)
        try:
            with self._records.begin() as uow:
                replay = uow.read(
                    _COMMANDS,
                    _command_key(binding.root_id, extension_id, command),
                )
                if replay is not None:
                    _require_equal(replay.payload.get("request"), command_payload)
                    snapshot = _snapshot_from_payload(replay.payload.get("outcome"))
                    self._require_snapshot_pointers(snapshot, uow.read)
                    uow.rollback()
                    return snapshot
                existing = uow.read(_INSTALLATIONS, installation_key)
                if existing is not None:
                    current = self._snapshot_from_record(existing)
                    self._require_snapshot_pointers(current, uow.read)
                    if current.root_id != binding.root_id:
                        raise ExternalExtensionInstallationConflict(
                            "installation belongs to another project root"
                        )
                    _require_state(current, expected_state_revision)
                    next_revision = current.latest_allocated_revision + 1
                else:
                    if expected_state_revision != 0:
                        raise ExternalExtensionInstallationConflict("installation state revision is stale")
                    next_revision = 1
                self._require_unreserved_install(
                    uow,
                    root_id=binding.root_id,
                    extension_id=extension_id,
                    expected_state_revision=expected_state_revision,
                )
                revision_ref_value = installation_revision_ref(
                    binding.root_id, extension_id, next_revision,
                )
                revision_payload = _canonical(
                    {
                        "schema_version": "1.0.0",
                        "revision_ref": revision_ref_value,
                        "root_id": binding.root_id,
                        "extension_id": extension_id,
                        "revision": next_revision,
                        "intake_ref": binding.intake_ref,
                        "artifact_ref": binding.artifact_ref,
                        "artifact_receipt_ref": binding.artifact_receipt_ref,
                        "artifact_content_sha256": binding.artifact_content_sha256,
                        "review_confirmation_ref": confirmation_ref_value,
                        "manifest_identity": _digest(asdict(binding.manifest)),
                        "review_plan_identity": _digest(asdict(binding.review_plan)),
                        "activation_plan_identity": _activation_plan(binding).identity,
                        "health_plan_identity": _health_identity(binding.review_plan),
                        "projection": "installed_disabled",
                    }
                )
                revision_key = _revision_key(
                    binding.root_id, extension_id, next_revision,
                )
                existing_revision = uow.read(_REVISIONS, revision_key)
                if existing_revision is None:
                    uow.put(_REVISIONS, revision_key, revision_payload, expected_revision=0)
                else:
                    _require_equal(existing_revision.payload, revision_payload)
                if existing is None:
                    state_payload = self._new_state_payload(
                        binding.root_id, extension_id, next_revision, revision_ref_value,
                    )
                    updated = uow.put(
                        _INSTALLATIONS, installation_key, state_payload, expected_revision=0,
                    )
                else:
                    updated = uow.put(
                        _INSTALLATIONS,
                        installation_key,
                        self._state_payload(
                            current,
                            latest_allocated_revision=next_revision,
                            candidate_revision=next_revision,
                            candidate_revision_ref=revision_ref_value,
                            candidate_status="installed_disabled",
                            uninstalled=False,
                            uninstall_receipt_ref=None,
                        ),
                        expected_revision=existing.revision,
                    )
                uow.put(
                    _COMMANDS,
                    _command_key(binding.root_id, extension_id, command),
                    _command_outcome(command_payload, self._snapshot_from_record(updated)),
                    expected_revision=0,
                )
                uow.commit()
                return self._snapshot_from_record(updated)
        except SQLiteUnitOfWorkConflict as error:
            raise ExternalExtensionInstallationConflict(str(error)) from error

    @staticmethod
    def _require_unreserved_install(
        uow,
        *,
        root_id: str,
        extension_id: str,
        expected_state_revision: int,
    ) -> None:
        """Fence upgrades against lifecycle Effects reserved at this CAS state.

        The lifecycle command writes its reservation in the same SQLite
        ``BEGIN IMMEDIATE`` transaction that validates the installation state.
        Reading that reservation here makes the two possible orderings safe:
        either this install advances the CAS first and the command is rejected,
        or the command owns the state revision and this install is rejected
        before an older handler can mutate external bindings.
        """

        expected_keys = {
            "schema_version",
            "reservation_id",
            "command_id",
            "root_id",
            "extension_id",
            "expected_state_revision",
            "effect_operation_id",
        }
        for record in uow.list(LIFECYCLE_RESERVATIONS_COLLECTION):
            payload = record.payload
            # A reservation for another installation cannot influence this
            # CAS.  In particular, do not turn unrelated corrupt state into a
            # system-wide installation outage.
            if not isinstance(payload, Mapping):
                continue
            if (
                payload.get("root_id") != root_id
                or payload.get("extension_id") != extension_id
                or payload.get("expected_state_revision") != expected_state_revision
            ):
                continue
            try:
                if set(payload) != expected_keys or payload.get("schema_version") != "1.0.0":
                    raise ValueError("reservation shape drifted")
                reservation_id = _command_id(payload.get("reservation_id"))
                if record.object_id != reservation_id:
                    raise ValueError("reservation identity drifted")
                reserved_root = _root_id(payload.get("root_id"))
                reserved_extension = _extension_id(payload.get("extension_id"))
                reserved_state = _revision(payload.get("expected_state_revision"))
                command_id = _command_id(payload.get("command_id"))
                effect_operation_id = _command_id(payload.get("effect_operation_id"))
                command = _required(
                    uow.read(LIFECYCLE_COMMANDS_COLLECTION, command_id),
                    "lifecycle command reservation projection",
                )
                semantic = command.payload.get("semantic")
                if (
                    not isinstance(semantic, Mapping)
                    or semantic.get("root_id") != reserved_root
                    or semantic.get("extension_id") != reserved_extension
                    or semantic.get("expected_state_revision") != reserved_state
                    or command.payload.get("operation_id") != effect_operation_id
                    or not isinstance(command.payload.get("intent_ref"), str)
                ):
                    raise ValueError("reservation command linkage drifted")
            except (TypeError, ValueError) as error:
                raise ExternalExtensionInstallationConflict(
                    "lifecycle reservation authority is invalid"
                ) from error
            if (
                reserved_root == root_id
                and reserved_extension == extension_id
                and reserved_state == expected_state_revision
            ):
                raise ExternalExtensionInstallationConflict(
                    "installation state revision is reserved by a lifecycle command"
                )

    def _state_transition(self, *, root_id: str, extension_id: str, command_id: str, expected_state_revision: int, operation: str, extra: dict[str, object], mutate, require=None) -> InstallationSnapshot:
        root = _root_id(root_id)
        self.load(extension_id, root_id=root)
        command = _command_id(command_id)
        command_payload = _canonical({"operation": operation, "root_id": root, "extension_id": extension_id, "expected_state_revision": _revision(expected_state_revision), **extra})
        try:
            with self._records.begin() as uow:
                replay = uow.read(_COMMANDS, _command_key(root, extension_id, command))
                if replay is not None:
                    _require_equal(replay.payload.get("request"), command_payload)
                    snapshot = _snapshot_from_payload(replay.payload.get("outcome"))
                    self._require_snapshot_pointers(snapshot, uow.read)
                    uow.rollback()
                    return snapshot
                record = _required(
                    uow.read(_INSTALLATIONS, _installation_key(root, extension_id)),
                    "installation",
                )
                current = self._snapshot_from_record(record)
                self._require_snapshot_pointers(current, uow.read)
                _require_state(current, expected_state_revision)
                if require is not None and not require(current):
                    raise ExternalExtensionInstallationConflict("installation transition precondition is not satisfied")
                updated = uow.put(
                    _INSTALLATIONS,
                    _installation_key(root, extension_id),
                    mutate(current),
                    expected_revision=record.revision,
                )
                uow.put(
                    _COMMANDS,
                    _command_key(root, extension_id, command),
                    _command_outcome(command_payload, self._snapshot_from_record(updated)),
                    expected_revision=0,
                )
                uow.commit()
                return self._snapshot_from_record(updated)
        except SQLiteUnitOfWorkConflict as error:
            raise ExternalExtensionInstallationConflict(str(error)) from error

    def _binding(self, intake_ref: str) -> _IntakeBinding:
        payload = self._facts.load_intake(intake_ref)
        install_identity = _id(payload.get("intent_id"), "install intent id")
        install = self._facts.load_intent(intent_ref(install_identity))
        if install.project_id is None:
            raise ExternalExtensionInstallationError("installation requires a project-scoped intake")
        result = payload.get("result")
        if not isinstance(result, Mapping) or result.get("projection") == "quarantined":
            raise ExternalExtensionInstallationError("quarantined intake cannot be installed")
        manifest = _manifest(result.get("manifest"))
        review_plan = _review_plan(result.get("review_plan"))
        if manifest.source.artifact_ref != payload.get("artifact_ref") or review_plan.artifact_ref != manifest.source.artifact_ref:
            raise ExternalExtensionInstallationConflict("intake artifact identity drifted")
        if derive_review_plan(manifest) != review_plan:
            raise ExternalExtensionInstallationConflict("intake review plan drifted")
        return _IntakeBinding(
            root_id=_root_id(install.project_id),
            intake_ref=intake_ref,
            manifest=manifest,
            review_plan=review_plan,
            artifact_ref=_text(payload.get("artifact_ref"), "artifact reference"),
            artifact_receipt_ref=_text(payload.get("artifact_receipt_ref"), "artifact receipt reference"),
            artifact_content_sha256=_sha(payload.get("artifact_content_sha256")),
        )

    def _require_confirmation(self, binding: _IntakeBinding, value: str | None) -> str | None:
        if not binding.review_plan.confirmation_ids:
            if value is not None:
                raise ExternalExtensionInstallationError("unneeded review confirmation is forbidden")
            return None
        if not isinstance(value, str):
            raise ExternalExtensionInstallationError("install requires immutable review confirmation")
        identity = _reference_id(value, _CONFIRMATION_REF, "review confirmation")
        record = _required(
            self._records.read(_CONFIRMATIONS, _confirmation_key(identity)),
            "review confirmation",
        )
        expected = {
            "confirmation_ref": value,
            "root_id": binding.root_id,
            "intake_ref": binding.intake_ref,
            "artifact_ref": binding.artifact_ref,
            "artifact_receipt_ref": binding.artifact_receipt_ref,
            "artifact_content_sha256": binding.artifact_content_sha256,
            "manifest": asdict(binding.manifest),
            "review_plan": asdict(binding.review_plan),
            "confirmation_ids": list(binding.review_plan.confirmation_ids),
        }
        for key, expected_value in expected.items():
            if _canonical_value(record.payload.get(key)) != _canonical_value(expected_value):
                raise ExternalExtensionInstallationConflict("review confirmation does not bind the intake evidence")
        return value

    def _require_health_verified(self, revision_ref_value: str, revision: InstallationRevision) -> None:
        record = self._records.read(
            _HEALTH,
            _revision_key(revision.root_id, revision.extension_id, revision.revision),
        )
        if record is None:
            raise ExternalExtensionInstallationError("activation or rollback requires a health-verified revision")
        payload = record.payload
        if set(payload) != {"schema_version", "revision_ref", "receipt_ref"}:
            raise ExternalExtensionInstallationConflict("health projection contains copied Receipt facts")
        receipt_ref = _text(payload.get("receipt_ref"), "health receipt reference")
        if payload.get("schema_version") != "1.0.0" or payload.get("revision_ref") != revision_ref_value:
            raise ExternalExtensionInstallationConflict("health projection identity drifted")
        try:
            receipt = self._verified_receipt(
                terminal_receipt_operation_id(receipt_ref), action="health", revision=revision,
            )
        except ValueError as error:
            raise ExternalExtensionInstallationConflict(str(error)) from error
        if receipt.receipt_ref != receipt_ref or receipt.passed is not True:
            raise ExternalExtensionInstallationError("activation or rollback requires a health-verified revision")

    def _verified_receipt(
        self, effect_operation_id: str, *, action: str, revision: InstallationRevision,
    ) -> ExternalExtensionTerminalReceipt:
        expected = ExternalExtensionReceiptExpectation(
            action=action,
            root_id=revision.root_id,
            revision_ref=revision.revision_ref,
            intake_ref=revision.intake_ref,
            artifact_ref=revision.artifact_ref,
            artifact_receipt_ref=revision.artifact_receipt_ref,
            artifact_content_sha256=revision.artifact_content_sha256,
            manifest_identity=revision.manifest_identity,
            review_plan_identity=revision.review_plan_identity,
            activation_plan_identity=revision.activation_plan_identity,
            health_plan_identity=revision.health_plan_identity,
        )
        try:
            receipt = self._terminal_receipts.verify(effect_operation_id, expected=expected)
        except (ExternalExtensionTerminalReceiptConflict, ValueError) as error:
            raise ExternalExtensionInstallationConflict(str(error)) from error
        if action == "health":
            if receipt.observed_checks != self._binding(revision.intake_ref).review_plan.health_checks:
                raise ExternalExtensionInstallationConflict("health receipt checks do not bind review plan")
        return receipt

    @staticmethod
    def _new_state_payload(
        root_id: str, extension_id: str, revision: int, reference: str,
    ) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "root_id": root_id,
            "extension_id": extension_id,
            "latest_allocated_revision": revision,
            "candidate_revision": revision,
            "candidate_revision_ref": reference,
            "candidate_status": "installed_disabled",
            "active_revision": None,
            "active_revision_ref": None,
            "uninstalled": False,
            "uninstall_receipt_ref": None,
        }

    @staticmethod
    def _state_payload(current: InstallationSnapshot, **changes: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": "1.0.0", "root_id": current.root_id,
            "extension_id": current.extension_id,
            "latest_allocated_revision": current.latest_allocated_revision,
            "candidate_revision": current.candidate_revision,
            "candidate_revision_ref": current.candidate_revision_ref,
            "candidate_status": current.candidate_status,
            "active_revision": current.active_revision,
            "active_revision_ref": current.active_revision_ref,
            "uninstalled": current.uninstalled,
            "uninstall_receipt_ref": current.uninstall_receipt_ref,
        }
        payload.update(changes)
        return payload

    @staticmethod
    def _snapshot_from_record(record: SQLiteStructuredRecord) -> InstallationSnapshot:
        payload = record.payload
        try:
            snapshot = InstallationSnapshot(
                root_id=_root_id(payload["root_id"]),
                extension_id=_extension_id(payload["extension_id"]),
                state_revision=record.revision,
                latest_allocated_revision=_revision(payload["latest_allocated_revision"]),
                candidate_revision=payload["candidate_revision"] if isinstance(payload.get("candidate_revision"), int) and not isinstance(payload.get("candidate_revision"), bool) else None,
                candidate_revision_ref=payload.get("candidate_revision_ref") if isinstance(payload.get("candidate_revision_ref"), str) else None,
                candidate_status=payload.get("candidate_status") if isinstance(payload.get("candidate_status"), str) else None,
                active_revision=payload["active_revision"] if isinstance(payload.get("active_revision"), int) and not isinstance(payload.get("active_revision"), bool) else None,
                active_revision_ref=payload.get("active_revision_ref") if isinstance(payload.get("active_revision_ref"), str) else None,
                uninstalled=payload.get("uninstalled", False),
                uninstall_receipt_ref=payload.get("uninstall_receipt_ref") if isinstance(payload.get("uninstall_receipt_ref"), str) else None,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ExternalExtensionInstallationConflict("installation state is invalid") from error
        if (snapshot.candidate_revision is None) != (snapshot.candidate_revision_ref is None) or (snapshot.candidate_revision is None) != (snapshot.candidate_status is None):
            raise ExternalExtensionInstallationConflict("candidate state is invalid")
        if (snapshot.active_revision is None) != (snapshot.active_revision_ref is None):
            raise ExternalExtensionInstallationConflict("active state is invalid")
        if snapshot.latest_allocated_revision < max(snapshot.candidate_revision or 0, snapshot.active_revision or 0):
            raise ExternalExtensionInstallationConflict("allocated revision is invalid")
        if snapshot.latest_allocated_revision < 1:
            raise ExternalExtensionInstallationConflict("allocated revision is invalid")
        if snapshot.candidate_revision is not None and snapshot.candidate_revision_ref != installation_revision_ref(snapshot.root_id, snapshot.extension_id, snapshot.candidate_revision):
            raise ExternalExtensionInstallationConflict("candidate reference drifted")
        if snapshot.active_revision is not None and snapshot.active_revision_ref != installation_revision_ref(snapshot.root_id, snapshot.extension_id, snapshot.active_revision):
            raise ExternalExtensionInstallationConflict("active reference drifted")
        if snapshot.candidate_status not in {None, "installed_disabled", "health_verified_disabled", "disabled"}:
            raise ExternalExtensionInstallationConflict("candidate status is invalid")
        if not isinstance(snapshot.uninstalled, bool):
            raise ExternalExtensionInstallationConflict("uninstall state is invalid")
        if snapshot.uninstalled != (snapshot.uninstall_receipt_ref is not None):
            raise ExternalExtensionInstallationConflict("uninstall receipt state is invalid")
        if snapshot.uninstalled and (
            snapshot.active_revision is not None or snapshot.candidate_revision is not None
        ):
            raise ExternalExtensionInstallationConflict("uninstalled state retains execution pointers")
        return snapshot

    def _put_immutable(self, collection: str, object_id: str, payload: dict[str, object]) -> None:
        try:
            with self._records.begin() as uow:
                existing = uow.read(collection, object_id)
                if existing is None:
                    uow.put(collection, object_id, payload, expected_revision=0)
                else:
                    _require_equal(existing.payload, payload)
                uow.commit()
        except SQLiteUnitOfWorkConflict as error:
            raise ExternalExtensionInstallationConflict(str(error)) from error


@dataclass(frozen=True, slots=True)
class _IntakeBinding:
    root_id: str
    intake_ref: str
    manifest: ExtensionManifest
    review_plan: ExtensionReviewPlan
    artifact_ref: str
    artifact_receipt_ref: str
    artifact_content_sha256: str


def confirmation_ref(confirmation_id: str) -> str:
    return _CONFIRMATION_REF + _id(confirmation_id, "review confirmation id")


def installation_revision_ref(
    root_id: str, extension_id: str, revision: int,
) -> str:
    return _REVISION_REF + _revision_key(root_id, extension_id, revision)


def _command_outcome(request: Mapping[str, object], snapshot: InstallationSnapshot) -> dict[str, object]:
    return _canonical({"request": dict(request), "outcome": _snapshot_payload(snapshot)})


def _snapshot_payload(snapshot: InstallationSnapshot) -> dict[str, object]:
    return {
        "root_id": snapshot.root_id, "extension_id": snapshot.extension_id,
        "state_revision": snapshot.state_revision,
        "latest_allocated_revision": snapshot.latest_allocated_revision,
        "candidate_revision": snapshot.candidate_revision, "candidate_revision_ref": snapshot.candidate_revision_ref,
        "candidate_status": snapshot.candidate_status, "active_revision": snapshot.active_revision,
        "active_revision_ref": snapshot.active_revision_ref,
        "uninstalled": snapshot.uninstalled,
        "uninstall_receipt_ref": snapshot.uninstall_receipt_ref,
    }


def _snapshot_from_payload(value: object) -> InstallationSnapshot:
    if not isinstance(value, Mapping):
        raise ExternalExtensionInstallationConflict("installation command outcome is invalid")
    try:
        record = SQLiteStructuredRecord(
            collection=_INSTALLATIONS,
            object_id=_installation_key(value["root_id"], value["extension_id"]),
            payload=dict(value), revision=_revision(value["state_revision"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ExternalExtensionInstallationConflict("installation command outcome is invalid") from error
    return ExternalExtensionInstallationStore._snapshot_from_record(record)


def _manifest(value: object) -> ExtensionManifest:
    if not isinstance(value, Mapping):
        raise ExternalExtensionInstallationConflict("intake manifest is invalid")
    try:
        source = ResolvedSource(**dict(_mapping(value.get("source"), "manifest source")))
        contributions = tuple(ContributionCandidate(**dict(item)) for item in _items(value.get("contributions"), "manifest contributions"))
        permissions = PermissionPlan(**dict(_mapping(value.get("permission_plan"), "manifest permissions")))
        issues = tuple(CompatibilityIssue(**dict(item)) for item in _items(value.get("issues"), "manifest issues"))
        runtime = FrozenRuntimeContract(**dict(_mapping(value.get("runtime_contract"), "runtime contract")))
        return ExtensionManifest(
            extension_id=value["extension_id"], version=value.get("version"), source=source,
            source_format=value["source_format"], adapter_id=value["adapter_id"], adapter_revision=value["adapter_revision"],
            contributions=contributions, permission_plan=permissions, issues=issues,
            runtime_contract=runtime, schema_version=value.get("schema_version", ""),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ExternalExtensionInstallationConflict("intake manifest is invalid") from error


def _review_plan(value: object) -> ExtensionReviewPlan:
    if not isinstance(value, Mapping):
        raise ExternalExtensionInstallationConflict("intake review plan is invalid")
    try:
        return ExtensionReviewPlan(**dict(value))
    except (TypeError, ValueError) as error:
        raise ExternalExtensionInstallationConflict("intake review plan is invalid") from error


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ExternalExtensionInstallationConflict(f"{label} is invalid")
    return value


def _items(value: object, label: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ExternalExtensionInstallationConflict(f"{label} is invalid")
    return tuple(value)


def _canonical(value: Mapping[str, object]) -> dict[str, object]:
    try:
        normalized = json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except (TypeError, ValueError) as error:
        raise ExternalExtensionInstallationError("installation value is not serializable") from error
    if not isinstance(normalized, dict):
        raise ExternalExtensionInstallationError("installation value is invalid")
    return normalized


def _canonical_value(value: object) -> object:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except (TypeError, ValueError) as error:
        raise ExternalExtensionInstallationConflict("installation evidence is not serializable") from error


def _digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _activation_plan(binding: _IntakeBinding) -> ExternalExtensionActivationPlan:
    skill_bindings: list[ExternalSkillActivationBinding] = []
    for contribution in binding.manifest.contributions:
        if contribution.kind != "application_skill":
            continue
        path = contribution.source_path
        layout = "flat_markdown" if path.endswith(".md") and not path.endswith("SKILL.md") else "package_directory"
        metadata = dict(contribution.metadata)
        description = metadata.get("description") or (
            f"Reviewed external extension Skill {contribution.contribution_id}."
        )
        when_to_use = metadata.get("when_to_use") or description
        model_invocable = _metadata_boolean(
            metadata.get("model_invocable"), default=True,
            label="activation Skill model invocation policy",
        )
        user_invocable = _metadata_boolean(
            metadata.get("user_invocable"), default=True,
            label="activation Skill user invocation policy",
        )
        skill_bindings.append(
            ExternalSkillActivationBinding(
                skill_id=_canonical_skill_id(contribution.contribution_id),
                source_path=path,
                package_layout=layout,
                description=description,
                trigger_boundary=when_to_use,
                model_invocable=model_invocable,
                user_invocable=user_invocable,
            )
        )
    return ExternalExtensionActivationPlan(
        root_id=binding.root_id,
        extension_id=binding.manifest.extension_id,
        source_format=binding.manifest.source_format,
        activation_routes=binding.review_plan.activation_routes,
        skill_bindings=tuple(sorted(skill_bindings, key=lambda item: item.skill_id)),
        manifest_identity=_digest(asdict(binding.manifest)),
        review_plan_identity=_digest(asdict(binding.review_plan)),
    )


def _canonical_skill_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if len(normalized) > 64:
        normalized = normalized[:64].rstrip("-")
    if not _SKILL_ID.fullmatch(normalized):
        raise ExternalExtensionInstallationError(
            "external Skill identity cannot be mapped to the Application Skill contract"
        )
    return normalized


def _metadata_boolean(value: str | None, *, default: bool, label: str) -> bool:
    if value is None:
        return default
    if value == "true":
        return True
    if value == "false":
        return False
    raise ExternalExtensionInstallationConflict(f"{label} drifted")


def _require_lifecycle_snapshot(
    snapshot: InstallationSnapshot,
    revision: InstallationRevision,
    action: str,
) -> None:
    if snapshot.root_id != revision.root_id or snapshot.extension_id != revision.extension_id:
        raise ExternalExtensionInstallationConflict(
            "lifecycle state crossed its installation identity"
        )
    if action == "health":
        valid = (
            snapshot.candidate_revision == revision.revision
            and snapshot.candidate_revision_ref == revision.revision_ref
            and snapshot.candidate_status == "installed_disabled"
        )
        message = "health work requires the current disabled candidate"
    elif action == "activation":
        valid = (
            snapshot.candidate_revision == revision.revision
            and snapshot.candidate_revision_ref == revision.revision_ref
            and snapshot.candidate_status == "health_verified_disabled"
        )
        message = "activation work requires the current health-verified candidate"
    elif action in {"disable", "uninstall"}:
        valid = (
            snapshot.active_revision == revision.revision
            and snapshot.active_revision_ref == revision.revision_ref
        )
        message = f"{action} work requires the current active revision"
    elif action == "rollback":
        valid = (
            snapshot.active_revision is not None
            and revision.revision < snapshot.active_revision
        )
        message = "rollback work requires an older health-verified revision"
    else:
        raise ExternalExtensionInstallationError("lifecycle action is invalid")
    if not valid:
        raise ExternalExtensionInstallationError(message)


def _health_identity(plan: ExtensionReviewPlan) -> str:
    return _digest({"review_plan_identity": _digest(asdict(plan)), "health_checks": list(plan.health_checks)})


def _require_equal(actual: Mapping[str, object], expected: Mapping[str, object]) -> None:
    if _canonical(actual) != _canonical(expected):
        raise ExternalExtensionInstallationConflict("installation command or immutable fact drifted")


def _required(record: SQLiteStructuredRecord | None, label: str) -> SQLiteStructuredRecord:
    if record is None:
        raise ExternalExtensionInstallationError(f"{label} is missing")
    return record


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ExternalExtensionInstallationError(f"{label} is invalid")
    return value


def _extension_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,127}", value):
        raise ExternalExtensionInstallationError("extension id is invalid")
    return value


def _root_id(value: object) -> str:
    if not isinstance(value, str) or not _ROOT_ID.fullmatch(value):
        raise ExternalExtensionInstallationError("project root id is invalid")
    return value


def _command_id(value: object) -> str:
    if not isinstance(value, str) or not _COMMAND_ID.fullmatch(value):
        raise ExternalExtensionInstallationError("installation command id is invalid")
    return value


def _reference_id(value: object, prefix: str, label: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ExternalExtensionInstallationError(f"{label} reference is invalid")
    return _id(value[len(prefix):], f"{label} id")


def _installation_key(root_id: str, extension_id: str) -> str:
    root = _root_id(root_id)
    extension = _extension_id(extension_id)
    legacy = f"{root}~{extension}"
    return _compatible_repository_key("i", legacy, root, extension)


def _revision_key(root_id: str, extension_id: str, revision: int) -> str:
    root = _root_id(root_id)
    extension = _extension_id(extension_id)
    number = _revision(revision)
    legacy = f"{root}~{extension}~{number}"
    return _compatible_repository_key("r", legacy, root, extension, number)


def _command_key(root_id: str, extension_id: str, command_id: str) -> str:
    root = _root_id(root_id)
    extension = _extension_id(extension_id)
    command = _command_id(command_id)
    legacy = f"{root}~{extension}~{command}"
    return _compatible_repository_key("c", legacy, root, extension, command)


def _confirmation_key(confirmation_id: str) -> str:
    identity = _id(confirmation_id, "review confirmation id")
    return _compatible_repository_key("f", identity, identity)


def _compatible_repository_key(kind: str, legacy: str, *parts: object) -> str:
    """Preserve every previously writable key; encode only impossible ones.

    The domain accepts project IDs containing ``:`` and IDs wider than one
    SQLite repository segment.  Existing short IDs already use raw keys, so a
    blanket key migration would make persisted state unreachable.  This
    adaptive contract keeps every valid legacy key byte-for-byte and uses a
    versioned, fixed-width locator only when the legacy form could never have
    passed the repository validator.  Original identities remain in the
    payload and are re-verified on every read.
    """

    if _REPOSITORY_SEGMENT.fullmatch(legacy):
        return legacy
    if kind not in {"i", "r", "c", "f"}:
        raise ExternalExtensionInstallationError(
            "installation storage key kind is invalid"
        )
    encoded = json.dumps(
        ["external-extension-installation-key-v2", kind, *parts],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{kind}-" + hashlib.sha256(encoded).hexdigest()


def _revision(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ExternalExtensionInstallationError("revision is invalid")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ExternalExtensionInstallationConflict("artifact digest is invalid")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or any(char in value for char in "\x00\r\n"):
        raise ExternalExtensionInstallationError(f"{label} is invalid")
    return value


def _actor(value: object) -> str:
    if not isinstance(value, str) or not _ACTOR.fullmatch(value):
        raise ExternalExtensionInstallationError("review actor is invalid")
    return value


def _reason(value: object) -> str:
    return _text(value, "review reason")


def _checks(value: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, tuple) or tuple(sorted(set(value))) != value or any(not re.fullmatch(r"[a-z][a-z0-9._-]{1,95}", item) for item in value):
        raise ExternalExtensionInstallationError("health checks are invalid")
    return value


def _bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ExternalExtensionInstallationError(f"{label} is invalid")
    return value


def _status(value: object) -> str:
    if value not in {"installed_disabled", "health_verified_disabled", "active", "disabled"}:
        raise ExternalExtensionInstallationConflict("installation status is invalid")
    return str(value)


def _projection(value: object) -> str:
    if value != "installed_disabled":
        raise ExternalExtensionInstallationConflict("installation projection is invalid")
    return str(value)


def _require_state(snapshot: InstallationSnapshot, expected: int) -> None:
    if snapshot.state_revision != _revision(expected):
        raise ExternalExtensionInstallationConflict("installation state revision is stale")
