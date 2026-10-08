from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .sqlite_uow import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


_COLLECTION = "external_apply_operations"
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STATES = {"prepared", "document_applied", "finalized"}
_TRANSITIONS = {"prepared": "document_applied", "document_applied": "finalized"}


class ExternalApplySagaError(ValueError):
    """Raised when an external apply operation violates its durable contract."""


class ExternalApplySagaConflict(ExternalApplySagaError):
    """Raised for stale revisions, duplicate drift, or illegal transitions."""


@dataclass(frozen=True, slots=True)
class ExternalApplyEvidence:
    namespace_id: str
    document_id: str
    base_revision: int
    payload_sha256: str
    document_authority_identity: str = "unbound:legacy"


@dataclass(frozen=True, slots=True)
class ExternalApplyOperation:
    operation_id: str
    state: str
    revision: int
    evidence: ExternalApplyEvidence
    applied_document_revision: int | None
    created_at: str
    updated_at: str


class SQLiteExternalApplySagaStore:
    """Durable CAS state for recovering cross-authority Document apply operations."""

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        self._records = records

    def prepare(
        self,
        *,
        operation_id: str,
        evidence: ExternalApplyEvidence,
        now: str | None = None,
    ) -> ExternalApplyOperation:
        _require_segment("operation_id", operation_id)
        _require_evidence(evidence)
        current = self.get(operation_id)
        if current is not None:
            if current.evidence != evidence:
                raise ExternalApplySagaConflict("external apply operation evidence drifted")
            return current
        timestamp = now or _utc_now()
        payload = {
            "operation_id": operation_id,
            "state": "prepared",
            "namespace_id": evidence.namespace_id,
            "document_id": evidence.document_id,
            "base_revision": evidence.base_revision,
            "payload_sha256": evidence.payload_sha256,
            "document_authority_identity": evidence.document_authority_identity,
            "applied_document_revision": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        try:
            with self._records.begin() as uow:
                record = uow.put(_COLLECTION, operation_id, payload, expected_revision=0)
                uow.commit()
        except SQLiteUnitOfWorkConflict as exc:
            concurrent = self.get(operation_id)
            if concurrent is not None and concurrent.evidence == evidence:
                return concurrent
            raise ExternalApplySagaConflict("external apply operation prepare conflicted") from exc
        return _operation(record.payload, record.revision)

    def get(self, operation_id: str) -> ExternalApplyOperation | None:
        _require_segment("operation_id", operation_id)
        record = self._records.read(_COLLECTION, operation_id)
        return _operation(record.payload, record.revision) if record is not None else None

    def mark_document_applied(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        applied_document_revision: int,
        now: str | None = None,
    ) -> ExternalApplyOperation:
        _require_positive_revision("applied_document_revision", applied_document_revision)
        return self._transition(
            operation_id,
            expected_revision=expected_revision,
            to_state="document_applied",
            applied_document_revision=applied_document_revision,
            now=now,
        )

    def finalize(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        now: str | None = None,
    ) -> ExternalApplyOperation:
        return self._transition(
            operation_id,
            expected_revision=expected_revision,
            to_state="finalized",
            applied_document_revision=None,
            now=now,
        )

    def list_recoverable(self) -> tuple[ExternalApplyOperation, ...]:
        return tuple(
            operation
            for record in self._records.list(_COLLECTION)
            if (operation := _operation(record.payload, record.revision)).state != "finalized"
        )

    def _transition(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        to_state: str,
        applied_document_revision: int | None,
        now: str | None,
    ) -> ExternalApplyOperation:
        _require_segment("operation_id", operation_id)
        _require_positive_revision("expected_revision", expected_revision)
        current = self.get(operation_id)
        if current is None:
            raise ExternalApplySagaError("external apply operation does not exist")
        if current.revision != expected_revision:
            raise ExternalApplySagaConflict(
                f"expected revision {expected_revision}, found {current.revision}"
            )
        if _TRANSITIONS.get(current.state) != to_state:
            raise ExternalApplySagaConflict(
                f"illegal external apply transition {current.state} -> {to_state}"
            )
        next_applied_revision = (
            applied_document_revision
            if to_state == "document_applied"
            else current.applied_document_revision
        )
        timestamp = now or _utc_now()
        payload = {
            "operation_id": current.operation_id,
            "state": to_state,
            "namespace_id": current.evidence.namespace_id,
            "document_id": current.evidence.document_id,
            "base_revision": current.evidence.base_revision,
            "payload_sha256": current.evidence.payload_sha256,
            "document_authority_identity": current.evidence.document_authority_identity,
            "applied_document_revision": next_applied_revision,
            "created_at": current.created_at,
            "updated_at": timestamp,
        }
        try:
            with self._records.begin() as uow:
                record = uow.put(
                    _COLLECTION,
                    operation_id,
                    payload,
                    expected_revision=expected_revision,
                )
                uow.commit()
        except SQLiteUnitOfWorkConflict as exc:
            raise ExternalApplySagaConflict("external apply operation transition conflicted") from exc
        return _operation(record.payload, record.revision)


def _operation(payload, revision: int) -> ExternalApplyOperation:
    operation_id = payload.get("operation_id")
    state = payload.get("state")
    evidence = ExternalApplyEvidence(
        namespace_id=payload.get("namespace_id"),
        document_id=payload.get("document_id"),
        base_revision=payload.get("base_revision"),
        payload_sha256=payload.get("payload_sha256"),
        document_authority_identity=payload.get("document_authority_identity") or "unbound:legacy",
    )
    applied_revision = payload.get("applied_document_revision")
    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")
    _require_segment("operation_id", operation_id)
    _require_evidence(evidence)
    if state not in _STATES:
        raise ExternalApplySagaError("external apply operation state is invalid")
    if state == "prepared" and applied_revision is not None:
        raise ExternalApplySagaError("prepared operation cannot have an applied revision")
    if state != "prepared":
        _require_positive_revision("applied_document_revision", applied_revision)
    if not isinstance(created_at, str) or not created_at or not isinstance(updated_at, str) or not updated_at:
        raise ExternalApplySagaError("external apply operation timestamps are invalid")
    _require_positive_revision("record revision", revision)
    return ExternalApplyOperation(
        operation_id=operation_id,
        state=state,
        revision=revision,
        evidence=evidence,
        applied_document_revision=applied_revision,
        created_at=created_at,
        updated_at=updated_at,
    )


def _require_evidence(evidence: ExternalApplyEvidence) -> None:
    if not isinstance(evidence, ExternalApplyEvidence):
        raise ExternalApplySagaError("external apply evidence is invalid")
    _require_segment("namespace_id", evidence.namespace_id)
    _require_segment("document_id", evidence.document_id)
    _require_positive_revision("base_revision", evidence.base_revision)
    if not isinstance(evidence.payload_sha256, str) or not _SHA256.fullmatch(evidence.payload_sha256):
        raise ExternalApplySagaError("payload_sha256 is invalid")
    if evidence.document_authority_identity not in {
        "json:object-store-v1",
        "sqlite:structured-records-v1",
        "unbound:legacy",
    }:
        raise ExternalApplySagaError("document_authority_identity is invalid")


def _require_segment(label: str, value: str) -> None:
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise ExternalApplySagaError(f"{label} is invalid")


def _require_positive_revision(label: str, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ExternalApplySagaError(f"{label} must be a positive integer")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
