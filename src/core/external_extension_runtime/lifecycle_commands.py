"""Server-owned commands for external-extension lifecycle Effects.

The command record is deliberately only a durable coordination projection.  The
Core Effect and its terminal Receipt remain the execution authority on every
load, including after a process crash.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass

from blake3 import blake3

from core.effect_log import (
    NOT_APPLICABLE,
    V2_REVISION_KEYS,
    Effect,
    EffectRuntime,
    EffectState,
)
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict

from .installation import (
    ExternalExtensionInstallationConflict,
    ExternalExtensionInstallationError,
    ExternalExtensionInstallationStore,
    InstallationSnapshot,
    LIFECYCLE_RESERVATIONS_COLLECTION,
)
from .gate_authority import (
    ExternalExtensionGateAuthorization,
    ExternalExtensionGateRequest,
)
from .terminal_receipts import (
    ExternalExtensionLifecycleIntent,
    ExternalExtensionTerminalReceiptStore,
    build_lifecycle_effect_intent,
)


_COMMANDS = "external_extension_lifecycle_commands"
_INTENTS = "external_extension_lifecycle_intents"
_RESERVATIONS = LIFECYCLE_RESERVATIONS_COLLECTION
_COORDINATION = "external_extension_lifecycle_coordination"
_TERMINAL_CURSOR = "terminal-projection-scan-v1"
_BACKFILL_CURSOR = "intent-only-backfill-scan-v1"
_ACTIONS = frozenset(("health", "activation", "disable", "rollback", "uninstall"))
_POLICY = "external-extension-lifecycle-policy-v1"
_BOUNDARY = "external-extension-lifecycle-boundary-v1"
_HANDLER = "external-extension-lifecycle-handler-v1"
_LOG = logging.getLogger(__name__)


class ExternalExtensionLifecycleCommandError(ValueError):
    """Raised for malformed or not-current lifecycle commands."""


class ExternalExtensionLifecycleCommandConflict(ExternalExtensionLifecycleCommandError):
    """Raised when a semantic replay or server-owned projection drifts."""


@dataclass(frozen=True, slots=True)
class LifecycleCommandResult:
    command_id: str
    effect: Effect
    snapshot: InstallationSnapshot | None

    @property
    def completed(self) -> bool:
        return self.effect.state is EffectState.SETTLED_OK and self.snapshot is not None


class ExternalExtensionLifecycleCommandService:
    """The sole lifecycle command authority exposed to an extension caller.

    ``execute`` intentionally accepts only a revision reference, an action, and
    the caller's installation-state CAS revision.  All Effect identity and Gate
    values are derived from that semantic request on the server.
    """

    def __init__(
        self,
        records: SQLiteStructuredRecordStore,
        installations: ExternalExtensionInstallationStore,
        terminal_receipts: ExternalExtensionTerminalReceiptStore,
        runtime: EffectRuntime,
        *,
        gate_authority: object,
        now: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(records, SQLiteStructuredRecordStore):
            raise TypeError("lifecycle commands require structured records")
        if not isinstance(installations, ExternalExtensionInstallationStore):
            raise TypeError("lifecycle commands require the installation store")
        if not isinstance(terminal_receipts, ExternalExtensionTerminalReceiptStore):
            raise TypeError("lifecycle commands require terminal receipt facts")
        if not isinstance(runtime, EffectRuntime):
            raise TypeError("lifecycle commands require the Core EffectRuntime")
        if not callable(getattr(gate_authority, "authorize", None)):
            raise TypeError("lifecycle commands require the Core Gate authority")
        self._records = records
        self._installations = installations
        self._receipts = terminal_receipts
        self._runtime = runtime
        self._gate_authority = gate_authority
        self._now = now or time.time

    def execute(
        self,
        revision_ref: str,
        action: str,
        expected_state_revision: int,
        *,
        authorization_ref: str = "",
    ) -> LifecycleCommandResult:
        """Plan/execute one semantic command and project a verified terminal result."""

        semantic = self._semantic(revision_ref, action, expected_state_revision)
        revision = self._installations.load_revision(revision_ref)
        command_id = _derived("command", semantic)
        existing = self._records.read(_COMMANDS, command_id)
        if existing is not None:
            self._require_projection_semantic(existing.payload, semantic)
            authorization = self._reauthorize_projection(
                semantic, revision, existing.payload, authorization_ref=authorization_ref,
            )
            lifecycle_intent = self._receipts.load_intent(str(existing.payload["intent_ref"]))
        else:
            authorization = self._authorize(
                semantic, revision, authorization_ref=authorization_ref,
            )
            lifecycle_intent = self._ensure_intent(semantic, command_id, authorization)
        effect_intent = self._effect_intent(lifecycle_intent, semantic, authorization.decision_id)
        self._require_projected_operation(existing.payload if existing is not None else None, effect_intent)
        try:
            effect = self._runtime.log.get(effect_intent.operation_id)
        except KeyError:
            # This is a legacy crash window from before lifecycle planning was
            # atomic.  Backfill only reconstructs durable Core facts; it never
            # probes, dispatches, or settles an Effect.
            self._backfill_command_record(existing or self._records.read(_COMMANDS, command_id))
            effect = self._runtime.log.get(effect_intent.operation_id)
        self.validate_frozen_effect_identity(effect)
        effect = self._runtime.dispatch_operation(effect.operation_id, now=int(self._now()))
        self.validate_frozen_effect_identity(effect)
        snapshot = self._finalize_if_settled(
            semantic, command_id, effect, lifecycle_intent,
        )
        self._record_projection(semantic, command_id, lifecycle_intent, effect, snapshot)
        self._release_terminal_reservation(semantic, command_id, effect)
        return LifecycleCommandResult(command_id, effect, snapshot)

    # Friendly aliases keep composition code from inventing a parallel command path.
    handle = execute
    submit = execute

    def load(
        self, revision_ref: str, action: str, expected_state_revision: int,
    ) -> LifecycleCommandResult | None:
        """Rebuild a command response from Core, never from projection state alone.

        Newly written commands always already have a PLANNED Core Effect.  A
        missing Effect can only denote a pre-atomic legacy record and remains
        unavailable here until Core's bounded intent-only backfill has safely
        reconstructed its immutable planning facts.
        """

        semantic = self._semantic(revision_ref, action, expected_state_revision)
        command_id = _derived("command", semantic)
        record = self._records.read(_COMMANDS, command_id)
        if record is None:
            return None
        self._require_projection_semantic(record.payload, semantic)
        intent = self._receipts.load_intent(str(record.payload["intent_ref"]))
        effect_intent = self._effect_intent_from_projection(intent, semantic, record.payload)
        self._require_projected_operation(record.payload, effect_intent)
        try:
            effect = self._runtime.log.get(effect_intent.operation_id)
        except KeyError:
            # A command commit may precede Core planning.  No external work is
            # inferred from the coordination projection alone.
            return None
        self.validate_frozen_effect_identity(effect)
        snapshot = self._finalize_if_settled(semantic, command_id, effect, intent)
        self._record_projection(semantic, command_id, intent, effect, snapshot)
        self._release_terminal_reservation(semantic, command_id, effect)
        return LifecycleCommandResult(command_id, effect, snapshot)

    def reconcile_terminal_projections(self, *, limit: int = 100) -> int:
        """Project terminal Core Effects without scheduling or replaying work.

        Command rows are coordination projections only.  New lifecycle writes
        atomically include the immutable Gate fact, Effect intent fact, and
        PLANNED Core Effect.  This bounded terminal pass is deliberately run
        after the Core Reaper: it neither creates a missing Effect nor probes,
        dispatches, retries, or reauthorizes one.  Pre-atomic legacy
        intent-only rows are instead owned by the Core-registered
        ``backfill_intent_only_effects`` preparation pass.

        The integer return is solely an observability count for the caller;
        callers must not infer that a non-terminal command was recovered.
        """

        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1_000:
            raise ValueError("terminal projection reconcile limit must be between 1 and 1000")

        reconciled = 0
        for record in self._reconciliation_batch(limit):
            try:
                if self._reconcile_terminal_record(record):
                    reconciled += 1
            finally:
                # Advance for terminal, non-terminal, and malformed rows
                # alike.  A corrupt coordination projection must still surface
                # to the Core coordinator, but it must not pin every later
                # pass on the same row and starve a recoverable terminal
                # projection behind it.
                self._advance_reconciliation_cursor(record.object_id)
        return reconciled

    def backfill_intent_only_effects(self, *, limit: int = 100) -> int:
        """Atomically backfill pre-atomic lifecycle records into Core Effects.

        This migration seam is deliberately bounded and fail-closed.  It only
        recreates the immutable Gate fact, intent fact, and PLANNED Core Effect
        for an already durable lifecycle command after revalidating its saved
        authorization and installation reservation.  It never dispatches a
        Handler, probes an external system, retries, or settles an Effect.
        """

        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1_000:
            raise ValueError("intent-only backfill limit must be between 1 and 1000")
        backfilled = 0
        failures = False
        for record in self._coordination_batch(_BACKFILL_CURSOR, limit):
            try:
                if self._backfill_command_record(record):
                    backfilled += 1
            except (ExternalExtensionLifecycleCommandError, ExternalExtensionInstallationError, ValueError) as error:
                # One malformed legacy command must not starve later records.
                # Keep the precise cause out of the aggregate exception, but
                # make the batch failure visible to the Core coordinator after
                # the durable cursor has advanced through every row.
                _LOG.warning(
                    "external_extension_lifecycle_intent_only_backfill_invalid",
                    extra={
                        "event": "external_extension_lifecycle_intent_only_backfill_invalid",
                        "command_id": record.object_id,
                        "error_type": type(error).__name__,
                    },
                )
                failures = True
            finally:
                self._advance_coordination_cursor(_BACKFILL_CURSOR, record.object_id)
        if failures:
            raise ExternalExtensionLifecycleCommandConflict(
                "lifecycle intent-only backfill encountered invalid durable records"
            )
        return backfilled

    def _reconciliation_batch(self, limit: int):
        return self._coordination_batch(_TERMINAL_CURSOR, limit)

    def _coordination_batch(self, cursor_id: str, limit: int):
        records = self._records.list(_COMMANDS)
        if not records:
            return ()
        cursor = self._records.read(_COORDINATION, cursor_id)
        last_command_id = ""
        if cursor is not None:
            if set(cursor.payload) != {"schema_version", "last_command_id"} or (
                cursor.payload.get("schema_version") != "1.0.0"
                or not isinstance(cursor.payload.get("last_command_id"), str)
            ):
                raise ExternalExtensionLifecycleCommandConflict(
                    "terminal projection reconciliation cursor drifted"
                )
            last_command_id = str(cursor.payload["last_command_id"])
        start = next(
            (
                index for index, record in enumerate(records)
                if record.object_id > last_command_id
            ),
            0,
        )
        ordered = (*records[start:], *records[:start])
        return ordered[:limit]

    def _reconcile_terminal_record(self, record) -> bool:
        semantic = self._semantic_from_projection(record.payload)
        command_id = record.object_id
        self._require_projection_semantic(record.payload, semantic)
        intent_ref = record.payload.get("intent_ref")
        if not isinstance(intent_ref, str) or not intent_ref:
            raise ExternalExtensionLifecycleCommandConflict(
                "lifecycle command intent reference is missing"
            )
        intent = self._receipts.load_intent(intent_ref)
        effect_intent = self._effect_intent_from_projection(
            intent, semantic, record.payload,
        )
        self._require_projected_operation(record.payload, effect_intent)
        try:
            effect = self._runtime.log.get(effect_intent.operation_id)
        except KeyError:
            # Only a pre-atomic legacy record can now predate Core planning.
            # Never manufacture it from this terminal projection pass; Core's
            # dedicated bounded backfill preparation owns that migration.
            return False
        self.validate_frozen_effect_identity(effect)
        if effect.state not in {
            EffectState.SETTLED_OK,
            EffectState.SETTLED_ERR,
            EffectState.COMPENSATED,
            EffectState.ABANDONED,
        }:
            # PLANNED, INFLIGHT and UNKNOWN remain owned by Core recovery.
            return False
        snapshot = self._finalize_if_settled(semantic, command_id, effect, intent)
        self._record_projection(semantic, command_id, intent, effect, snapshot)
        self._release_terminal_reservation(semantic, command_id, effect)
        return True

    def _advance_reconciliation_cursor(self, command_id: str) -> None:
        self._advance_coordination_cursor(_TERMINAL_CURSOR, command_id)

    def _advance_coordination_cursor(self, cursor_id: str, command_id: str) -> None:
        payload = {
            "schema_version": "1.0.0",
            "last_command_id": command_id,
        }
        try:
            with self._records.begin() as uow:
                current = uow.read(_COORDINATION, cursor_id)
                expected_revision = current.revision if current is not None else 0
                uow.put(
                    _COORDINATION,
                    cursor_id,
                    payload,
                    expected_revision=expected_revision,
                )
                uow.commit()
        except SQLiteUnitOfWorkConflict:
            # A concurrent Core coordinator may have advanced the advisory
            # fairness cursor.  Terminal projection and reservation deletion
            # are independently CAS/idempotent, so the winner's cursor is safe.
            return

    def _semantic(self, revision_ref: str, action: str, expected: int) -> dict[str, object]:
        if action not in _ACTIONS:
            raise ExternalExtensionLifecycleCommandError("lifecycle action is invalid")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            raise ExternalExtensionLifecycleCommandError("expected state revision is invalid")
        revision = self._installations.load_revision(revision_ref)
        return {
            "root_id": revision.root_id,
            "extension_id": revision.extension_id,
            "revision_ref": revision.revision_ref,
            "action": action,
            "expected_state_revision": expected,
        }

    @staticmethod
    def _semantic_from_projection(payload: Mapping[str, object]) -> dict[str, object]:
        semantic = payload.get("semantic")
        if not isinstance(semantic, Mapping):
            raise ExternalExtensionLifecycleCommandConflict(
                "lifecycle command semantic is missing"
            )
        required_strings = ("root_id", "extension_id", "revision_ref", "action")
        if any(
            not isinstance(semantic.get(field), str) or not semantic[field]
            for field in required_strings
        ):
            raise ExternalExtensionLifecycleCommandConflict(
                "lifecycle command semantic is malformed"
            )
        expected = semantic.get("expected_state_revision")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            raise ExternalExtensionLifecycleCommandConflict(
                "lifecycle command semantic is malformed"
            )
        if semantic["action"] not in _ACTIONS:
            raise ExternalExtensionLifecycleCommandConflict(
                "lifecycle command semantic is malformed"
            )
        return {
            "root_id": semantic["root_id"],
            "extension_id": semantic["extension_id"],
            "revision_ref": semantic["revision_ref"],
            "action": semantic["action"],
            "expected_state_revision": expected,
        }

    def _ensure_intent(
        self, semantic: Mapping[str, object], command_id: str,
        authorization: ExternalExtensionGateAuthorization,
    ) -> ExternalExtensionLifecycleIntent:
        intent_id = _derived("intent", semantic)
        replay = self._records.read(_COMMANDS, command_id)
        if replay is not None:
            self._require_projection_semantic(replay.payload, semantic)
            return self._receipts.load_intent(str(replay.payload["intent_ref"]))
        revision = self._installations.load_revision(str(semantic["revision_ref"]))
        intent = self._installations.build_lifecycle_intent(
            revision.revision_ref,
            action=str(semantic["action"]),
            intent_id=intent_id,
        )
        effect_intent = self._effect_intent(intent, semantic, authorization.decision_id)
        payload = _intent_payload(intent)
        try:
            with self._records.begin() as uow:
                existing_command = uow.read(_COMMANDS, command_id)
                if existing_command is not None:
                    self._require_projection_semantic(existing_command.payload, semantic)
                    existing_intent = uow.read(_INTENTS, intent_id)
                    if (
                        existing_intent is None
                        or _canonical(existing_intent.payload) != _canonical(payload)
                    ):
                        raise ExternalExtensionLifecycleCommandConflict(
                            "lifecycle intent replay drifted"
                        )
                    uow.rollback()
                    return intent
                self._installations.require_lifecycle_state(
                    revision,
                    action=str(semantic["action"]),
                    expected_state_revision=int(semantic["expected_state_revision"]),
                    reader=uow.read,
                )
                if semantic["action"] == "uninstall":
                    self._installations.plan_uninstall_in_uow(
                        revision,
                        effect_operation_id=effect_intent.operation_id,
                        command_id=command_id,
                        expected_state_revision=int(semantic["expected_state_revision"]),
                        uow=uow,
                    )
                reservation_id = _reservation_id(semantic)
                reservation_payload = {
                    "schema_version": "1.0.0",
                    "reservation_id": reservation_id,
                    "command_id": command_id,
                    "root_id": semantic["root_id"],
                    "extension_id": semantic["extension_id"],
                    "expected_state_revision": semantic["expected_state_revision"],
                    "effect_operation_id": effect_intent.operation_id,
                }
                reservation = uow.read(_RESERVATIONS, reservation_id)
                if reservation is None:
                    uow.put(
                        _RESERVATIONS, reservation_id, reservation_payload,
                        expected_revision=0,
                    )
                elif _canonical(reservation.payload) != _canonical(reservation_payload):
                    raise ExternalExtensionLifecycleCommandConflict(
                        "installation revision already has a lifecycle command"
                    )
                existing_intent = uow.read(_INTENTS, intent_id)
                if existing_intent is None:
                    uow.put(_INTENTS, intent_id, payload, expected_revision=0)
                elif _canonical(existing_intent.payload) != _canonical(payload):
                    raise ExternalExtensionLifecycleCommandConflict("lifecycle intent drifted")
                uow.put(
                    _COMMANDS, command_id,
                    {
                        "schema_version": "1.0.0", "semantic": dict(semantic),
                        "intent_ref": intent.intent_ref,
                        "operation_id": effect_intent.operation_id,
                        "gate_decision_id": authorization.decision_id,
                        "authorization_ref": authorization.authorization_ref,
                        "snapshot": None,
                    }, expected_revision=0,
                )
                planned, _created = self._runtime.log.plan_v2_in_connection(
                    uow.connection,
                    effect_intent,
                    gate_decision_id=authorization.decision_id,
                    gate_fact=authorization.fact,
                    now=int(self._now()),
                )
                self._require_effect_identity(planned, effect_intent)
                uow.commit()
                return intent
        except (SQLiteUnitOfWorkConflict, ExternalExtensionInstallationError) as error:
            existing = self._records.read(_COMMANDS, command_id)
            if existing is None:
                raise ExternalExtensionLifecycleCommandConflict(str(error)) from error
            self._require_projection_semantic(existing.payload, semantic)
            return self._receipts.load_intent(str(existing.payload["intent_ref"]))

    def _authorize(
        self, semantic: Mapping[str, object], revision, *, authorization_ref: str,
    ) -> ExternalExtensionGateAuthorization:
        return self._gate_authority.authorize(ExternalExtensionGateRequest(
            phase=f"lifecycle_{semantic['action']}",
            project_id=str(semantic["root_id"]),
            subject_ref=revision.intake_ref,
            authorization_ref=authorization_ref,
            policy_revision=_POLICY,
            revision_ref=str(semantic["revision_ref"]),
            expected_state_revision=int(semantic["expected_state_revision"]),
        ))

    def _reauthorize_projection(
        self, semantic: Mapping[str, object], revision, payload: Mapping[str, object], *,
        authorization_ref: str = "",
    ) -> ExternalExtensionGateAuthorization:
        stored_ref = payload.get("authorization_ref")
        stored_decision = payload.get("gate_decision_id")
        if not isinstance(stored_ref, str) or not stored_ref or not isinstance(stored_decision, str) or not stored_decision:
            raise ExternalExtensionLifecycleCommandConflict(
                "lifecycle command Gate authorization is missing"
            )
        if authorization_ref and authorization_ref != stored_ref:
            raise ExternalExtensionLifecycleCommandConflict(
                "lifecycle command Gate authorization drifted"
            )
        authorization = self._authorize(semantic, revision, authorization_ref=stored_ref)
        if authorization.decision_id != stored_decision:
            raise ExternalExtensionLifecycleCommandConflict(
                "lifecycle command Gate decision drifted"
            )
        return authorization

    def _backfill_command_record(self, record) -> bool:
        if record is None:
            raise ExternalExtensionLifecycleCommandConflict("lifecycle command disappeared")
        semantic = self._semantic_from_projection(record.payload)
        command_id = record.object_id
        self._require_projection_semantic(record.payload, semantic)
        revision = self._installations.load_revision(str(semantic["revision_ref"]))
        if revision.root_id != semantic["root_id"] or revision.extension_id != semantic["extension_id"]:
            raise ExternalExtensionLifecycleCommandConflict("lifecycle command installation revision drifted")
        authorization = self._reauthorize_projection(semantic, revision, record.payload)
        intent_ref = record.payload.get("intent_ref")
        if not isinstance(intent_ref, str) or not intent_ref:
            raise ExternalExtensionLifecycleCommandConflict("lifecycle command intent reference is missing")
        intent = self._receipts.load_intent(intent_ref)
        self._require_intent_revision_contract(intent, revision, semantic)
        effect_intent = self._effect_intent(intent, semantic, authorization.decision_id)
        self._require_projected_operation(record.payload, effect_intent)
        try:
            current = self._runtime.log.get(effect_intent.operation_id)
        except KeyError:
            current = None
        if current is not None:
            self._require_effect_identity(current, effect_intent)
            self._validate_rebuilt_effect_identity(current, effect_intent, authorization)
            return False
        reservation_id = _reservation_id(semantic)
        expected_reservation = self._reservation_payload(semantic, command_id, effect_intent.operation_id)
        try:
            with self._records.begin() as uow:
                command = uow.read(_COMMANDS, command_id)
                if command is None:
                    raise ExternalExtensionLifecycleCommandConflict("lifecycle command disappeared")
                self._require_projection_semantic(command.payload, semantic)
                # The persisted authorization was re-evaluated before this
                # BEGIN IMMEDIATE transaction.  Do not open FactStore/Gate's
                # second SQLite connection while this write lock is held.
                # Instead make the outer authorization's exact command
                # projection a transaction-local CAS precondition: any
                # semantic, authorization, decision, operation, or snapshot
                # mutation since the Gate check fails closed.
                if _canonical(command.payload) != _canonical(record.payload):
                    raise ExternalExtensionLifecycleCommandConflict(
                        "lifecycle command changed during intent-only backfill"
                    )
                if command.payload.get("intent_ref") != intent.intent_ref:
                    raise ExternalExtensionLifecycleCommandConflict("lifecycle command intent drifted")
                if command.payload.get("operation_id") != effect_intent.operation_id:
                    raise ExternalExtensionLifecycleCommandConflict("lifecycle command operation drifted")
                stored_intent = uow.read(_INTENTS, intent.intent_id)
                if stored_intent is None or _canonical(stored_intent.payload) != _canonical(_intent_payload(intent)):
                    raise ExternalExtensionLifecycleCommandConflict("lifecycle intent drifted")
                reservation = uow.read(_RESERVATIONS, reservation_id)
                if reservation is None or _canonical(reservation.payload) != _canonical(expected_reservation):
                    raise ExternalExtensionLifecycleCommandConflict("lifecycle reservation drifted")
                self._installations.require_lifecycle_state(
                    revision, action=str(semantic["action"]),
                    expected_state_revision=int(semantic["expected_state_revision"]), reader=uow.read,
                )
                planned, _created = self._runtime.log.plan_v2_in_connection(
                    uow.connection,
                    effect_intent,
                    gate_decision_id=authorization.decision_id,
                    gate_fact=authorization.fact,
                    now=int(self._now()),
                )
                self._require_effect_identity(planned, effect_intent)
                uow.commit()
                return True
        except SQLiteUnitOfWorkConflict as error:
            try:
                current = self._runtime.log.get(effect_intent.operation_id)
            except KeyError:
                raise ExternalExtensionLifecycleCommandConflict(str(error)) from error
            self._require_effect_identity(current, effect_intent)
            self._validate_rebuilt_effect_identity(current, effect_intent, authorization)
            return False

    @staticmethod
    def _reservation_payload(
        semantic: Mapping[str, object], command_id: str, operation_id: str,
    ) -> dict[str, object]:
        reservation_id = _reservation_id(semantic)
        return {
            "schema_version": "1.0.0",
            "reservation_id": reservation_id,
            "command_id": command_id,
            "root_id": semantic["root_id"],
            "extension_id": semantic["extension_id"],
            "expected_state_revision": semantic["expected_state_revision"],
            "effect_operation_id": operation_id,
        }

    def _effect_intent(
        self,
        intent: ExternalExtensionLifecycleIntent,
        semantic: Mapping[str, object],
        gate_decision_id: str,
    ):
        session_id = _derived("session", semantic)
        step_key = _derived("step", semantic)
        gate_id = gate_decision_id
        return build_lifecycle_effect_intent(
            intent, session_id=session_id, step_key=step_key, gate_decision_id=gate_id,
            rev_set={
                key: ({"policy": _POLICY, "boundary": _BOUNDARY, "handler": _HANDLER}.get(key, NOT_APPLICABLE))
                for key in V2_REVISION_KEYS
            },
            idem_key=_derived("idem", semantic),
        )

    def _effect_intent_from_projection(
        self, intent: ExternalExtensionLifecycleIntent, semantic: Mapping[str, object], payload: Mapping[str, object],
    ):
        decision_id = payload.get("gate_decision_id")
        if not isinstance(decision_id, str):
            raise ExternalExtensionLifecycleCommandConflict("lifecycle command Gate authorization is missing")
        return self._effect_intent(intent, semantic, decision_id)

    def validate_frozen_effect_identity(self, effect: Effect) -> None:
        """Validate an Effect from command, immutable intent, and Gate evidence."""
        if not isinstance(effect, Effect):
            raise TypeError("lifecycle frozen identity requires an Effect")
        matches = [
            record for record in self._records.list(_COMMANDS)
            if record.payload.get("operation_id") == effect.operation_id
        ]
        if len(matches) != 1:
            raise ExternalExtensionLifecycleCommandConflict(
                "lifecycle Effect has no unique durable command authority"
            )
        record = matches[0]
        semantic = self._semantic_from_projection(record.payload)
        self._require_command_contract(record.object_id, record.payload, semantic)
        revision = self._installations.load_revision(str(semantic["revision_ref"]))
        if revision.root_id != semantic["root_id"] or revision.extension_id != semantic["extension_id"]:
            raise ExternalExtensionLifecycleCommandConflict(
                "lifecycle command installation revision drifted"
            )
        authorization = self._reauthorize_projection(semantic, revision, record.payload)
        intent_ref = record.payload.get("intent_ref")
        if not isinstance(intent_ref, str) or not intent_ref:
            raise ExternalExtensionLifecycleCommandConflict(
                "lifecycle command intent reference is missing"
            )
        intent = self._receipts.load_intent(intent_ref)
        self._require_command_contract(record.object_id, record.payload, semantic, intent)
        self._require_intent_revision_contract(intent, revision, semantic)
        expected = self._effect_intent(intent, semantic, authorization.decision_id)
        self._require_projected_operation(record.payload, expected)
        self._validate_rebuilt_effect_identity(effect, expected, authorization)

    def _validate_rebuilt_effect_identity(self, effect: Effect, expected, authorization) -> None:
        """Check Effect row plus its immutable Core intent and Gate facts."""
        try:
            persisted = self._runtime.log.require_v2_execution_facts(
                expected,
                gate_decision_id=authorization.decision_id,
                gate_fact=authorization.fact,
            )
        except (KeyError, RuntimeError, ValueError) as error:
            raise ExternalExtensionLifecycleCommandConflict(
                "lifecycle command frozen Core authority drifted"
            ) from error
        _require_complete_effect_identity(effect, expected)
        _require_complete_effect_identity(persisted, expected)

    @staticmethod
    def _require_intent_revision_contract(intent, revision, semantic: Mapping[str, object]) -> None:
        """Bind frozen intent to its immutable revision without live-state CAS.

        A settled health/activation command legitimately changes the current
        installation snapshot.  Recovery must therefore verify revision facts,
        never re-run the command's former lifecycle-state precondition.
        """
        expected = (
            str(semantic["action"]), revision.root_id, revision.revision_ref,
            revision.intake_ref, revision.artifact_ref, revision.artifact_receipt_ref,
            revision.artifact_content_sha256, revision.manifest_identity,
            revision.review_plan_identity, revision.activation_plan_identity,
            revision.health_plan_identity,
        )
        actual = (
            intent.action, intent.root_id, intent.revision_ref, intent.intake_ref,
            intent.artifact_ref, intent.artifact_receipt_ref,
            intent.artifact_content_sha256, intent.manifest_identity,
            intent.review_plan_identity, intent.activation_plan_identity,
            intent.health_plan_identity,
        )
        if actual != expected:
            raise ExternalExtensionLifecycleCommandConflict(
                "lifecycle intent revision contract drifted"
            )

    @staticmethod
    def _require_command_contract(
        command_id: str,
        payload: Mapping[str, object],
        semantic: Mapping[str, object],
        intent: ExternalExtensionLifecycleIntent | None = None,
    ) -> None:
        """Require the immutable lifecycle command derivation and schema."""
        expected_fields = {
            "schema_version", "semantic", "intent_ref", "operation_id",
            "gate_decision_id", "authorization_ref", "snapshot",
        }
        if (
            set(payload) != expected_fields
            or payload.get("schema_version") != "1.0.0"
            or command_id != _derived("command", semantic)
        ):
            raise ExternalExtensionLifecycleCommandConflict(
                "lifecycle command identity or schema drifted"
            )
        ExternalExtensionLifecycleCommandService._require_projection_semantic(payload, semantic)
        if intent is not None:
            if intent.intent_id != _derived("intent", semantic) or payload.get("intent_ref") != intent.intent_ref:
                raise ExternalExtensionLifecycleCommandConflict(
                    "lifecycle command intent identity drifted"
                )

    def _finalize_if_settled(self, semantic, command_id, effect, intent):
        if effect.state is not EffectState.SETTLED_OK:
            return None
        # The installation store verifies both Core SETTLED_OK and the immutable
        # Receipt.  No PLANNED/INFLIGHT/UNKNOWN state can become a snapshot here.
        projection_id = _derived("projection", semantic)
        expected = int(semantic["expected_state_revision"])
        try:
            if intent.action == "health":
                return self._installations.record_health(
                    intent.revision_ref, effect_operation_id=effect.operation_id,
                    command_id=projection_id, expected_state_revision=expected,
                )
            if intent.action == "activation":
                return self._installations.finalize_activation(
                    intent.revision_ref, effect_operation_id=effect.operation_id,
                    command_id=projection_id, expected_state_revision=expected,
                )
            if intent.action == "disable":
                return self._installations.finalize_disable(
                    intent.revision_ref, effect_operation_id=effect.operation_id,
                    command_id=projection_id, expected_state_revision=expected,
                )
            if intent.action == "uninstall":
                return self._installations.finalize_uninstall(
                    intent.revision_ref, effect_operation_id=effect.operation_id,
                    command_id=projection_id, expected_state_revision=expected,
                )
            return self._installations.finalize_rollback(
                str(semantic["extension_id"]), target_revision_reference=intent.revision_ref,
                effect_operation_id=effect.operation_id, command_id=projection_id,
                expected_state_revision=expected,
            )
        except ExternalExtensionInstallationConflict as error:
            raise ExternalExtensionLifecycleCommandConflict(str(error)) from error

    def _record_projection(self, semantic, command_id, intent, effect, snapshot) -> None:
        try:
            with self._records.begin() as uow:
                record = uow.read(_COMMANDS, command_id)
                if record is None:
                    raise ExternalExtensionLifecycleCommandConflict("lifecycle command disappeared")
                self._require_projection_semantic(record.payload, semantic)
                payload = dict(record.payload)
                expected_operation = payload.get("operation_id")
                if expected_operation not in {None, effect.operation_id}:
                    raise ExternalExtensionLifecycleCommandConflict("lifecycle command operation drifted")
                payload["intent_ref"] = intent.intent_ref
                payload["operation_id"] = effect.operation_id
                if snapshot is not None:
                    encoded = asdict(snapshot)
                    prior_snapshot = payload.get("snapshot")
                    if prior_snapshot is not None and prior_snapshot != encoded:
                        raise ExternalExtensionLifecycleCommandConflict("lifecycle command snapshot drifted")
                    payload["snapshot"] = encoded
                if payload == dict(record.payload):
                    uow.rollback()
                    return
                uow.put(_COMMANDS, command_id, payload, expected_revision=record.revision)
                uow.commit()
        except SQLiteUnitOfWorkConflict as error:
            record = self._records.read(_COMMANDS, command_id)
            if record is None:
                raise ExternalExtensionLifecycleCommandConflict(str(error)) from error
            self._require_projection_semantic(record.payload, semantic)
            expected_snapshot = asdict(snapshot) if snapshot is not None else None
            if (
                record.payload.get("intent_ref") != intent.intent_ref
                or record.payload.get("operation_id") != effect.operation_id
                or (
                    expected_snapshot is not None
                    and record.payload.get("snapshot") != expected_snapshot
                )
            ):
                raise ExternalExtensionLifecycleCommandConflict(str(error)) from error

    def _release_terminal_reservation(self, semantic, command_id, effect) -> None:
        """Release only a terminal Core-owned reservation after identity checks.

        UNKNOWN deliberately remains reserved: Core Reaper, reauthorization, or
        explicit abandonment owns its resolution.  Replaying this exact command
        after Core resolves it is reentrant and performs the release.
        """

        if effect.state not in {
            EffectState.SETTLED_OK,
            EffectState.SETTLED_ERR,
            EffectState.ABANDONED,
            EffectState.COMPENSATED,
        }:
            return
        reservation_id = _reservation_id(semantic)
        try:
            with self._records.begin() as uow:
                reservation = uow.read(_RESERVATIONS, reservation_id)
                if reservation is None:
                    uow.rollback()
                    return
                expected = {
                    "schema_version": "1.0.0",
                    "reservation_id": reservation_id,
                    "command_id": command_id,
                    "root_id": semantic["root_id"],
                    "extension_id": semantic["extension_id"],
                    "expected_state_revision": semantic["expected_state_revision"],
                    "effect_operation_id": effect.operation_id,
                }
                if _canonical(reservation.payload) != _canonical(expected):
                    raise ExternalExtensionLifecycleCommandConflict(
                        "lifecycle reservation drifted"
                    )
                command = uow.read(_COMMANDS, command_id)
                if command is None:
                    raise ExternalExtensionLifecycleCommandConflict(
                        "lifecycle reservation command disappeared"
                    )
                self._require_projection_semantic(command.payload, semantic)
                if command.payload.get("operation_id") != effect.operation_id:
                    raise ExternalExtensionLifecycleCommandConflict(
                        "lifecycle reservation operation drifted"
                    )
                uow.delete(
                    _RESERVATIONS, reservation_id,
                    expected_revision=reservation.revision,
                )
                uow.commit()
        except SQLiteUnitOfWorkConflict as error:
            # A concurrent reconciler may already have released this exact
            # terminal reservation.  Re-read before surfacing a real drift.
            if self._records.read(_RESERVATIONS, reservation_id) is None:
                return
            raise ExternalExtensionLifecycleCommandConflict(str(error)) from error

    @staticmethod
    def _require_projected_operation(
        payload: Mapping[str, object] | None, effect_intent,
    ) -> None:
        if payload is None:
            return
        operation_id = payload.get("operation_id")
        if operation_id not in {None, effect_intent.operation_id}:
            raise ExternalExtensionLifecycleCommandConflict(
                "lifecycle command operation drifted"
            )

    @staticmethod
    def _require_effect_identity(effect: Effect, expected) -> None:
        _require_complete_effect_identity(effect, expected)

    @staticmethod
    def _require_projection_semantic(payload: Mapping[str, object], semantic: Mapping[str, object]) -> None:
        if _canonical(payload.get("semantic")) != _canonical(semantic):
            raise ExternalExtensionLifecycleCommandConflict("lifecycle command semantic drifted")


def _require_complete_effect_identity(effect: Effect, expected) -> None:
    """Compare the complete persisted v2 identity, never a partial tuple."""
    scalar_fields = (
        "operation_id", "session_id", "turn_id", "root_id", "parent_id",
        "step_key", "kind", "effect_class", "purpose", "intent_ref",
        "intent_digest", "gate_decision_id", "idem_key", "contract_version",
        "intent_schema_version", "expected_receipt_kind",
        "expected_receipt_schema_version", "authority_set_id",
        "identity_algorithm", "revision_schema_version",
    )
    if any(getattr(effect, field) != getattr(expected, field) for field in scalar_fields):
        raise ExternalExtensionLifecycleCommandConflict(
            "lifecycle command Core Effect identity drifted"
        )
    if _canonical(dict(effect.rev_set)) != _canonical(dict(expected.rev_set)):
        raise ExternalExtensionLifecycleCommandConflict(
            "lifecycle command Core authority revisions drifted"
        )

# The concise name is useful at composition boundaries; both names denote the
# same single authority rather than separate handlers.
ExternalExtensionLifecycleCommands = ExternalExtensionLifecycleCommandService


def _derived(prefix: str, semantic: Mapping[str, object]) -> str:
    return f"{prefix}-" + blake3(_canonical(semantic).encode("utf-8")).hexdigest()


def _reservation_id(semantic: Mapping[str, object]) -> str:
    return _derived(
        "reservation",
        {
            "root_id": semantic["root_id"],
            "extension_id": semantic["extension_id"],
            "expected_state_revision": semantic["expected_state_revision"],
        },
    )


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _intent_payload(intent: ExternalExtensionLifecycleIntent) -> dict[str, object]:
    return _canonical_dict({
        **asdict(intent), "health_checks": list(intent.health_checks), "intent_ref": intent.intent_ref,
    })


def _canonical_dict(value: Mapping[str, object]) -> dict[str, object]:
    return json.loads(_canonical(value))
