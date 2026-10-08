from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .sqlite_uow import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


_COLLECTION = "external_project_skill_apply_operations"
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STATES = {"prepared", "skill_applied", "finalized"}
_TRANSITIONS = {"prepared": "skill_applied", "skill_applied": "finalized"}
_AUTHORITIES = {"json:object-store-v1", "sqlite:structured-records-v1"}


class ExternalProjectSkillApplySagaError(ValueError):
    """Raised when durable Project Skill apply evidence is invalid."""


class ExternalProjectSkillApplySagaConflict(ExternalProjectSkillApplySagaError):
    """Raised for evidence drift, stale CAS, or illegal transitions."""


@dataclass(frozen=True, slots=True)
class ExternalProjectSkillApplyEvidence:
    namespace_id: str
    project_id: str
    project_skill_id: str
    base_revision: int
    payload_sha256: str
    project_skill_authority_identity: str


@dataclass(frozen=True, slots=True)
class ExternalProjectSkillApplyOperation:
    operation_id: str
    state: str
    revision: int
    evidence: ExternalProjectSkillApplyEvidence
    applied_skill_revision: int | None
    created_at: str
    updated_at: str


class SQLiteExternalProjectSkillApplySagaStore:
    """Durable CAS state isolated from Document apply operations."""

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        self._records = records

    def prepare(self, *, operation_id: str, evidence: ExternalProjectSkillApplyEvidence, now: str | None = None):
        _segment("operation_id", operation_id)
        _evidence(evidence)
        current = self.get(operation_id)
        if current is not None:
            if current.evidence != evidence:
                raise ExternalProjectSkillApplySagaConflict("project skill apply operation evidence drifted")
            return current
        timestamp = now or _utc_now()
        payload = _payload(operation_id, "prepared", evidence, None, timestamp, timestamp)
        try:
            with self._records.begin() as uow:
                record = uow.put(_COLLECTION, operation_id, payload, expected_revision=0)
                uow.commit()
        except SQLiteUnitOfWorkConflict as exc:
            concurrent = self.get(operation_id)
            if concurrent is not None and concurrent.evidence == evidence:
                return concurrent
            raise ExternalProjectSkillApplySagaConflict("project skill apply prepare conflicted") from exc
        return _operation(record.payload, record.revision)

    def get(self, operation_id: str):
        _segment("operation_id", operation_id)
        record = self._records.read(_COLLECTION, operation_id)
        return _operation(record.payload, record.revision) if record is not None else None

    def mark_skill_applied(self, operation_id: str, *, expected_revision: int, applied_skill_revision: int, now: str | None = None):
        _positive("applied_skill_revision", applied_skill_revision)
        return self._transition(operation_id, expected_revision, "skill_applied", applied_skill_revision, now)

    def finalize(self, operation_id: str, *, expected_revision: int, now: str | None = None):
        return self._transition(operation_id, expected_revision, "finalized", None, now)

    def list_recoverable(self):
        return tuple(
            operation
            for record in self._records.list(_COLLECTION)
            if (operation := _operation(record.payload, record.revision)).state != "finalized"
        )

    def _transition(self, operation_id, expected_revision, to_state, applied_revision, now):
        _positive("expected_revision", expected_revision)
        current = self.get(operation_id)
        if current is None:
            raise ExternalProjectSkillApplySagaError("project skill apply operation does not exist")
        if current.revision != expected_revision:
            raise ExternalProjectSkillApplySagaConflict(f"expected revision {expected_revision}, found {current.revision}")
        if _TRANSITIONS.get(current.state) != to_state:
            raise ExternalProjectSkillApplySagaConflict(f"illegal project skill apply transition {current.state} -> {to_state}")
        next_applied = applied_revision if to_state == "skill_applied" else current.applied_skill_revision
        payload = _payload(current.operation_id, to_state, current.evidence, next_applied, current.created_at, now or _utc_now())
        try:
            with self._records.begin() as uow:
                record = uow.put(_COLLECTION, operation_id, payload, expected_revision=expected_revision)
                uow.commit()
        except SQLiteUnitOfWorkConflict as exc:
            raise ExternalProjectSkillApplySagaConflict("project skill apply transition conflicted") from exc
        return _operation(record.payload, record.revision)


def _payload(operation_id, state, evidence, applied_revision, created_at, updated_at):
    return {
        "operation_id": operation_id, "state": state, "namespace_id": evidence.namespace_id,
        "project_id": evidence.project_id, "project_skill_id": evidence.project_skill_id,
        "base_revision": evidence.base_revision, "payload_sha256": evidence.payload_sha256,
        "project_skill_authority_identity": evidence.project_skill_authority_identity,
        "applied_skill_revision": applied_revision, "created_at": created_at, "updated_at": updated_at,
    }


def _operation(payload, revision):
    evidence = ExternalProjectSkillApplyEvidence(
        payload.get("namespace_id"), payload.get("project_id"), payload.get("project_skill_id"),
        payload.get("base_revision"), payload.get("payload_sha256"), payload.get("project_skill_authority_identity"),
    )
    operation_id, state, applied = payload.get("operation_id"), payload.get("state"), payload.get("applied_skill_revision")
    created_at, updated_at = payload.get("created_at"), payload.get("updated_at")
    _segment("operation_id", operation_id); _evidence(evidence); _positive("record revision", revision)
    if state not in _STATES or (state == "prepared" and applied is not None):
        raise ExternalProjectSkillApplySagaError("project skill apply operation state is invalid")
    if state != "prepared": _positive("applied_skill_revision", applied)
    if not isinstance(created_at, str) or not created_at or not isinstance(updated_at, str) or not updated_at:
        raise ExternalProjectSkillApplySagaError("project skill apply operation timestamps are invalid")
    return ExternalProjectSkillApplyOperation(operation_id, state, revision, evidence, applied, created_at, updated_at)


def _evidence(value):
    if not isinstance(value, ExternalProjectSkillApplyEvidence):
        raise ExternalProjectSkillApplySagaError("project skill apply evidence is invalid")
    _segment("namespace_id", value.namespace_id); _segment("project_id", value.project_id); _segment("project_skill_id", value.project_skill_id)
    _positive("base_revision", value.base_revision)
    if not isinstance(value.payload_sha256, str) or not _SHA256.fullmatch(value.payload_sha256):
        raise ExternalProjectSkillApplySagaError("payload_sha256 is invalid")
    if value.project_skill_authority_identity not in _AUTHORITIES:
        raise ExternalProjectSkillApplySagaError("project_skill_authority_identity is invalid")


def _segment(label, value):
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise ExternalProjectSkillApplySagaError(f"{label} is invalid")


def _positive(label, value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ExternalProjectSkillApplySagaError(f"{label} must be a positive integer")


def _utc_now():
    return datetime.now(timezone.utc).isoformat()
