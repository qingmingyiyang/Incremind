from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from core.external_extensions import (
    ArtifactInventory,
    ExtensionManifest,
    ExtensionReviewPlan,
    InstallIntent,
    ResolvedSource,
    SourceSpec,
)
from core.effect_log import EFFECT_V2, EffectClass, EffectPurpose
from core.storage_provider import (
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SQLiteUnitOfWorkConflict,
)

from .artifact_evidence import (
    ArtifactEvidence,
    ImmutableQuarantineArtifactStore,
)


class ExternalExtensionFactError(ValueError):
    """Raised when a durable external extension fact is invalid."""


class ExternalExtensionFactConflict(ExternalExtensionFactError):
    """Raised when immutable intake identity or content drifts."""


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:-]{7,159}$")
_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_INTENTS = "external_extension_install_intents"
_PROPOSALS = "external_extension_install_proposals"
_SOURCE_CONFIRMATIONS = "external_extension_source_confirmations"
_REVISION_CONFIRMATIONS = "external_extension_revision_confirmations"
_LIFECYCLE_CONFIRMATIONS = "external_extension_lifecycle_confirmations"
_RESOLUTIONS = "external_extension_source_resolutions"
_RESOLUTION_OBSERVATIONS = "external_extension_source_resolution_observations"
_INTAKES = "external_extension_intake_receipts"
_COMMANDS = "external_extension_install_commands"
_BACKFILL_COORDINATION = "external_extension_install_backfill_coordination"
_BACKFILL_FAILURES = "external_extension_install_backfill_failures"
_CONFIRMED_ACQUIRE_BACKFILL_FAILURES = "external_extension_confirmed_acquire_backfill_failures"
_RESOLVE_BACKFILL_CURSOR = "resolve-intent-only-backfill-v1"
_CONFIRMED_ACQUIRE_BACKFILL_CURSOR = "confirmed-acquire-intent-backfill-v1"
_INTENT_REF = "crp://external-extension-install-intents/"
_PROPOSAL_REF = "crp://external-extension-install-proposals/"
_SOURCE_CONFIRMATION_REF = "crp://external-extension-source-confirmations/"
_REVISION_CONFIRMATION_REF = "crp://external-extension-revision-confirmations/"
_LIFECYCLE_CONFIRMATION_REF = "crp://external-extension-lifecycle-confirmations/"
_RESOLUTION_REF = "crp://external-extension-source-resolutions/"
_INTAKE_REF = "crp://external-extension-intake-receipts/"
_ARTIFACT_REF = "crp://extension-artifacts/"
_OBSERVATION_REF = "crp://external-extension-source-observations/"


class ExternalExtensionFactStore:
    """Immutable lifecycle facts; execution state remains in Core Effect Log."""

    def __init__(
        self,
        records: SQLiteStructuredRecordStore,
        artifacts: ImmutableQuarantineArtifactStore,
    ) -> None:
        if not isinstance(artifacts, ImmutableQuarantineArtifactStore):
            raise TypeError("artifacts must be an ImmutableQuarantineArtifactStore")
        self._records = records
        self._artifacts = artifacts

    def record_intent(self, intent: InstallIntent, *, command_id: str) -> str:
        command = _command_id(command_id)
        payload = _canonical_payload(_intent_payload(intent))
        try:
            with self._records.begin() as uow:
                replay = uow.read(_COMMANDS, command)
                if replay is not None:
                    _require_payload(replay, {"operation": "record_intent", "intent_id": intent.intent_id, "payload": payload})
                    current = _required(uow.read(_INTENTS, intent.intent_id), "install intent")
                    _require_payload(current, payload)
                    uow.rollback()
                    return intent_ref(intent.intent_id)
                _put_immutable(uow, _INTENTS, intent.intent_id, payload)
                uow.put(
                    _COMMANDS,
                    command,
                    {"operation": "record_intent", "intent_id": intent.intent_id, "payload": payload},
                    expected_revision=0,
                )
                uow.commit()
                return intent_ref(intent.intent_id)
        except SQLiteUnitOfWorkConflict as error:
            raise ExternalExtensionFactConflict(str(error)) from error

    def _record_intent_and_plan_effect(
        self,
        intent: InstallIntent,
        *,
        command_id: str,
        command_payload: Mapping[str, object],
        effect_log,
        effect_intent,
        gate_decision_id: str,
        gate_fact,
        now: int,
    ):
        """Atomically persist an intake command/intent and its Core v2 Effect.

        The caller must derive and validate Gate authorization before this
        method opens the write transaction.  The supplied EffectLog joins the
        same SQLite connection, so a crash cannot leave a newly-created
        resolve command/intention without its PLANNED Effect.
        """

        command = _command_id(command_id)
        intent_payload = _canonical_payload(_intent_payload(intent))
        expected_command = _canonical_payload(command_payload)
        operation = expected_command.get("operation")
        contracts = {
            "resolve_install_v2": (
                "external_extension_source_resolve",
                "external-extension-source-resolve/v1",
                "external-extension-source-resolution",
                "external-extension-source-resolution/v1",
                "resolve-command",
                {"schema_version", "operation", "intent_id", "project_id", "authorization_ref", "gate_decision_id", "operation_id"},
            ),
            "acquire_resolved_v2": (
                "external_extension_acquire_intake",
                "external-extension-acquire-intake/v1",
                "external-extension-intake",
                "external-extension-intake/v1",
                "acquire-command",
                {"schema_version", "operation", "intent_id", "project_id", "authorization_ref", "gate_decision_id", "operation_id", "resolution_operation_id"},
            ),
        }
        if operation not in contracts:
            raise ExternalExtensionFactConflict("external extension atomic plan operation is invalid")
        kind, schema, receipt_kind, receipt_schema, prefix, fields = contracts[operation]
        semantic_id = intent.intent_id if operation == "resolve_install_v2" else expected_command.get("resolution_operation_id")
        if (
            set(expected_command) != fields
            or expected_command.get("schema_version") != "2.0.0"
            or command != _derived_command_id(prefix, str(semantic_id))
            or getattr(effect_intent, "kind", None) != kind
            or getattr(effect_intent, "intent_schema_version", None) != schema
            or getattr(effect_intent, "expected_receipt_kind", None) != receipt_kind
            or getattr(effect_intent, "expected_receipt_schema_version", None) != receipt_schema
            or getattr(effect_intent, "effect_class", None) is not EffectClass.QUERYABLE
            or getattr(effect_intent, "purpose", None) is not EffectPurpose.PRIMARY
            or getattr(effect_intent, "contract_version", None) != EFFECT_V2
            or getattr(effect_intent, "turn_id", None) is not None
            or getattr(effect_intent, "parent_id", None) is not None
            or getattr(effect_intent, "identity_algorithm", None) != "blake3-256"
            or getattr(effect_intent, "revision_schema_version", None) != "effect-authority-v2"
            or dict(getattr(effect_intent, "rev_set", {})) != {
                "policy": "external-extension-natural-language-policy-v1",
                "boundary": "external-extension-natural-language-boundary-v1",
                "capability": "not_applicable", "context_manifest": "not_applicable",
                "provider": "not_applicable", "model_route": "not_applicable",
                "bundle": "not_applicable", "handler": "external-extension-natural-language-handler-v1",
                "secret": "not_applicable", "budget": "not_applicable",
                "workflow": "not_applicable",
            }
        ):
            raise ExternalExtensionFactConflict(
                f"{str(operation).removesuffix('_v2')} atomic plan contract drifted",
            )
        operation_label = str(operation).removesuffix("_v2")
        if expected_command.get("intent_id") != intent.intent_id:
            raise ExternalExtensionFactConflict(f"{operation_label} command intent identity drifted")
        if expected_command.get("operation_id") != effect_intent.operation_id:
            raise ExternalExtensionFactConflict(f"{operation_label} command Effect identity drifted")
        if expected_command.get("gate_decision_id") != gate_decision_id:
            raise ExternalExtensionFactConflict(f"{operation_label} command Gate identity drifted")
        if Path(str(getattr(effect_log, "database", ""))).resolve(strict=False) != self._records.database_path:
            raise ExternalExtensionFactConflict(
                "Core Effect log and external extension facts must share one database"
            )
        try:
            with self._records.begin() as uow:
                replay = uow.read(_COMMANDS, command)
                if replay is None:
                    _put_immutable(uow, _INTENTS, intent.intent_id, intent_payload)
                    uow.put(_COMMANDS, command, expected_command, expected_revision=0)
                else:
                    _require_payload(replay, expected_command)
                    current = _required(uow.read(_INTENTS, intent.intent_id), "install intent")
                    _require_payload(current, intent_payload)
                effect, _created = effect_log.plan_v2_in_connection(
                    uow.connection,
                    effect_intent,
                    gate_decision_id=gate_decision_id,
                    gate_fact=gate_fact,
                    now=now,
                )
                uow.commit()
                return effect
        except SQLiteUnitOfWorkConflict as error:
            raise ExternalExtensionFactConflict(str(error)) from error

    def load_v2_command_for_operation(
        self,
        operation_id: str,
        *,
        operation: str,
    ) -> dict[str, object]:
        """Load the sole immutable schema-2 command bound to an Effect.

        Handlers must not treat an Effect operation id as sufficient proof of
        authorization: it binds only the deterministic identity fields.  This
        lookup makes the persisted command, including its confirmation and
        Gate identity, a required part of the execution proof.
        """

        if operation not in {"resolve_install_v2", "acquire_resolved_v2"}:
            raise ValueError("external extension command operation is invalid")
        candidates: list[tuple[SQLiteStructuredRecord, dict[str, object]]] = []
        for record in self._records.list(_COMMANDS):
            payload = _canonical_payload(record.payload)
            if payload.get("operation_id") == operation_id:
                candidates.append((record, payload))
        if len(candidates) != 1:
            raise ExternalExtensionFactConflict(
                "external extension Effect must have exactly one durable v2 command",
            )
        record, payload = candidates[0]
        expected = {
            "schema_version", "operation", "intent_id", "project_id",
            "authorization_ref", "gate_decision_id", "operation_id",
        }
        if operation == "acquire_resolved_v2":
            expected.add("resolution_operation_id")
        if (
            set(payload) != expected
            or payload.get("schema_version") != "2.0.0"
            or payload.get("operation") != operation
            or payload.get("operation_id") != operation_id
        ):
            raise ExternalExtensionFactConflict(
                "external extension durable command contract drifted",
            )
        for name in expected - {"schema_version", "operation"}:
            value = payload.get(name)
            if not isinstance(value, str) or not value:
                raise ExternalExtensionFactConflict(
                    "external extension durable command identity is invalid",
                )
        semantic_identity = (
            str(payload["intent_id"])
            if operation == "resolve_install_v2"
            else str(payload["resolution_operation_id"])
        )
        prefix = "resolve-command" if operation == "resolve_install_v2" else "acquire-command"
        if record.object_id != _derived_command_id(prefix, semantic_identity):
            raise ExternalExtensionFactConflict(
                "external extension durable command semantic identity drifted",
            )
        return payload

    def resolve_intent_only_backfill_batch(self, *, limit: int):
        """Return one durable cursor page of resolve command records."""

        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1_000:
            raise ValueError("resolve intent-only backfill limit must be between 1 and 1000")
        records = tuple(
            record for record in self._records.list(_COMMANDS)
            if not self._resolve_intent_only_backfill_failed(record.object_id)
        )
        if not records:
            return ()
        cursor = self._records.read(_BACKFILL_COORDINATION, _RESOLVE_BACKFILL_CURSOR)
        last = ""
        if cursor is not None:
            payload = cursor.payload
            if set(payload) != {"schema_version", "last_command_id"} or (
                payload.get("schema_version") != "1.0.0"
                or not isinstance(payload.get("last_command_id"), str)
            ):
                raise ExternalExtensionFactConflict("resolve intent-only backfill cursor drifted")
            last = str(payload["last_command_id"])
        start = next((index for index, record in enumerate(records) if record.object_id > last), 0)
        return (*records[start:], *records[:start])[:limit]

    def advance_resolve_intent_only_backfill_cursor(self, command_id: str) -> None:
        command = _command_id(command_id)
        payload = {"schema_version": "1.0.0", "last_command_id": command}
        try:
            with self._records.begin() as uow:
                current = uow.read(_BACKFILL_COORDINATION, _RESOLVE_BACKFILL_CURSOR)
                uow.put(
                    _BACKFILL_COORDINATION,
                    _RESOLVE_BACKFILL_CURSOR,
                    payload,
                    expected_revision=current.revision if current is not None else 0,
                )
                uow.commit()
        except SQLiteUnitOfWorkConflict:
            return

    def record_resolve_intent_only_backfill_failure(
        self, command_id: str, *, reason_code: str, error: Exception,
    ) -> None:
        command = _command_id(command_id)
        if reason_code not in {
            "unsupported_legacy_provenance", "malformed_command",
            "integrity_conflict", "missing_authority",
        }:
            raise ValueError("resolve backfill failure reason is invalid")
        payload = _canonical_payload({
            "schema_version": "1.0.0",
            "reason_code": reason_code,
            "error_type": type(error).__name__,
        })
        try:
            self._record_immutable(_BACKFILL_FAILURES, command, payload)
        except ExternalExtensionFactConflict:
            # The first durable failure remains evidence; subsequent scans do
            # not overwrite it.  Validate it before honoring the exclusion so
            # a forged record cannot permanently hide a command.
            existing = _required(
                self._records.read(_BACKFILL_FAILURES, command),
                "resolve intent-only backfill failure",
            )
            _require_payload(existing, payload)

    def _resolve_intent_only_backfill_failed(self, command_id: str) -> bool:
        command = _command_id(command_id)
        record = self._records.read(_BACKFILL_FAILURES, command)
        if record is None:
            return False
        payload = _canonical_payload(record.payload)
        expected = {"schema_version", "reason_code", "error_type"}
        if (
            set(payload) != expected
            or payload.get("schema_version") != "1.0.0"
            or payload.get("reason_code") not in {
                "unsupported_legacy_provenance", "malformed_command",
                "integrity_conflict", "missing_authority",
            }
            or not isinstance(payload.get("error_type"), str)
        ):
            raise ExternalExtensionFactConflict(
                "resolve intent-only backfill failure evidence drifted",
            )
        return True

    def confirmed_acquire_intent_backfill_batch(self, *, limit: int):
        """Return a bounded cursor page of immutable resolutions for recovery.

        This intentionally reads resolutions rather than commands.  A crash
        can occur after a source/revision confirmation was made durable but
        before the intake facade created its atomic acquire command.  Only the
        confirmation rules in the Core backfill may reconstruct that command.
        """

        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1_000:
            raise ValueError("confirmed acquire backfill limit must be between 1 and 1000")
        records = tuple(
            record for record in self._records.list(_RESOLUTIONS)
            if not self._confirmed_acquire_backfill_failed(record.object_id)
        )
        if not records:
            return ()
        cursor = self._records.read(
            _BACKFILL_COORDINATION, _CONFIRMED_ACQUIRE_BACKFILL_CURSOR,
        )
        last = ""
        if cursor is not None:
            payload = cursor.payload
            if set(payload) != {"schema_version", "last_resolution_operation_id"} or (
                payload.get("schema_version") != "1.0.0"
                or not isinstance(payload.get("last_resolution_operation_id"), str)
            ):
                raise ExternalExtensionFactConflict(
                    "confirmed acquire backfill cursor drifted",
                )
            last = str(payload["last_resolution_operation_id"])
        start = next((index for index, record in enumerate(records) if record.object_id > last), 0)
        return (*records[start:], *records[:start])[:limit]

    def advance_confirmed_acquire_intent_backfill_cursor(
        self, resolution_operation_id_value: str,
    ) -> None:
        operation = _identity(resolution_operation_id_value, "resolution operation id")
        payload = {
            "schema_version": "1.0.0",
            "last_resolution_operation_id": operation,
        }
        try:
            with self._records.begin() as uow:
                current = uow.read(
                    _BACKFILL_COORDINATION, _CONFIRMED_ACQUIRE_BACKFILL_CURSOR,
                )
                uow.put(
                    _BACKFILL_COORDINATION,
                    _CONFIRMED_ACQUIRE_BACKFILL_CURSOR,
                    payload,
                    expected_revision=current.revision if current is not None else 0,
                )
                uow.commit()
        except SQLiteUnitOfWorkConflict:
            # Another Core worker advanced the shared cursor.  Re-reading in
            # its next bounded pass is safe because planning is idempotent.
            return

    def record_confirmed_acquire_intent_backfill_failure(
        self,
        resolution_operation_id_value: str,
        *,
        reason_code: str,
        error: Exception,
    ) -> None:
        """Persist safe terminal evidence for an unrecoverable resolution.

        No exception text or confirmation payload is stored: both can contain
        attacker-controlled source metadata.  Pending human confirmation is
        deliberately not a failure and therefore is not recorded here.
        """

        operation = _identity(resolution_operation_id_value, "resolution operation id")
        if reason_code not in {
            "integrity_conflict", "malformed_resolution", "invalid_confirmation",
        }:
            raise ValueError("confirmed acquire backfill failure reason is invalid")
        payload = _canonical_payload({
            "schema_version": "1.0.0",
            "kind": "confirmed_acquire",
            "reason_code": reason_code,
            "error_type": type(error).__name__,
        })
        try:
            self._record_immutable(
                _CONFIRMED_ACQUIRE_BACKFILL_FAILURES,
                operation,
                payload,
            )
        except ExternalExtensionFactConflict:
            # Honor only an exact existing safe failure record.  A drifted
            # record must fail closed rather than permanently suppressing a
            # legitimate recovery candidate.
            existing = _required(
                self._records.read(_CONFIRMED_ACQUIRE_BACKFILL_FAILURES, operation),
                "confirmed acquire backfill failure",
            )
            _require_payload(existing, payload)

    def _confirmed_acquire_backfill_failed(self, resolution_operation_id_value: str) -> bool:
        operation = _identity(resolution_operation_id_value, "resolution operation id")
        record = self._records.read(
            _CONFIRMED_ACQUIRE_BACKFILL_FAILURES, operation,
        )
        if record is None:
            return False
        payload = _canonical_payload(record.payload)
        expected = {"schema_version", "kind", "reason_code", "error_type"}
        if (
            set(payload) != expected
            or payload.get("schema_version") != "1.0.0"
            or payload.get("kind") != "confirmed_acquire"
            or payload.get("reason_code") not in {
                "integrity_conflict", "malformed_resolution", "invalid_confirmation",
            }
            or not isinstance(payload.get("error_type"), str)
        ):
            raise ExternalExtensionFactConflict(
                "confirmed acquire backfill failure evidence drifted",
            )
        return True

    def confirmed_acquire_backfill_failure(
        self, resolution_operation_id_value: str,
    ) -> dict[str, object] | None:
        """Return redacted terminal evidence for support and architecture tests."""

        if not self._confirmed_acquire_backfill_failed(resolution_operation_id_value):
            return None
        operation = _identity(resolution_operation_id_value, "resolution operation id")
        record = self._records.read(
            _CONFIRMED_ACQUIRE_BACKFILL_FAILURES, operation,
        )
        assert record is not None
        return _canonical_payload(record.payload)

    def record_proposal(self, intent: InstallIntent, *, proposal_id: str) -> str:
        """Atomically freeze a local install proposal without creating an Effect."""

        proposal = _identity(proposal_id, "install proposal id")
        if intent.project_id is None or intent.source_spec is None:
            raise ExternalExtensionFactError("install proposal requires an exact project source")
        intent_payload = _canonical_payload(_intent_payload(intent))
        proposal_payload = _canonical_payload({
            "schema_version": "1.0.0",
            "proposal_ref": proposal_ref(proposal),
            "proposal_id": proposal,
            "intent_ref": intent_ref(intent.intent_id),
            "project_id": intent.project_id,
            "source_spec_digest": _source_spec_digest(intent.source_spec),
            "confirmation_ids": ["approve_initial_network_source"],
        })
        try:
            with self._records.begin() as uow:
                _put_immutable(uow, _INTENTS, intent.intent_id, intent_payload)
                _put_immutable(uow, _PROPOSALS, proposal, proposal_payload)
                uow.commit()
        except SQLiteUnitOfWorkConflict as error:
            raise ExternalExtensionFactConflict(str(error)) from error
        return proposal_ref(proposal)

    def load_proposal(self, reference: str) -> dict[str, object]:
        proposal = _reference_id(reference, _PROPOSAL_REF, "install proposal")
        record = _required(self._records.read(_PROPOSALS, proposal), "install proposal")
        payload = _canonical_payload(record.payload)
        expected = {
            "schema_version", "proposal_ref", "proposal_id", "intent_ref", "project_id",
            "source_spec_digest", "confirmation_ids",
        }
        if set(payload) != expected or payload["proposal_ref"] != proposal_ref(proposal) or payload["proposal_id"] != proposal:
            raise ExternalExtensionFactConflict("install proposal identity is invalid")
        intent = self.load_intent(str(payload["intent_ref"]))
        if intent.project_id != payload["project_id"] or intent.source_spec is None or (
            payload["source_spec_digest"] != _source_spec_digest(intent.source_spec)
        ):
            raise ExternalExtensionFactConflict("install proposal source drifted")
        if payload["confirmation_ids"] != ["approve_initial_network_source"]:
            raise ExternalExtensionFactConflict("install proposal confirmation contract is invalid")
        return payload

    def confirm_source_proposal(
        self,
        proposal_reference: str,
        *,
        confirmation_id: str,
        confirmation_ids: tuple[str, ...],
        actor: str,
        reason: str,
    ) -> str:
        proposal = self.load_proposal(proposal_reference)
        if tuple(confirmation_ids) != ("approve_initial_network_source",):
            raise ExternalExtensionFactError("source confirmation ids do not match proposal")
        identity = _identity(confirmation_id, "source confirmation id")
        payload = _canonical_payload({
            "schema_version": "1.0.0",
            "confirmation_ref": source_confirmation_ref(identity),
            "confirmation_id": identity,
            "proposal_ref": proposal_reference,
            "intent_ref": proposal["intent_ref"],
            "project_id": proposal["project_id"],
            "source_spec_digest": proposal["source_spec_digest"],
            "confirmation_ids": list(confirmation_ids),
            "actor": _safe_text(actor, "source confirmation actor", maximum=128),
            "reason": _safe_text(reason, "source confirmation reason", maximum=1000),
        })
        self._record_immutable(_SOURCE_CONFIRMATIONS, identity, payload)
        return str(payload["confirmation_ref"])

    def load_source_confirmation(self, reference: str) -> dict[str, object]:
        identity = _reference_id(reference, _SOURCE_CONFIRMATION_REF, "source confirmation")
        record = _required(self._records.read(_SOURCE_CONFIRMATIONS, identity), "source confirmation")
        payload = _canonical_payload(record.payload)
        expected = {
            "schema_version", "confirmation_ref", "confirmation_id", "proposal_ref", "intent_ref",
            "project_id", "source_spec_digest", "confirmation_ids", "actor", "reason",
        }
        if set(payload) != expected or payload["confirmation_ref"] != source_confirmation_ref(identity) or payload["confirmation_id"] != identity:
            raise ExternalExtensionFactConflict("source confirmation identity is invalid")
        proposal = self.load_proposal(str(payload["proposal_ref"]))
        for field in ("intent_ref", "project_id", "source_spec_digest"):
            if payload[field] != proposal[field]:
                raise ExternalExtensionFactConflict("source confirmation proposal drifted")
        if payload["confirmation_ids"] != ["approve_initial_network_source"]:
            raise ExternalExtensionFactConflict("source confirmation contract is invalid")
        return payload

    def confirm_resolved_revision(
        self,
        proposal_reference: str,
        resolution_reference: str,
        *,
        confirmation_id: str,
        confirmation_ids: tuple[str, ...],
        actor: str,
        reason: str,
    ) -> str:
        """Freeze approval for one exact revision discovered after source review."""

        proposal = self.load_proposal(proposal_reference)
        if tuple(confirmation_ids) != ("approve_resolved_revision_download",):
            raise ExternalExtensionFactError("revision confirmation ids do not match proposal")
        intent_id_value, source = self.load_resolution(resolution_reference)
        if proposal["intent_ref"] != intent_ref(intent_id_value):
            raise ExternalExtensionFactConflict("revision confirmation resolution drifted")
        if not isinstance(source.immutable_revision, str) or not source.immutable_revision:
            raise ExternalExtensionFactConflict("revision confirmation lacks an immutable revision")
        source_confirmation_reference = self._source_confirmation_for_proposal(
            proposal_reference,
        )
        identity = _identity(confirmation_id, "revision confirmation id")
        payload = _canonical_payload({
            "schema_version": "1.0.0",
            "confirmation_ref": revision_confirmation_ref(identity),
            "confirmation_id": identity,
            "proposal_ref": proposal_reference,
            "intent_ref": proposal["intent_ref"],
            "project_id": proposal["project_id"],
            "resolution_ref": resolution_reference,
            "immutable_revision": source.immutable_revision,
            "source_confirmation_ref": source_confirmation_reference,
            "confirmation_ids": list(confirmation_ids),
            "actor": _safe_text(actor, "revision confirmation actor", maximum=128),
            "reason": _safe_text(reason, "revision confirmation reason", maximum=1000),
        })
        self._record_immutable(_REVISION_CONFIRMATIONS, identity, payload)
        return str(payload["confirmation_ref"])

    def load_revision_confirmation(self, reference: str) -> dict[str, object]:
        identity = _reference_id(
            reference, _REVISION_CONFIRMATION_REF, "revision confirmation",
        )
        record = _required(
            self._records.read(_REVISION_CONFIRMATIONS, identity),
            "revision confirmation",
        )
        payload = _canonical_payload(record.payload)
        expected = {
            "schema_version", "confirmation_ref", "confirmation_id", "proposal_ref",
            "intent_ref", "project_id", "resolution_ref", "immutable_revision",
            "source_confirmation_ref", "confirmation_ids", "actor", "reason",
        }
        if (
            set(payload) != expected
            or payload["confirmation_ref"] != revision_confirmation_ref(identity)
            or payload["confirmation_id"] != identity
        ):
            raise ExternalExtensionFactConflict("revision confirmation identity is invalid")
        proposal = self.load_proposal(str(payload["proposal_ref"]))
        for field in ("intent_ref", "project_id"):
            if payload[field] != proposal[field]:
                raise ExternalExtensionFactConflict("revision confirmation proposal drifted")
        source_confirmation = self.load_source_confirmation(
            str(payload["source_confirmation_ref"]),
        )
        for field in ("proposal_ref", "intent_ref", "project_id", "source_spec_digest"):
            if source_confirmation[field] != proposal[field]:
                raise ExternalExtensionFactConflict("revision confirmation source ancestry drifted")
        intent_id_value, source = self.load_resolution(str(payload["resolution_ref"]))
        if (
            payload["intent_ref"] != intent_ref(intent_id_value)
            or payload["immutable_revision"] != source.immutable_revision
        ):
            raise ExternalExtensionFactConflict("revision confirmation resolution drifted")
        if payload["confirmation_ids"] != ["approve_resolved_revision_download"]:
            raise ExternalExtensionFactConflict("revision confirmation contract is invalid")
        return payload

    def revision_confirmation_for_resolution(self, resolution_reference: str) -> str | None:
        """Return one exact, fully validated revision approval, if present.

        A missing approval is a normal pending state.  Ambiguous or malformed
        records are integrity failures and callers must not infer acquisition.
        """

        operation = _reference_id(
            resolution_reference, _RESOLUTION_REF, "source resolution",
        )
        # Validate the resolution before inspecting evidence bound to it.
        self.load_resolution(resolution_ref(operation))
        matches = [
            record for record in self._records.list(_REVISION_CONFIRMATIONS)
            if record.payload.get("resolution_ref") == resolution_ref(operation)
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ExternalExtensionFactConflict(
                "floating source acquisition requires one exact revision confirmation",
            )
        reference = revision_confirmation_ref(matches[0].object_id)
        confirmation = self.load_revision_confirmation(reference)
        _intent_id, source = self.load_resolution(resolution_ref(operation))
        if confirmation.get("immutable_revision") != source.immutable_revision:
            raise ExternalExtensionFactConflict(
                "floating source revision confirmation drifted from resolution",
            )
        return reference

    def load_gate_confirmation(self, reference: str) -> dict[str, object]:
        """Load one immutable Gate input without allowing caller-supplied policy."""

        if isinstance(reference, str) and reference.startswith(_SOURCE_CONFIRMATION_REF):
            return {**self.load_source_confirmation(reference), "authorization_kind": "source"}
        if isinstance(reference, str) and reference.startswith(_REVISION_CONFIRMATION_REF):
            return {**self.load_revision_confirmation(reference), "authorization_kind": "revision"}
        if isinstance(reference, str) and reference.startswith(_LIFECYCLE_CONFIRMATION_REF):
            return {**self.load_lifecycle_confirmation(reference), "authorization_kind": "lifecycle"}
        raise ExternalExtensionFactError("external extension Gate confirmation reference is invalid")

    def confirm_lifecycle_action(
        self,
        *,
        confirmation_id: str,
        project_id: str,
        action: str,
        revision_ref: str,
        subject_ref: str,
        expected_state_revision: int,
        actor: str,
        reason: str,
    ) -> str:
        identity = _identity(confirmation_id, "lifecycle confirmation id")
        if action not in {"disable", "rollback", "uninstall"}:
            raise ExternalExtensionFactError("lifecycle confirmation action is invalid")
        if not isinstance(expected_state_revision, int) or isinstance(expected_state_revision, bool) or expected_state_revision < 0:
            raise ExternalExtensionFactError("lifecycle confirmation state revision is invalid")
        payload = _canonical_payload({
            "schema_version": "1.0.0",
            "confirmation_ref": lifecycle_confirmation_ref(identity),
            "confirmation_id": identity,
            "project_id": _safe_text(project_id, "lifecycle confirmation project", maximum=160),
            "action": action,
            "revision_ref": _safe_text(revision_ref, "lifecycle confirmation revision", maximum=512),
            "subject_ref": _safe_text(subject_ref, "lifecycle confirmation subject", maximum=512),
            "expected_state_revision": expected_state_revision,
            "actor": _safe_text(actor, "lifecycle confirmation actor", maximum=160),
            "reason": _safe_text(reason, "lifecycle confirmation reason", maximum=1000),
        })
        self._record_immutable(_LIFECYCLE_CONFIRMATIONS, identity, payload)
        return lifecycle_confirmation_ref(identity)

    def load_lifecycle_confirmation(self, reference: str) -> dict[str, object]:
        identity = _reference_id(
            reference, _LIFECYCLE_CONFIRMATION_REF, "lifecycle confirmation",
        )
        record = _required(
            self._records.read(_LIFECYCLE_CONFIRMATIONS, identity),
            "lifecycle confirmation",
        )
        payload = _canonical_payload(record.payload)
        expected = {
            "schema_version", "confirmation_ref", "confirmation_id", "project_id",
            "action", "revision_ref", "subject_ref", "expected_state_revision",
            "actor", "reason",
        }
        if (
            set(payload) != expected
            or payload.get("schema_version") != "1.0.0"
            or payload.get("confirmation_ref") != lifecycle_confirmation_ref(identity)
            or payload.get("confirmation_id") != identity
            or payload.get("action") not in {"disable", "rollback", "uninstall"}
        ):
            raise ExternalExtensionFactConflict("lifecycle confirmation identity is invalid")
        return payload

    def confirmation_authorizes_subject(
        self, confirmation: Mapping[str, object], *, subject_ref: str,
    ) -> bool:
        """Check that an immutable source approval belongs to one Effect subject."""

        if confirmation.get("authorization_kind") == "revision":
            return confirmation.get("resolution_ref") == subject_ref

        intent_reference = confirmation.get("intent_ref")
        if not isinstance(intent_reference, str):
            return False
        if subject_ref == intent_reference:
            return True
        try:
            if subject_ref.startswith(_RESOLUTION_REF):
                intent_id_value, _source = self.load_resolution(subject_ref)
                return intent_reference == intent_ref(intent_id_value)
            if subject_ref.startswith(_INTAKE_REF):
                intake = self.load_intake(subject_ref)
                return intent_reference == intent_ref(str(intake.get("intent_id")))
        except ExternalExtensionFactError:
            return False
        return False

    def source_confirmation_for_intent(self, intent_reference: str) -> str:
        """Find the unique immutable source approval bound to one install intent."""

        self.load_intent(intent_reference)
        matches = [
            record for record in self._records.list(_SOURCE_CONFIRMATIONS)
            if record.payload.get("intent_ref") == intent_reference
        ]
        if len(matches) != 1:
            raise ExternalExtensionFactConflict("install intent lacks a unique source confirmation")
        reference = source_confirmation_ref(matches[0].object_id)
        confirmation = self.load_source_confirmation(reference)
        if confirmation["intent_ref"] != intent_reference:
            raise ExternalExtensionFactConflict("source confirmation intent drifted")
        return reference

    def _source_confirmation_for_proposal(self, proposal_reference: str) -> str:
        """Return the one validated initial approval anchoring a revision review."""

        proposal = self.load_proposal(proposal_reference)
        matches = [
            record for record in self._records.list(_SOURCE_CONFIRMATIONS)
            if record.payload.get("proposal_ref") == proposal_reference
        ]
        if len(matches) != 1:
            raise ExternalExtensionFactConflict(
                "revision confirmation requires a unique initial source confirmation"
            )
        reference = source_confirmation_ref(matches[0].object_id)
        confirmation = self.load_source_confirmation(reference)
        for field in ("proposal_ref", "intent_ref", "project_id", "source_spec_digest"):
            if confirmation[field] != proposal[field]:
                raise ExternalExtensionFactConflict(
                    "revision confirmation source ancestry drifted"
                )
        return reference

    def load_intent(self, reference: str) -> InstallIntent:
        identity = _reference_id(reference, _INTENT_REF, "install intent")
        record = _required(self._records.read(_INTENTS, identity), "install intent")
        return _intent_from_payload(record.payload)

    def record_resolution_observation(
        self,
        *,
        operation_id: str,
        intent_reference: str,
        source: ResolvedSource,
    ) -> str:
        operation = _identity(operation_id, "resolution operation id")
        intent = self.load_intent(intent_reference)
        _require_resolved_source(intent, operation, source)
        reference = _OBSERVATION_REF + operation
        payload = _canonical_payload(
            {
                "schema_version": "1.0.0",
                "operation_id": operation,
                "intent_ref": intent_reference,
                "source": asdict(source),
                "observation_ref": reference,
            }
        )
        self._record_immutable(_RESOLUTION_OBSERVATIONS, operation, payload)
        return reference

    def resolution_observation(
        self,
        *,
        operation_id: str,
        intent_reference: str,
    ) -> ResolvedSource | None:
        operation = _identity(operation_id, "resolution operation id")
        record = self._records.read(_RESOLUTION_OBSERVATIONS, operation)
        if record is None:
            return None
        if (
            record.payload.get("operation_id") != operation
            or record.payload.get("intent_ref") != intent_reference
            or record.payload.get("observation_ref") != _OBSERVATION_REF + operation
        ):
            raise ExternalExtensionFactConflict("source resolution observation identity is invalid")
        source = _resolved_source_from(record.payload.get("source"), "source resolution observation")
        intent = self.load_intent(intent_reference)
        _require_resolved_source(intent, operation, source)
        return source

    def record_resolution(
        self,
        *,
        operation_id: str,
        intent_reference: str,
        source: ResolvedSource,
    ) -> str:
        operation = _identity(operation_id, "resolution operation id")
        intent = self.load_intent(intent_reference)
        _require_resolved_source(intent, operation, source)
        observation = self.resolution_observation(
            operation_id=operation,
            intent_reference=intent_reference,
        )
        if observation is None or observation != source:
            raise ExternalExtensionFactConflict("source resolution lacks matching durable observation")
        payload = _canonical_payload(
            {
                "schema_version": "1.0.0",
                "operation_id": operation,
                "intent_id": intent.intent_id,
                "intent_ref": intent_reference,
                "source": asdict(source),
                "receipt_ref": resolution_ref(operation),
            }
        )
        self._record_immutable(_RESOLUTIONS, operation, payload)
        return str(payload["receipt_ref"])

    def load_resolution(self, reference: str) -> tuple[str, ResolvedSource]:
        operation = _reference_id(reference, _RESOLUTION_REF, "source resolution")
        record = _required(self._records.read(_RESOLUTIONS, operation), "source resolution")
        if (
            record.payload.get("operation_id") != operation
            or record.payload.get("receipt_ref") != resolution_ref(operation)
        ):
            raise ExternalExtensionFactConflict("source resolution identity is invalid")
        resolved = _resolved_source_from(record.payload.get("source"), "source resolution")
        intent_id_value = record.payload.get("intent_id")
        if not isinstance(intent_id_value, str):
            raise ExternalExtensionFactConflict("source resolution intent identity is invalid")
        expected_intent_ref = intent_ref(intent_id_value)
        if record.payload.get("intent_ref") != expected_intent_ref:
            raise ExternalExtensionFactConflict("source resolution intent identity is invalid")
        intent = self.load_intent(expected_intent_ref)
        _require_resolved_source(intent, operation, resolved)
        return intent_id_value, resolved

    def resolution_reference(self, operation_id: str) -> str | None:
        operation = _identity(operation_id, "resolution operation id")
        return resolution_ref(operation) if self._records.read(_RESOLUTIONS, operation) is not None else None

    def resolution_receipt(self, operation_id: str) -> str | None:
        operation = _identity(operation_id, "resolution operation id")
        record = self._records.read(_RESOLUTIONS, operation)
        return (
            _receipt_from(record, "source resolution", expected=resolution_ref(operation))
            if record is not None
            else None
        )

    def record_intake(
        self,
        *,
        operation_id: str,
        resolution_reference: str,
        manifest: ExtensionManifest | None,
        review_plan: ExtensionReviewPlan | None,
        quarantine_code: str | None = None,
    ) -> str:
        operation = _identity(operation_id, "intake operation id")
        intent_id_value, source = self.load_resolution(resolution_reference)
        artifact_evidence = self.artifact_evidence(operation_id=operation, source=source)
        if artifact_evidence is None:
            raise ExternalExtensionFactConflict("intake lacks durable quarantine evidence")
        success = manifest is not None and review_plan is not None
        if success == (quarantine_code is not None):
            raise ExternalExtensionFactError("intake requires exactly one manifest plan or quarantine code")
        if success:
            if manifest.source != source or review_plan.artifact_ref != source.artifact_ref:
                raise ExternalExtensionFactConflict("intake facts drifted from the resolved source")
            result: dict[str, object] = {
                "manifest": asdict(manifest),
                "review_plan": asdict(review_plan),
                "projection": review_plan.install_projection,
            }
        else:
            code = _safe_code(quarantine_code)
            result = {
                "quarantine_code": code,
                "projection": "quarantined",
            }
        payload = _canonical_payload(
            {
                "schema_version": "1.0.0",
                "operation_id": operation,
                "intent_id": intent_id_value,
                "resolution_ref": resolution_reference,
                "artifact_ref": source.artifact_ref,
                "artifact_receipt_ref": artifact_evidence.artifact_receipt_ref,
                "artifact_content_sha256": artifact_evidence.content_sha256,
                "artifact_file_count": artifact_evidence.file_count,
                "artifact_total_bytes": artifact_evidence.total_bytes,
                "result": result,
                "receipt_ref": intake_receipt_ref(operation),
            }
        )
        self._record_immutable(_INTAKES, operation, payload)
        return str(payload["receipt_ref"])

    def commit_artifact(
        self,
        *,
        operation_id: str,
        source: ResolvedSource,
        inventory: ArtifactInventory,
    ) -> ArtifactEvidence:
        return self._artifacts.commit(operation_id, source, inventory)

    def artifact_evidence(
        self,
        *,
        operation_id: str,
        source: ResolvedSource,
    ) -> ArtifactEvidence | None:
        return self._artifacts.probe(operation_id, source)

    def load_verified_intake_artifact(self, intake_ref: str) -> ArtifactEvidence:
        """Load only final quarantine evidence exactly bound by an immutable intake."""
        operation = _reference_id(intake_ref, _INTAKE_REF, "extension intake")
        intake = self.load_intake(intake_ref)
        resolution_reference = intake.get("resolution_ref")
        if not isinstance(resolution_reference, str):
            raise ExternalExtensionFactConflict("extension intake resolution reference is invalid")
        _intent_id, source = self.load_resolution(resolution_reference)
        try:
            evidence = self._artifacts.load_final(operation, source)
        except ValueError as error:
            raise ExternalExtensionFactConflict("extension intake artifact evidence is invalid") from error
        if evidence is None:
            raise ExternalExtensionFactConflict("extension intake final artifact evidence is missing")
        expected = (
            intake.get("artifact_ref"),
            intake.get("artifact_receipt_ref"),
            intake.get("artifact_content_sha256"),
            intake.get("artifact_file_count"),
            intake.get("artifact_total_bytes"),
        )
        actual = (
            evidence.artifact_ref,
            evidence.artifact_receipt_ref,
            evidence.content_sha256,
            evidence.file_count,
            evidence.total_bytes,
        )
        if actual != expected:
            raise ExternalExtensionFactConflict("extension intake final artifact evidence drifted")
        return evidence

    def intake_receipt(self, operation_id: str) -> str | None:
        operation = _identity(operation_id, "intake operation id")
        record = self._records.read(_INTAKES, operation)
        return (
            _receipt_from(record, "extension intake", expected=intake_receipt_ref(operation))
            if record is not None
            else None
        )

    def load_intake(self, reference: str) -> dict[str, object]:
        operation = _reference_id(reference, _INTAKE_REF, "extension intake")
        record = _required(self._records.read(_INTAKES, operation), "extension intake")
        _receipt_from(record, "extension intake", expected=intake_receipt_ref(operation))
        if record.payload.get("operation_id") != operation:
            raise ExternalExtensionFactConflict("extension intake identity is invalid")
        resolution_reference = record.payload.get("resolution_ref")
        if not isinstance(resolution_reference, str):
            raise ExternalExtensionFactConflict("extension intake resolution reference is invalid")
        _intent_id, source = self.load_resolution(resolution_reference)
        if record.payload.get("artifact_ref") != source.artifact_ref:
            raise ExternalExtensionFactConflict("extension intake artifact reference drifted")
        return _canonical_payload(record.payload)

    def intake_snapshot(self, operation_id: str) -> dict[str, object] | None:
        operation = _identity(operation_id, "intake operation id")
        reference = intake_receipt_ref(operation)
        return self.load_intake(reference) if self._records.read(_INTAKES, operation) is not None else None

    def _record_immutable(self, collection: str, object_id: str, payload: Mapping[str, object]) -> None:
        try:
            with self._records.begin() as uow:
                _put_immutable(uow, collection, object_id, payload)
                uow.commit()
        except SQLiteUnitOfWorkConflict as error:
            raise ExternalExtensionFactConflict(str(error)) from error


def intent_ref(intent_id_value: str) -> str:
    return _INTENT_REF + _identity(intent_id_value, "install intent id")


def proposal_ref(proposal_id: str) -> str:
    return _PROPOSAL_REF + _identity(proposal_id, "install proposal id")


def source_confirmation_ref(confirmation_id: str) -> str:
    return _SOURCE_CONFIRMATION_REF + _identity(confirmation_id, "source confirmation id")


def revision_confirmation_ref(confirmation_id: str) -> str:
    return _REVISION_CONFIRMATION_REF + _identity(
        confirmation_id, "revision confirmation id",
    )


def lifecycle_confirmation_ref(confirmation_id: str) -> str:
    return _LIFECYCLE_CONFIRMATION_REF + _identity(
        confirmation_id, "lifecycle confirmation id",
    )


def resolution_ref(operation_id: str) -> str:
    return _RESOLUTION_REF + _identity(operation_id, "resolution operation id")


def resolution_operation_id(reference: str) -> str:
    return _reference_id(reference, _RESOLUTION_REF, "source resolution")


def resolution_receipt_ref(operation_id: str) -> str:
    return resolution_ref(operation_id)


def artifact_ref(intent_id_value: str, operation_id: str) -> str:
    return (
        _ARTIFACT_REF
        + _identity(intent_id_value, "install intent id")
        + "/"
        + _identity(operation_id, "resolution operation id")
    )


def intake_receipt_ref(operation_id: str) -> str:
    return _INTAKE_REF + _identity(operation_id, "intake operation id")


def _intent_payload(intent: InstallIntent) -> dict[str, object]:
    return {
        "schema_version": intent.schema_version,
        "intent_id": intent.intent_id,
        "kind_hint": intent.kind_hint,
        "source_spec": asdict(intent.source_spec) if intent.source_spec is not None else None,
        "search_term": intent.search_term,
        "project_id": intent.project_id,
        "disposition": intent.disposition,
        "reason_codes": list(intent.reason_codes),
    }


def _source_spec_digest(source: SourceSpec) -> str:
    return hashlib.sha256(
        json.dumps(asdict(source), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _intent_from_payload(payload: Mapping[str, object]) -> InstallIntent:
    source_payload = payload.get("source_spec")
    try:
        source = SourceSpec(**dict(source_payload)) if isinstance(source_payload, Mapping) else None
        reasons = payload.get("reason_codes")
        if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
            raise TypeError("reason codes are invalid")
        return InstallIntent(
            intent_id=str(payload["intent_id"]),
            kind_hint=str(payload["kind_hint"]),
            source_spec=source,
            search_term=payload.get("search_term") if isinstance(payload.get("search_term"), str) else None,
            project_id=payload.get("project_id") if isinstance(payload.get("project_id"), str) else None,
            disposition=str(payload["disposition"]),
            reason_codes=tuple(reasons),
            schema_version=str(payload["schema_version"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ExternalExtensionFactConflict("stored install intent is invalid") from error


def _resolved_source_from(value: object, label: str) -> ResolvedSource:
    if not isinstance(value, Mapping):
        raise ExternalExtensionFactConflict(f"{label} payload is invalid")
    try:
        return ResolvedSource(**dict(value))
    except (TypeError, ValueError) as error:
        raise ExternalExtensionFactConflict(f"{label} payload is invalid") from error


def _require_resolved_source(intent: InstallIntent, operation: str, source: ResolvedSource) -> None:
    if intent.source_spec is None:
        raise ExternalExtensionFactError("unresolved search intent cannot record a source resolution")
    if source.source_kind != intent.source_spec.kind or source.canonical_locator != intent.source_spec.locator:
        raise ExternalExtensionFactConflict("resolved source drifted from the install intent")
    requested_ref = intent.source_spec.requested_ref
    if isinstance(requested_ref, str) and _COMMIT.fullmatch(requested_ref):
        if source.immutable_revision != requested_ref:
            raise ExternalExtensionFactConflict(
                "resolved immutable revision does not match the fixed source revision"
            )
    if not source.is_immutable:
        raise ExternalExtensionFactError("source resolution must freeze an immutable revision")
    if source.acquisition_contract_revision != "1":
        raise ExternalExtensionFactConflict("source acquisition contract revision is unsupported")
    if source.trust_tier != "untrusted":
        raise ExternalExtensionFactConflict("source trust tier cannot be self-attested by a resolver")
    if source.artifact_ref != artifact_ref(intent.intent_id, operation):
        raise ExternalExtensionFactConflict("resolved source artifact reference drifted from the operation")


def _put_immutable(uow, collection: str, object_id: str, payload: Mapping[str, object]) -> None:
    normalized = _canonical_payload(payload)
    existing = uow.read(collection, object_id)
    if existing is None:
        uow.put(collection, object_id, normalized, expected_revision=0)
        return
    _require_payload(existing, normalized)


def _require_payload(record: SQLiteStructuredRecord, payload: Mapping[str, object]) -> None:
    if _canonical_payload(record.payload) != _canonical_payload(payload):
        raise ExternalExtensionFactConflict("immutable external extension fact drifted")


def _canonical_payload(payload: Mapping[str, object]) -> dict[str, object]:
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        value = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ExternalExtensionFactError("external extension fact is not serializable") from error
    if not isinstance(value, dict):
        raise ExternalExtensionFactError("external extension fact must be an object")
    return value


def _required(record: SQLiteStructuredRecord | None, label: str) -> SQLiteStructuredRecord:
    if record is None:
        raise ExternalExtensionFactError(f"{label} is missing")
    return record


def _receipt_from(record: SQLiteStructuredRecord, label: str, *, expected: str) -> str:
    receipt = record.payload.get("receipt_ref")
    if receipt != expected:
        raise ExternalExtensionFactConflict(f"{label} receipt is invalid")
    return expected


def _reference_id(reference: str, prefix: str, label: str) -> str:
    if not isinstance(reference, str) or not reference.startswith(prefix):
        raise ExternalExtensionFactError(f"{label} reference is invalid")
    suffix = reference[len(prefix):]
    if "/" in suffix:
        raise ExternalExtensionFactError(f"{label} reference is invalid")
    return _identity(suffix, f"{label} id")


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ExternalExtensionFactError(f"{label} is invalid")
    return value


def _command_id(value: object) -> str:
    if not isinstance(value, str) or not _COMMAND_ID.fullmatch(value):
        raise ExternalExtensionFactError("install command id is invalid")
    return value


def _safe_code(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9._-]{1,95}", value):
        raise ExternalExtensionFactError("quarantine code is invalid")
    return value


def _derived_command_id(prefix: str, semantic_identity: str) -> str:
    """Canonical command id shared with the startup-only intake composer."""

    if prefix not in {"resolve-command", "acquire-command"}:
        raise ValueError("external extension command prefix is invalid")
    identity = _identity(semantic_identity, "external extension command semantic identity")
    digest = hashlib.sha256(f"{prefix}\0{identity}".encode("utf-8")).hexdigest()
    return f"external-extension-{prefix}-{digest[:32]}"


def _safe_text(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(
        character in value for character in ("\x00", "\r", "\n")
    ):
        raise ExternalExtensionFactError(f"{label} is invalid")
    return value.strip()
