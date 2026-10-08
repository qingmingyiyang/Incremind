"""Non-executable durable lifecycle around one contained Plugin Hands attempt.

Records carry only opaque approval references, lease identity, and terminal
classification. Launches, inputs, environments, paths, and outputs never enter
this store. Later composition must obtain a launch from an artifact resolver
and OS runtime catalog, never from this lifecycle.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from core.effect_log import (
    Effect, EffectClass, EffectIntent, EffectLog, EffectPurpose, EffectRunner, EffectState,
    InvalidEffectTransition, backfill_interrupted_effects, shared_effect_runner,
)
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError
from .contained_host import PluginHandsContainedExecution, WindowsContainedPluginHandsHost
from .contracts import PluginHandsControl, PluginHandsInvocation, PluginHandsLaunch, PluginHandsOutcome
from .workspace import PluginHandsWorkspace, PluginHandsWorkspaceError, PluginHandsWorkspaceManager

_COLLECTION = "plugin_hands_lifecycles"
_RECEIPTS = "plugin_hands_outcome_receipts"
_RESULT_PAYLOADS = "plugin_hands_result_payloads"
_CLEANUP_RECEIPTS = "plugin_hands_cleanup_receipts"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,255}$")
_PACKAGE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_STATES = frozenset({"prepared", "pre_fence_cleanup_pending", "fenced", "unknown", "cleanup_pending", "cleaned", "pre_fence_cleaned"})


class PluginHandsDurableLifecycleError(ValueError): pass


@dataclass(frozen=True, slots=True)
class PluginHandsExecutionScope:
    """Read-only durable lease scope exposed to the outer authority adapter."""
    invocation_id: str
    lease_id: str
    generation: int
    project_id: str
    turn_id: str
    boundary_revision: int
    recipe_revision: str
    allowed_resources: tuple[str, ...]
    resource_policy_revision: str
    expires_at: str


class PluginHandsExecutionAuthority(Protocol):
    """Outer composition resolves a current host-owned launch; never this ledger."""
    def resolve(self, binding: "PluginHandsLifecycleBinding", scope: PluginHandsExecutionScope) -> PluginHandsLaunch: ...
    def prepare_workspace(self, binding: "PluginHandsLifecycleBinding", scope: PluginHandsExecutionScope, workspace: PluginHandsWorkspace) -> None: ...
    def verify_workspace(self, binding: "PluginHandsLifecycleBinding", scope: PluginHandsExecutionScope, workspace: PluginHandsWorkspace) -> None: ...
    def validate_outcome(self, binding: "PluginHandsLifecycleBinding", scope: PluginHandsExecutionScope, outcome: PluginHandsOutcome) -> None: ...


@dataclass(frozen=True, slots=True)
class PluginHandsLifecycleBinding:
    intent_ref: str
    capability_id: str
    artifact_opaque_ref: str
    plugin_id: str
    hand_id: str
    package_record_id: str
    review_revision: int
    materialization_revision: int
    activation_revision: int
    containment_profile_revision: str
    launch_recipe_revision: str

    def __post_init__(self) -> None:
        for item in (self.intent_ref, self.capability_id, self.artifact_opaque_ref, self.package_record_id, self.launch_recipe_revision, self.containment_profile_revision):
            if not isinstance(item, str) or not item or len(item) > 256 or "\x00" in item:
                raise PluginHandsDurableLifecycleError("Plugin Hands lifecycle binding is invalid")
        if _PACKAGE_ID.fullmatch(self.plugin_id) is None or _PACKAGE_ID.fullmatch(self.hand_id) is None:
            raise PluginHandsDurableLifecycleError("Plugin Hands lifecycle package identity is invalid")
        for item in (self.review_revision, self.materialization_revision, self.activation_revision):
            if not isinstance(item, int) or isinstance(item, bool) or item < 1:
                raise PluginHandsDurableLifecycleError("Plugin Hands lifecycle binding revision is invalid")


@dataclass(frozen=True, slots=True)
class PluginHandsLifecycleRecord:
    binding: PluginHandsLifecycleBinding
    invocation_id: str
    lease_id: str
    generation: int
    project_id: str
    turn_id: str
    boundary_revision: int
    recipe_revision: str
    allowed_resources: tuple[str, ...]
    resource_policy_revision: str
    expires_at: str
    state: str
    revision: int
    outcome_status: str | None = None
    error_code: str | None = None
    workspace_ref: str | None = None


@dataclass(frozen=True, slots=True)
class PluginHandsLifecycleAuditProjection:
    """Safe, immutable view of one retained Hands attempt.

    This deliberately excludes the workspace reference and every launch detail.
    A projection is an observation only: it neither retries the Hand nor starts
    lifecycle reconciliation.
    """

    attempt_id: str
    plugin_id: str
    hand_id: str
    project_id: str
    turn_id: str
    state: str
    outcome_status: str | None
    error_code: str | None
    revision: int
    updated_at: str
    workspace_retention: str


class PluginHandsLifecycleReader:
    """Read one durable lifecycle record without invoking recovery or execution."""

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        if not isinstance(records, SQLiteStructuredRecordStore):
            raise PluginHandsDurableLifecycleError("Plugin Hands durable store is invalid")
        self._records = records

    def read_attempt(self, attempt_id: str) -> PluginHandsLifecycleAuditProjection | None:
        if not isinstance(attempt_id, str) or _ID.fullmatch(attempt_id) is None:
            raise PluginHandsDurableLifecycleError("Plugin Hands lifecycle attempt identity is invalid")
        try:
            raw = self._records.read(_COLLECTION, attempt_id)
        except SQLiteUnitOfWorkError as error:
            raise PluginHandsDurableLifecycleError("Plugin Hands lifecycle read failed") from error
        if raw is None:
            return None
        record = _decode(raw.payload, raw.revision)
        updated_at = raw.payload.get("updated_at")
        if not isinstance(updated_at, str) or not updated_at or len(updated_at) > 64:
            raise PluginHandsDurableLifecycleError("Plugin Hands durable record is invalid")
        workspace_retention = {
            "unknown": "retained",
            "cleanup_pending": "cleanup_pending",
            "cleaned": "removed_or_not_retained",
            "pre_fence_cleaned": "removed_or_not_retained",
            "prepared": "not_yet_terminal",
            "fenced": "not_yet_terminal",
            "pre_fence_cleanup_pending": "not_yet_terminal",
        }.get(record.state)
        if workspace_retention is None:
            raise PluginHandsDurableLifecycleError("Plugin Hands durable record is invalid")
        return PluginHandsLifecycleAuditProjection(
            attempt_id=record.invocation_id,
            plugin_id=record.binding.plugin_id,
            hand_id=record.binding.hand_id,
            project_id=record.project_id,
            turn_id=record.turn_id,
            state=record.state,
            outcome_status=record.outcome_status,
            error_code=record.error_code,
            revision=record.revision,
            updated_at=updated_at,
            workspace_retention=workspace_retention,
        )


class PluginHandsLifecycleRecovery:
    """Startup-only recovery over the one durable Hands lifecycle collection."""

    def __init__(self, records: SQLiteStructuredRecordStore, *, now: Callable[[], str] | None = None) -> None:
        if not isinstance(records, SQLiteStructuredRecordStore):
            raise PluginHandsDurableLifecycleError("Plugin Hands durable store is invalid")
        self._records, self._now = records, now or _utc_now
        self._effects = EffectLog(records.database_path)

    def reconcile(self, manager: PluginHandsWorkspaceManager) -> tuple[PluginHandsLifecycleRecord, ...]:
        repaired: list[PluginHandsLifecycleRecord] = []
        for raw in self._records.list(_COLLECTION):
            try:
                record = _decode(raw.payload, raw.revision)
                if record.state == "prepared":
                    record = self.claim_pre_fence_cleanup(record)
                if record.state == "pre_fence_cleanup_pending":
                    repaired.append(self.finish_pre_fence_cleanup(manager, record))
                elif record.state == "fenced":
                    repaired.append(self.replace(record, state="unknown", workspace_ref=_ref(record), now=self._now()))
                elif record.state == "cleanup_pending":
                    manager.cleanup_known_identity(record.lease_id, record.invocation_id)
                    repaired.append(self.replace(
                        record, state="cleaned", outcome_status=record.outcome_status,
                        error_code=record.error_code, now=self._now(),
                    ))
            except (PluginHandsDurableLifecycleError, PluginHandsWorkspaceError):
                continue
        return tuple(repaired)

    def reconcile_lazy(self, manager_factory: Callable[[], PluginHandsWorkspaceManager]) -> tuple[PluginHandsLifecycleRecord, ...]:
        """Recover one snapshot and provision a workspace manager only for cleanup."""
        if not callable(manager_factory):
            raise PluginHandsDurableLifecycleError("Plugin Hands workspace manager factory is invalid")
        repaired: list[PluginHandsLifecycleRecord] = []
        manager: PluginHandsWorkspaceManager | None = None
        for raw in self._records.list(_COLLECTION):
            try:
                record = _decode(raw.payload, raw.revision)
                if record.state == "prepared":
                    record = self.claim_pre_fence_cleanup(record)
                if record.state == "pre_fence_cleanup_pending":
                    manager = manager or _workspace_manager(manager_factory)
                    repaired.append(self.finish_pre_fence_cleanup(manager, record))
                elif record.state == "fenced":
                    repaired.append(self.replace(record, state="unknown", workspace_ref=_ref(record), now=self._now()))
                elif record.state == "cleanup_pending":
                    manager = manager or _workspace_manager(manager_factory)
                    manager.cleanup_known_identity(record.lease_id, record.invocation_id)
                    repaired.append(self.replace(
                        record, state="cleaned", outcome_status=record.outcome_status,
                        error_code=record.error_code, now=self._now(),
                    ))
            except (PluginHandsDurableLifecycleError, PluginHandsWorkspaceError):
                continue
        return tuple(repaired)

    def cleanup_only_lazy(
        self, manager_factory: Callable[[], PluginHandsWorkspaceManager],
    ) -> tuple[PluginHandsLifecycleRecord, ...]:
        """Compensate local workspaces without deciding external Effect state."""

        if not callable(manager_factory):
            raise PluginHandsDurableLifecycleError("Plugin Hands workspace manager factory is invalid")
        repaired: list[PluginHandsLifecycleRecord] = []
        manager: PluginHandsWorkspaceManager | None = None
        for raw in self._records.list(_COLLECTION):
            try:
                record = _decode(raw.payload, raw.revision)
                if record.state == "prepared":
                    record = self.claim_pre_fence_cleanup(record)
                if record.state == "pre_fence_cleanup_pending":
                    manager = manager or _workspace_manager(manager_factory)
                    repaired.append(self.finish_pre_fence_cleanup(manager, record))
                elif record.state == "cleanup_pending":
                    manager = manager or _workspace_manager(manager_factory)
                    manager.cleanup_known_identity(record.lease_id, record.invocation_id)
                    repaired.append(self.replace(
                        record, state="cleaned", outcome_status=record.outcome_status,
                        error_code=record.error_code, now=self._now(),
                    ))
            except (PluginHandsDurableLifecycleError, PluginHandsWorkspaceError):
                continue
        return tuple(repaired)

    def claim_pre_fence_cleanup(self, prepared: PluginHandsLifecycleRecord) -> PluginHandsLifecycleRecord:
        if prepared.state != "prepared":
            raise PluginHandsDurableLifecycleError("Plugin Hands pre-fence cleanup claim is invalid")
        return self.replace(prepared, state="pre_fence_cleanup_pending", now=self._now())

    def finish_pre_fence_cleanup(self, manager: PluginHandsWorkspaceManager, claimed: PluginHandsLifecycleRecord) -> PluginHandsLifecycleRecord:
        if claimed.state != "pre_fence_cleanup_pending":
            raise PluginHandsDurableLifecycleError("Plugin Hands pre-fence cleanup claim is invalid")
        try:
            manager.cleanup_pre_fence_identity(claimed.lease_id)
        except PluginHandsWorkspaceError as error:
            raise PluginHandsDurableLifecycleError("Plugin Hands pre-fence cleanup failed") from error
        return self.replace(claimed, state="pre_fence_cleaned", now=self._now())

    def replace(self, record: PluginHandsLifecycleRecord, *, state: str, outcome_status: str | None = None, error_code: str | None = None, workspace_ref: str | None = None, now: str) -> PluginHandsLifecycleRecord:
        try:
            with self._records.begin() as uow:
                saved = uow.put(_COLLECTION, record.invocation_id, _encode(record.binding, record, state, outcome_status, error_code, workspace_ref, now), expected_revision=record.revision)
                uow.commit()
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
            raise PluginHandsDurableLifecycleError("Plugin Hands lifecycle CAS failed") from error
        return _decode(saved.payload, saved.revision)

class PluginHandsDurableLifecycle:
    """CAS lifecycle that never retries Plugin code or attaches to a PID."""
    def __init__(self, records: SQLiteStructuredRecordStore, authority: PluginHandsExecutionAuthority, *, now: Callable[[], str] | None = None, effect_runner: EffectRunner | None = None) -> None:
        if not isinstance(records, SQLiteStructuredRecordStore): raise PluginHandsDurableLifecycleError("Plugin Hands durable store is invalid")
        if not all(hasattr(authority, name) for name in ("resolve", "prepare_workspace", "verify_workspace", "validate_outcome")):
            raise PluginHandsDurableLifecycleError("Plugin Hands execution authority is invalid")
        self._records, self._authority, self._now = records, authority, now or _utc_now
        self._effects = EffectLog(records.database_path)
        self._runner = effect_runner or shared_effect_runner(
            records.database_path, owner_role="plugin-hands-effect-runner", lease_seconds=300,
        )
        self._recovery = PluginHandsLifecycleRecovery(records, now=self._now)

    def prepare(self, binding: PluginHandsLifecycleBinding, invocation: PluginHandsInvocation) -> PluginHandsLifecycleRecord:
        _match(binding, invocation); _live(invocation.lease.expires_at, self._now)
        try:
            with self._records.begin() as uow:
                old = uow.read(_COLLECTION, invocation.invocation_id)
                if old is not None:
                    record = _decode(old.payload, old.revision)
                    if not _same(record, binding, invocation): raise PluginHandsDurableLifecycleError("Plugin Hands lifecycle binding drift")
                    uow.rollback(); return record
                saved = uow.put(_COLLECTION, invocation.invocation_id, _encode(binding, invocation, "prepared", now=self._now()), expected_revision=0)
                uow.commit()
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
            raise PluginHandsDurableLifecycleError("Plugin Hands durable prepare failed") from error
        record = _decode(saved.payload, saved.revision)
        _ensure_effect(self._effects, record, now=_epoch(self._now()))
        return record

    def fence(self, invocation_id: str, *, expected_revision: int) -> PluginHandsLifecycleRecord:
        record = self._require(invocation_id, "prepared", expected_revision)
        _live(record.expires_at, self._now)
        effect = _ensure_effect(self._effects, record, now=_epoch(self._now()))
        if effect.state is not EffectState.PLANNED:
            raise PluginHandsDurableLifecycleError("Plugin Hands Effect is not executable")
        claimed = self._runner.begin_planned(
            effect.operation_id, now=_epoch(self._now()),
            lease_expires_at=_epoch(record.expires_at),
        )
        if claimed.state is not EffectState.INFLIGHT or claimed.lease_owner != self._runner.owner_id:
            raise PluginHandsDurableLifecycleError("Plugin Hands Effect claim failed")
        return self._replace(record, state="fenced", now=self._now())

    def record_outcome(self, invocation_id: str, outcome: PluginHandsOutcome, *, expected_revision: int) -> PluginHandsLifecycleRecord:
        saved = self._record_outcome_fact(
            invocation_id, outcome, expected_revision=expected_revision,
        )
        effect = self._effects.get(_effect_operation_id(saved.invocation_id))
        observed_at = _epoch(self._now())
        if outcome.status == "success":
            self._runner.settle_ok(
                effect,
                receipt_ref=_outcome_receipt_ref(saved), receipt_kind="plugin-hands-outcome-receipt",
                now=observed_at,
            )
        elif outcome.status == "unknown":
            self._runner.mark_unknown(
                effect, now=observed_at,
                error_ref=outcome.error_code or "plugin-hands.unknown",
            )
        else:
            self._runner.settle_error(
                effect, now=observed_at,
                error_ref=outcome.error_code or f"plugin-hands.{outcome.status}",
            )
        return saved

    def _record_outcome_fact(self, invocation_id: str, outcome: PluginHandsOutcome, *, expected_revision: int) -> PluginHandsLifecycleRecord:
        record = self._require(invocation_id, "fenced", expected_revision)
        if (outcome.invocation_id, outcome.lease_id) != (record.invocation_id, record.lease_id): raise PluginHandsDurableLifecycleError("Plugin Hands outcome binding drift")
        state = "unknown" if outcome.status == "unknown" else "cleanup_pending"
        observed = self._now()
        try:
            with self._records.begin() as uow:
                current = uow.read(_COLLECTION, invocation_id)
                if current is None or current.revision != expected_revision or current.payload.get("state") != "fenced":
                    raise PluginHandsDurableLifecycleError("Plugin Hands lifecycle transition is invalid")
                result_payload_ref = None
                if outcome.status == "success":
                    result_payload = uow.put(
                        _RESULT_PAYLOADS,
                        invocation_id,
                        {
                            "schema_version": "1.0.0",
                            "invocation_id": invocation_id,
                            "result": dict(outcome.output or {}),
                            "recorded_at": observed,
                        },
                        expected_revision=0,
                    )
                    if result_payload.revision != 1:
                        raise PluginHandsDurableLifecycleError(
                            "Plugin Hands result payload is not immutable"
                        )
                    result_payload_ref = (
                        f"plugin-hands-result:{invocation_id}:r{result_payload.revision}"
                    )
                receipt = uow.put(
                    _RECEIPTS, invocation_id,
                    _encode_outcome_receipt(
                        record, outcome, recorded_at=observed,
                        result_payload_ref=result_payload_ref,
                    ),
                    expected_revision=0,
                )
                projected = uow.put(
                    _COLLECTION, invocation_id,
                    _encode(
                        record.binding, record, state, outcome.status, outcome.error_code,
                        _ref(record), observed,
                    ),
                    expected_revision=expected_revision,
                )
                uow.commit()
        except PluginHandsDurableLifecycleError:
            raise
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
            raise PluginHandsDurableLifecycleError("Plugin Hands outcome fact commit failed") from error
        if receipt.revision != 1:
            raise PluginHandsDurableLifecycleError("Plugin Hands outcome receipt is not immutable")
        saved = _decode(projected.payload, projected.revision)
        return saved

    def mark_cleaned(self, invocation_id: str, *, expected_revision: int) -> PluginHandsLifecycleRecord:
        record = self._require(invocation_id, "cleanup_pending", expected_revision)
        return self._replace(record, state="cleaned", outcome_status=record.outcome_status, error_code=record.error_code, now=self._now())

    def retry_cleanup(self, manager: PluginHandsWorkspaceManager, invocation_id: str, *, expected_revision: int) -> PluginHandsLifecycleRecord:
        record = self._require(invocation_id, "cleanup_pending", expected_revision)
        try: manager.cleanup_known_identity(record.lease_id, record.invocation_id)
        except PluginHandsWorkspaceError as error: raise PluginHandsDurableLifecycleError("Plugin Hands workspace cleanup failed") from error
        return self.mark_cleaned(invocation_id, expected_revision=record.revision)

    def reconcile(self, manager: PluginHandsWorkspaceManager) -> tuple[PluginHandsLifecycleRecord, ...]:
        return self._recovery.reconcile(manager)

    def reconcile_lazy(self, manager_factory: Callable[[], PluginHandsWorkspaceManager]) -> tuple[PluginHandsLifecycleRecord, ...]:
        return self._recovery.reconcile_lazy(manager_factory)

    def cleanup_only_lazy(self, manager_factory: Callable[[], PluginHandsWorkspaceManager]) -> tuple[PluginHandsLifecycleRecord, ...]:
        return self._recovery.cleanup_only_lazy(manager_factory)

    def execute(self, host: WindowsContainedPluginHandsHost, manager: PluginHandsWorkspaceManager, binding: PluginHandsLifecycleBinding, invocation: PluginHandsInvocation, control: PluginHandsControl = PluginHandsControl()) -> PluginHandsContainedExecution:
        if not isinstance(host, WindowsContainedPluginHandsHost): raise PluginHandsDurableLifecycleError("Plugin Hands durable execution is invalid")
        prepared = self.prepare(binding, invocation)
        if prepared.state != "prepared": raise PluginHandsDurableLifecycleError("Plugin Hands command replay is not executable")
        try:
            self._runner.execute(
                _effect_intent(prepared),
                lambda effect: self.handle_claimed(
                    effect, host, manager, binding, invocation, control,
                ),
                now=_epoch(self._now()),
            )
        except PluginHandsDurableLifecycleError as error:
            try:
                outcome = load_plugin_hands_outcome_fact(
                    self._records, invocation.invocation_id,
                )
            except PluginHandsDurableLifecycleError:
                raise error
        else:
            outcome = load_plugin_hands_outcome_fact(
                self._records, invocation.invocation_id,
            )
        record = self.load(invocation.invocation_id)
        retained = record.workspace_ref if record is not None else None
        return PluginHandsContainedExecution(outcome, retained)

    def handle_claimed(
        self,
        effect: Effect,
        host: WindowsContainedPluginHandsHost,
        manager: PluginHandsWorkspaceManager,
        binding: PluginHandsLifecycleBinding,
        invocation: PluginHandsInvocation,
        control: PluginHandsControl = PluginHandsControl(),
    ) -> str:
        """Execute one Core-claimed attempt; safe for a Registry Handler."""

        prepared = self._require(invocation.invocation_id, "prepared", 1)
        if prepared.binding != binding or not _same(prepared, binding, invocation):
            raise PluginHandsDurableLifecycleError("Plugin Hands execution facts drifted")
        if (
            effect.operation_id != _effect_operation_id(prepared.invocation_id)
            or effect.state is not EffectState.INFLIGHT
            or effect.lease_owner != self._runner.owner_id
        ):
            raise PluginHandsDurableLifecycleError("Plugin Hands Core Effect claim drifted")
        try:
            launch = self._resolve(binding, prepared)
            workspace = manager.create(invocation.lease)
            self._prepare_workspace(binding, prepared, workspace)
            if self._resolve(binding, prepared) != launch:
                raise PluginHandsDurableLifecycleError("Plugin Hands execution authority drift")
            fenced = self._replace(prepared, state="fenced", now=self._now())
        except PluginHandsDurableLifecycleError:
            self._cleanup_if_still_pre_fence(manager, prepared)
            raise
        except PluginHandsWorkspaceError as error:
            raise PluginHandsDurableLifecycleError("Plugin Hands workspace creation failed") from error
        try:
            self._verify_workspace(binding, fenced, workspace)
            if self._resolve(binding, fenced) != launch:
                raise PluginHandsDurableLifecycleError("Plugin Hands execution authority drift")
        except PluginHandsDurableLifecycleError:
            outcome = PluginHandsOutcome(
                invocation.invocation_id, invocation.lease.lease_id, "unknown",
                error_code="authority-staging-drift",
            )
            self._record_outcome_fact(
                invocation.invocation_id, outcome, expected_revision=fenced.revision,
            )
            raise
        outcome = host.execute_prepared(workspace, launch, invocation, control)
        self._validate_outcome(binding, fenced, outcome)
        recorded = self._record_outcome_fact(
            invocation.invocation_id, outcome, expected_revision=fenced.revision,
        )
        if recorded.state == "unknown":
            raise PluginHandsDurableLifecycleError("Plugin Hands execution outcome is unknown")
        cleanup_intent = _workspace_cleanup_intent(
            recorded, parent_id=effect.operation_id,
        )
        try:
            self._runner.execute(
                cleanup_intent,
                lambda cleanup_effect: execute_plugin_hands_workspace_cleanup(
                    self._records, manager, cleanup_effect,
                ),
                now=_epoch(self._now()),
            )
        except Exception:
            pass
        if outcome.status != "success":
            raise PluginHandsDurableLifecycleError("Plugin Hands contained execution failed")
        return _outcome_receipt_ref(recorded)

    def _resolve(self, binding: PluginHandsLifecycleBinding, record: PluginHandsLifecycleRecord) -> PluginHandsLaunch:
        try:
            launch = self._authority.resolve(binding, _scope(record))
        except Exception as error:
            raise PluginHandsDurableLifecycleError("Plugin Hands execution authority drift") from error
        if not isinstance(launch, PluginHandsLaunch):
            raise PluginHandsDurableLifecycleError("Plugin Hands execution authority drift")
        return launch

    def _prepare_workspace(self, binding: PluginHandsLifecycleBinding, record: PluginHandsLifecycleRecord, workspace: PluginHandsWorkspace) -> None:
        try:
            result = self._authority.prepare_workspace(binding, _scope(record), workspace)
        except Exception as error:
            raise PluginHandsDurableLifecycleError("Plugin Hands workspace preparation drift") from error
        if result is not None:
            raise PluginHandsDurableLifecycleError("Plugin Hands workspace preparation drift")

    def _verify_workspace(self, binding: PluginHandsLifecycleBinding, record: PluginHandsLifecycleRecord, workspace: PluginHandsWorkspace) -> None:
        try:
            result = self._authority.verify_workspace(binding, _scope(record), workspace)
        except Exception as error:
            raise PluginHandsDurableLifecycleError("Plugin Hands workspace verification drift") from error
        if result is not None:
            raise PluginHandsDurableLifecycleError("Plugin Hands workspace verification drift")

    def _validate_outcome(self, binding: PluginHandsLifecycleBinding, record: PluginHandsLifecycleRecord, outcome: PluginHandsOutcome) -> None:
        try:
            result = self._authority.validate_outcome(binding, _scope(record), outcome)
        except Exception as error:
            raise PluginHandsDurableLifecycleError("Plugin Hands outcome authority drift") from error
        if result is not None:
            raise PluginHandsDurableLifecycleError("Plugin Hands outcome authority drift")

    def _cleanup_if_still_pre_fence(self, manager: PluginHandsWorkspaceManager, prepared: PluginHandsLifecycleRecord) -> None:
        current = self.load(prepared.invocation_id)
        if current is None or current.state != "prepared" or current.revision != prepared.revision or current.binding != prepared.binding:
            return
        try:
            claimed = self._claim_pre_fence_cleanup(current)
        except PluginHandsDurableLifecycleError:
            return
        self._finish_pre_fence_cleanup(manager, claimed)

    def _claim_pre_fence_cleanup(self, prepared: PluginHandsLifecycleRecord) -> PluginHandsLifecycleRecord:
        if prepared.state != "prepared":
            raise PluginHandsDurableLifecycleError("Plugin Hands pre-fence cleanup claim is invalid")
        return self._replace(prepared, state="pre_fence_cleanup_pending", now=self._now())

    def _finish_pre_fence_cleanup(self, manager: PluginHandsWorkspaceManager, claimed: PluginHandsLifecycleRecord) -> PluginHandsLifecycleRecord:
        if claimed.state != "pre_fence_cleanup_pending":
            raise PluginHandsDurableLifecycleError("Plugin Hands pre-fence cleanup claim is invalid")
        try:
            manager.cleanup_pre_fence_identity(claimed.lease_id)
        except PluginHandsWorkspaceError as error:
            raise PluginHandsDurableLifecycleError("Plugin Hands pre-fence cleanup failed") from error
        return self._replace(claimed, state="pre_fence_cleaned", now=self._now())

    def load(self, invocation_id: str) -> PluginHandsLifecycleRecord | None:
        try: raw = self._records.read(_COLLECTION, invocation_id)
        except SQLiteUnitOfWorkError as error: raise PluginHandsDurableLifecycleError("Plugin Hands lifecycle read failed") from error
        return _decode(raw.payload, raw.revision) if raw is not None else None

    def _require(self, invocation_id: str, state: str, revision: int) -> PluginHandsLifecycleRecord:
        record = self.load(invocation_id)
        if record is None or record.state != state or record.revision != revision: raise PluginHandsDurableLifecycleError("Plugin Hands lifecycle transition is invalid")
        return record

    def _replace(self, record: PluginHandsLifecycleRecord, *, state: str, outcome_status: str | None = None, error_code: str | None = None, workspace_ref: str | None = None, now: str) -> PluginHandsLifecycleRecord:
        return self._recovery.replace(
            record, state=state, outcome_status=outcome_status, error_code=error_code,
            workspace_ref=workspace_ref, now=now,
        )


def _encode(binding: PluginHandsLifecycleBinding, source: PluginHandsInvocation | PluginHandsLifecycleRecord, state: str, outcome_status: str | None = None, error_code: str | None = None, workspace_ref: str | None = None, now: str = "") -> dict[str, object]:
    lease = source.lease if isinstance(source, PluginHandsInvocation) else source
    return {"binding": {key: getattr(binding, key) for key in PluginHandsLifecycleBinding.__dataclass_fields__}, "lease": {"invocation_id": lease.invocation_id, "lease_id": lease.lease_id, "generation": lease.generation, "project_id": lease.project_id, "turn_id": lease.turn_id, "boundary_revision": lease.boundary_revision, "recipe_revision": lease.recipe_revision, "allowed_resources": list(lease.allowed_resources), "resource_policy_revision": lease.resource_policy_revision, "expires_at": lease.expires_at}, "state": state, "outcome_status": outcome_status, "error_code": error_code, "workspace_ref": workspace_ref, "updated_at": now}


def _decode(payload: Mapping[str, object], revision: int) -> PluginHandsLifecycleRecord:
    try:
        binding = PluginHandsLifecycleBinding(**dict(_mapping(payload["binding"])))
        lease = _mapping(payload["lease"]); invocation_id, lease_id, generation, project_id, turn_id, boundary_revision, recipe_revision, resources, resource_policy_revision, expiry = lease["invocation_id"], lease["lease_id"], lease["generation"], lease["project_id"], lease["turn_id"], lease["boundary_revision"], lease["recipe_revision"], lease["allowed_resources"], lease.get("resource_policy_revision", "legacy-unbounded-v0"), lease["expires_at"]
        state, status, code, ref = payload["state"], payload.get("outcome_status"), payload.get("error_code"), payload.get("workspace_ref")
        if state not in _STATES or not isinstance(invocation_id, str) or not isinstance(lease_id, str) or _ID.fullmatch(invocation_id) is None or _ID.fullmatch(lease_id) is None or not isinstance(generation, int) or isinstance(generation, bool) or generation < 1 or not isinstance(project_id, str) or not isinstance(turn_id, str) or not isinstance(boundary_revision, int) or isinstance(boundary_revision, bool) or boundary_revision < 1 or not isinstance(recipe_revision, str) or not isinstance(resources, list) or not all(isinstance(item, str) for item in resources) or not isinstance(resource_policy_revision, str) or not resource_policy_revision or not isinstance(expiry, str): raise ValueError
        if status is not None and status not in {"success", "failed", "cancelled", "unknown"}: raise ValueError
        if code is not None and not isinstance(code, str): raise ValueError
        expected = f"plugin-hands-workspace:{lease_id}:{generation}"
        if ref is not None and ref != expected: raise ValueError
        if state == "unknown" and ref != expected: raise ValueError
        if state in {"cleanup_pending", "cleaned"} and status not in {"success", "failed", "cancelled"}: raise ValueError
        return PluginHandsLifecycleRecord(binding, invocation_id, lease_id, generation, project_id, turn_id, boundary_revision, recipe_revision, tuple(resources), resource_policy_revision, expiry, state, revision, status, code, ref)
    except (KeyError, TypeError, ValueError) as error: raise PluginHandsDurableLifecycleError("Plugin Hands durable record is invalid") from error


def _match(binding: PluginHandsLifecycleBinding, invocation: PluginHandsInvocation) -> None:
    if not isinstance(binding, PluginHandsLifecycleBinding) or not isinstance(invocation, PluginHandsInvocation) or binding.plugin_id != invocation.plugin_id or binding.launch_recipe_revision != invocation.lease.recipe_revision: raise PluginHandsDurableLifecycleError("Plugin Hands lifecycle binding drift")

def _same(record: PluginHandsLifecycleRecord, binding: PluginHandsLifecycleBinding, invocation: PluginHandsInvocation) -> bool:
    return record.binding == binding and (record.invocation_id, record.lease_id, record.generation, record.project_id, record.turn_id, record.boundary_revision, record.recipe_revision, record.allowed_resources, record.resource_policy_revision, record.expires_at) == (invocation.invocation_id, invocation.lease.lease_id, invocation.lease.generation, invocation.lease.project_id, invocation.lease.turn_id, invocation.lease.boundary_revision, invocation.lease.recipe_revision, invocation.lease.allowed_resources, invocation.lease.resource_policy_revision, invocation.lease.expires_at)

def _ref(record: PluginHandsLifecycleRecord) -> str: return f"plugin-hands-workspace:{record.lease_id}:{record.generation}"


def _workspace_manager(factory: Callable[[], PluginHandsWorkspaceManager]) -> PluginHandsWorkspaceManager:
    manager = factory()
    if not isinstance(manager, PluginHandsWorkspaceManager):
        raise PluginHandsDurableLifecycleError("Plugin Hands workspace manager factory is invalid")
    return manager
def _scope(record: PluginHandsLifecycleRecord) -> PluginHandsExecutionScope:
    return PluginHandsExecutionScope(record.invocation_id, record.lease_id, record.generation, record.project_id, record.turn_id, record.boundary_revision, record.recipe_revision, record.allowed_resources, record.resource_policy_revision, record.expires_at)
def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping): raise ValueError
    return value
def _live(expiry: str, now: Callable[[], str]) -> None:
    try: expired_at, current = datetime.fromisoformat(expiry[:-1] + "+00:00"), datetime.fromisoformat(now()[:-1] + "+00:00")
    except (TypeError, ValueError) as error: raise PluginHandsDurableLifecycleError("Plugin Hands lease expiry is invalid") from error
    if expired_at <= current: raise PluginHandsDurableLifecycleError("Plugin Hands lease is expired")
def _utc_now() -> str: return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _epoch(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except (TypeError, ValueError) as error:
        raise PluginHandsDurableLifecycleError("Plugin Hands Effect time is invalid") from error
    if parsed.tzinfo is None:
        raise PluginHandsDurableLifecycleError("Plugin Hands Effect time is invalid")
    return int(parsed.astimezone(timezone.utc).timestamp())


def _effect_operation_id(invocation_id: str) -> str:
    return f"plugin-hands-effect-{invocation_id}"


def _effect_lease_owner(record: PluginHandsLifecycleRecord) -> str:
    return f"plugin-hands:{record.lease_id}:{record.generation}"


def _outcome_receipt_ref(record: PluginHandsLifecycleRecord) -> str:
    return f"plugin-hands-outcome:{record.invocation_id}:r1"


def _encode_outcome_receipt(
    record: PluginHandsLifecycleRecord,
    outcome: PluginHandsOutcome,
    *,
    recorded_at: str,
    result_payload_ref: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "invocation_id": record.invocation_id,
        "lease_id": record.lease_id,
        "effect_operation_id": _effect_operation_id(record.invocation_id),
        "status": outcome.status,
        "error_code": outcome.error_code,
        "result_payload_ref": result_payload_ref,
        "binding": {
            "capability_id": record.binding.capability_id,
            "plugin_id": record.binding.plugin_id,
            "hand_id": record.binding.hand_id,
            "artifact_opaque_ref": record.binding.artifact_opaque_ref,
            "activation_revision": record.binding.activation_revision,
        },
        "recorded_at": recorded_at,
    }


def _decode_outcome_receipt(
    payload: Mapping[str, object], *, effect: Effect,
) -> tuple[EffectState, str | None]:
    required = {
        "schema_version", "invocation_id", "lease_id", "effect_operation_id",
        "status", "error_code", "recorded_at",
    }
    allowed = required | {"result_payload_ref", "binding"}
    invocation_id = payload.get("invocation_id")
    status, error_code = payload.get("status"), payload.get("error_code")
    if (
        not required <= set(payload) <= allowed
        or payload.get("schema_version") != "1.0.0"
        or invocation_id != effect.root_id
        or payload.get("effect_operation_id") != effect.operation_id
        or not isinstance(payload.get("lease_id"), str)
        or status not in {"success", "failed", "cancelled", "unknown"}
        or (error_code is not None and not isinstance(error_code, str))
        or not isinstance(payload.get("recorded_at"), str)
        or (
            payload.get("result_payload_ref") is not None
            and payload.get("result_payload_ref")
            != f"plugin-hands-result:{invocation_id}:r1"
        )
    ):
        raise PluginHandsDurableLifecycleError("Plugin Hands outcome receipt is invalid")
    if status == "success":
        return EffectState.SETTLED_OK, f"plugin-hands-outcome:{invocation_id}:r1"
    if status in {"failed", "cancelled"}:
        return EffectState.SETTLED_ERR, error_code or f"plugin-hands.{status}"
    return EffectState.UNKNOWN, error_code or "plugin-hands.execution-unconfirmed"


def load_plugin_hands_outcome_fact(
    records: SQLiteStructuredRecordStore, invocation_id: str,
) -> PluginHandsOutcome:
    """Rebuild a terminal outcome from immutable Receipt and result facts."""

    if not isinstance(records, SQLiteStructuredRecordStore) or not isinstance(invocation_id, str):
        raise PluginHandsDurableLifecycleError("Plugin Hands outcome identity is invalid")
    receipt = records.read(_RECEIPTS, invocation_id)
    if receipt is None or receipt.revision != 1:
        raise PluginHandsDurableLifecycleError("Plugin Hands outcome receipt is unavailable")
    payload = receipt.payload
    status = payload.get("status")
    lease_id = payload.get("lease_id")
    error_code = payload.get("error_code")
    if (
        payload.get("invocation_id") != invocation_id
        or not isinstance(lease_id, str)
        or status not in {"success", "failed", "cancelled", "unknown"}
    ):
        raise PluginHandsDurableLifecycleError("Plugin Hands outcome receipt is invalid")
    if status != "success":
        return PluginHandsOutcome(invocation_id, lease_id, status, error_code=error_code)
    result_ref = payload.get("result_payload_ref")
    result = records.read(_RESULT_PAYLOADS, invocation_id)
    if (
        result_ref != f"plugin-hands-result:{invocation_id}:r1"
        or result is None
        or result.revision != 1
        or result.payload.get("schema_version") != "1.0.0"
        or result.payload.get("invocation_id") != invocation_id
        or not isinstance(result.payload.get("result"), Mapping)
    ):
        raise PluginHandsDurableLifecycleError("Plugin Hands result payload is unavailable")
    return PluginHandsOutcome(
        invocation_id, lease_id, "success", dict(result.payload["result"]),
    )


def _effect_intent(record: PluginHandsLifecycleRecord) -> EffectIntent:
    return EffectIntent(
        session_id=record.project_id,
        turn_id=record.turn_id,
        root_id=record.invocation_id,
        parent_id=record.invocation_id,
        step_key=f"plugin-hands:{record.binding.plugin_id}:{record.binding.hand_id}",
        kind="plugin_hands_execution",
        effect_class=EffectClass.AT_MOST_ONCE,
        purpose=EffectPurpose.PRIMARY,
        intent_ref=record.binding.intent_ref,
        gate_decision_id=f"plugin-hands-boundary:{record.boundary_revision}",
        rev_set={
            "boundary_revision": record.boundary_revision,
            "recipe_revision": record.recipe_revision,
            "review_revision": record.binding.review_revision,
            "materialization_revision": record.binding.materialization_revision,
            "activation_revision": record.binding.activation_revision,
            "containment_profile_revision": record.binding.containment_profile_revision,
            "resource_policy_revision": record.resource_policy_revision,
        },
        payload={
            "invocation_id": record.invocation_id,
            "project_id": record.project_id,
            "plugin_id": record.binding.plugin_id,
            "hand_id": record.binding.hand_id,
            "capability_id": record.binding.capability_id,
            "artifact_opaque_ref": record.binding.artifact_opaque_ref,
            "package_record_id": record.binding.package_record_id,
            "lease_id": record.lease_id,
            "lease_generation": record.generation,
        },
        operation_id_override=_effect_operation_id(record.invocation_id),
    )


def _workspace_cleanup_intent(
    record: PluginHandsLifecycleRecord, *, parent_id: str,
) -> EffectIntent:
    return EffectIntent(
        session_id=record.project_id,
        turn_id=record.turn_id,
        root_id=record.invocation_id,
        parent_id=parent_id,
        step_key=f"plugin-hands-cleanup:{record.binding.plugin_id}:{record.binding.hand_id}",
        kind="plugin_hands_workspace_cleanup",
        effect_class=EffectClass.IDEMPOTENT,
        purpose=EffectPurpose.AUX,
        intent_ref=record.binding.intent_ref,
        gate_decision_id=f"plugin-hands-boundary:{record.boundary_revision}",
        rev_set={
            "boundary_revision": record.boundary_revision,
            "recipe_revision": record.recipe_revision,
            "activation_revision": record.binding.activation_revision,
            "resource_policy_revision": record.resource_policy_revision,
        },
        payload={
            "invocation_id": record.invocation_id,
            "lease_id": record.lease_id,
            "parent_operation_id": parent_id,
        },
        operation_id_override=f"plugin-hands-cleanup-{record.invocation_id}",
    )


def execute_plugin_hands_workspace_cleanup(
    records: SQLiteStructuredRecordStore,
    manager: PluginHandsWorkspaceManager,
    effect: Effect,
) -> str:
    """Idempotent child Handler; Core owns scheduling and retries."""

    if (
        not isinstance(records, SQLiteStructuredRecordStore)
        or not isinstance(manager, PluginHandsWorkspaceManager)
        or not isinstance(effect, Effect)
        or effect.kind != "plugin_hands_workspace_cleanup"
        or effect.effect_class is not EffectClass.IDEMPOTENT
        or effect.parent_id != _effect_operation_id(effect.root_id)
        or effect.operation_id != f"plugin-hands-cleanup-{effect.root_id}"
    ):
        raise PluginHandsDurableLifecycleError("Plugin Hands cleanup Effect is invalid")
    lifecycle = records.read(_COLLECTION, effect.root_id)
    outcome = records.read(_RECEIPTS, effect.root_id)
    existing = records.read(_CLEANUP_RECEIPTS, effect.root_id)
    if existing is not None:
        if existing.revision != 1 or existing.payload.get("effect_operation_id") != effect.operation_id:
            raise PluginHandsDurableLifecycleError("Plugin Hands cleanup receipt drifted")
        return f"plugin-hands-cleanup:{effect.root_id}:r1"
    if lifecycle is None or outcome is None or outcome.revision != 1:
        raise PluginHandsDurableLifecycleError("Plugin Hands cleanup authority is unavailable")
    record = _decode(lifecycle.payload, lifecycle.revision)
    status = outcome.payload.get("status")
    if (
        status not in {"success", "failed", "cancelled"}
        or record.state != "cleanup_pending"
        or record.invocation_id != effect.root_id
    ):
        raise PluginHandsDurableLifecycleError("Plugin Hands cleanup authority drifted")
    manager.cleanup_known_identity(record.lease_id, record.invocation_id)
    now = _utc_now()
    try:
        with records.begin() as uow:
            current = uow.read(_COLLECTION, record.invocation_id)
            if current is None or current.revision != record.revision or current.payload.get("state") != "cleanup_pending":
                raise PluginHandsDurableLifecycleError("Plugin Hands cleanup projection drifted")
            receipt = uow.put(
                _CLEANUP_RECEIPTS, record.invocation_id,
                {
                    "schema_version": "1.0.0",
                    "invocation_id": record.invocation_id,
                    "effect_operation_id": effect.operation_id,
                    "parent_operation_id": effect.parent_id,
                    "disposition": "removed",
                    "recorded_at": now,
                },
                expected_revision=0,
            )
            uow.put(
                _COLLECTION, record.invocation_id,
                _encode(
                    record.binding, record, "cleaned", record.outcome_status,
                    record.error_code, None, now,
                ),
                expected_revision=record.revision,
            )
            uow.commit()
    except PluginHandsDurableLifecycleError:
        raise
    except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
        raise PluginHandsDurableLifecycleError("Plugin Hands cleanup receipt commit failed") from error
    if receipt.revision != 1:
        raise PluginHandsDurableLifecycleError("Plugin Hands cleanup receipt is not immutable")
    return f"plugin-hands-cleanup:{record.invocation_id}:r1"


def _ensure_effect(log: EffectLog, record: PluginHandsLifecycleRecord, *, now: int):
    effect, _created = log.plan(_effect_intent(record), now=now)
    return effect


def verify_plugin_hands_effect(
    records: SQLiteStructuredRecordStore, effect: Effect,
) -> tuple[EffectState, str | None]:
    """Read-only domain verification used only by the Core Reaper scheduler."""

    if not isinstance(records, SQLiteStructuredRecordStore):
        raise PluginHandsDurableLifecycleError("Plugin Hands verifier store is invalid")
    if not isinstance(effect, Effect) or effect.kind != "plugin_hands_execution":
        raise PluginHandsDurableLifecycleError("Plugin Hands verifier Effect is invalid")
    invocation_id = effect.root_id
    if effect.operation_id != _effect_operation_id(invocation_id):
        raise PluginHandsDurableLifecycleError("Plugin Hands verifier identity drift")
    try:
        raw = records.read(_RECEIPTS, invocation_id)
    except SQLiteUnitOfWorkError as error:
        raise PluginHandsDurableLifecycleError("Plugin Hands verifier read failed") from error
    if raw is None:
        return EffectState.UNKNOWN, "plugin-hands.outcome-receipt-missing"
    if raw.revision != 1:
        raise PluginHandsDurableLifecycleError("Plugin Hands outcome receipt is not immutable")
    return _decode_outcome_receipt(raw.payload, effect=effect)


def backfill_plugin_hands_execution_effects(
    records: SQLiteStructuredRecordStore,
    effects: EffectLog,
    *,
    now: int,
) -> tuple[str, ...]:
    """Create intent-only legacy Effects; never repair or execute a Hand."""
    operation_ids: list[str] = []
    interrupted: list[EffectIntent] = []
    for raw in records.list(_COLLECTION):
        record = _decode(raw.payload, raw.revision)
        intent = _effect_intent(record)
        operation_ids.append(intent.operation_id)
        if record.state in {"prepared", "pre_fence_cleanup_pending", "pre_fence_cleaned"}:
            effects.plan(intent, now=now)
        else:
            interrupted.append(intent)
    backfill_interrupted_effects(
        effects, interrupted, now=now,
        lease_owner="legacy-plugin-hands-execution",
    )
    return tuple(operation_ids)
