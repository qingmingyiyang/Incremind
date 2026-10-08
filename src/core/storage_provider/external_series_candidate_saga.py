from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .sqlite_uow import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


_COLLECTION = "external_series_candidate_operations"
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRANSITIONS = {"prepared": "candidate_created", "candidate_created": "finalized"}


class ExternalSeriesCandidateSagaError(ValueError):
    pass


class ExternalSeriesCandidateSagaConflict(ExternalSeriesCandidateSagaError):
    pass


@dataclass(frozen=True, slots=True)
class ExternalSeriesCandidateEvidence:
    namespace_id: str
    series_id: str
    series_memory_id: str
    candidate_id: str
    base_object_revision: int
    base_series_revision: int
    payload_sha256: str
    authority_identity: str = "json:object-store-v1"


@dataclass(frozen=True, slots=True)
class ExternalSeriesCandidateOperation:
    operation_id: str
    state: str
    revision: int
    evidence: ExternalSeriesCandidateEvidence
    candidate_revision: int | None
    created_at: str
    updated_at: str


class SQLiteExternalSeriesCandidateSagaStore:
    """Durable candidate-creation operation store, separate from legacy direct apply."""

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        self._records = records

    def prepare(
        self,
        *,
        operation_id: str,
        evidence: ExternalSeriesCandidateEvidence,
        now: str | None = None,
    ) -> ExternalSeriesCandidateOperation:
        _segment("operation_id", operation_id)
        _evidence(evidence)
        current = self.get(operation_id)
        if current is not None:
            if current.evidence != evidence:
                raise ExternalSeriesCandidateSagaConflict("series candidate operation evidence drifted")
            return current
        timestamp = now or _utc_now()
        try:
            with self._records.begin() as transaction:
                record = transaction.put(
                    _COLLECTION,
                    operation_id,
                    _payload(operation_id, "prepared", evidence, None, timestamp, timestamp),
                    expected_revision=0,
                )
                transaction.commit()
        except SQLiteUnitOfWorkConflict as exc:
            concurrent = self.get(operation_id)
            if concurrent is not None and concurrent.evidence == evidence:
                return concurrent
            raise ExternalSeriesCandidateSagaConflict("series candidate prepare conflicted") from exc
        return _operation(record.payload, record.revision)

    def get(self, operation_id: str) -> ExternalSeriesCandidateOperation | None:
        _segment("operation_id", operation_id)
        record = self._records.read(_COLLECTION, operation_id)
        return _operation(record.payload, record.revision) if record is not None else None

    def mark_candidate_created(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        candidate_revision: int,
        now: str | None = None,
    ) -> ExternalSeriesCandidateOperation:
        _positive("candidate_revision", candidate_revision)
        return self._transition(operation_id, expected_revision, "candidate_created", candidate_revision, now)

    def finalize(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        now: str | None = None,
    ) -> ExternalSeriesCandidateOperation:
        return self._transition(operation_id, expected_revision, "finalized", None, now)

    def list_recoverable(self) -> tuple[ExternalSeriesCandidateOperation, ...]:
        return tuple(
            operation
            for record in self._records.list(_COLLECTION)
            if (operation := _operation(record.payload, record.revision)).state != "finalized"
        )

    def _transition(
        self,
        operation_id: str,
        expected_revision: int,
        to_state: str,
        candidate_revision: int | None,
        now: str | None,
    ) -> ExternalSeriesCandidateOperation:
        _positive("expected_revision", expected_revision)
        current = self.get(operation_id)
        if current is None:
            raise ExternalSeriesCandidateSagaError("series candidate operation does not exist")
        if current.revision != expected_revision:
            raise ExternalSeriesCandidateSagaConflict(f"expected revision {expected_revision}, found {current.revision}")
        if _TRANSITIONS.get(current.state) != to_state:
            raise ExternalSeriesCandidateSagaConflict(
                f"illegal series candidate transition {current.state} -> {to_state}"
            )
        applied = candidate_revision if to_state == "candidate_created" else current.candidate_revision
        try:
            with self._records.begin() as transaction:
                record = transaction.put(
                    _COLLECTION,
                    operation_id,
                    _payload(
                        operation_id,
                        to_state,
                        current.evidence,
                        applied,
                        current.created_at,
                        now or _utc_now(),
                    ),
                    expected_revision=expected_revision,
                )
                transaction.commit()
        except SQLiteUnitOfWorkConflict as exc:
            raise ExternalSeriesCandidateSagaConflict("series candidate transition conflicted") from exc
        return _operation(record.payload, record.revision)


def _payload(
    operation_id: str,
    state: str,
    evidence: ExternalSeriesCandidateEvidence,
    candidate_revision: int | None,
    created_at: str,
    updated_at: str,
) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "state": state,
        "namespace_id": evidence.namespace_id,
        "series_id": evidence.series_id,
        "series_memory_id": evidence.series_memory_id,
        "candidate_id": evidence.candidate_id,
        "base_object_revision": evidence.base_object_revision,
        "base_series_revision": evidence.base_series_revision,
        "payload_sha256": evidence.payload_sha256,
        "authority_identity": evidence.authority_identity,
        "candidate_revision": candidate_revision,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _operation(payload: object, revision: int) -> ExternalSeriesCandidateOperation:
    if not isinstance(payload, dict):
        raise ExternalSeriesCandidateSagaError("series candidate operation payload is invalid")
    evidence = ExternalSeriesCandidateEvidence(
        payload.get("namespace_id"),
        payload.get("series_id"),
        payload.get("series_memory_id"),
        payload.get("candidate_id"),
        payload.get("base_object_revision"),
        payload.get("base_series_revision"),
        payload.get("payload_sha256"),
        payload.get("authority_identity"),
    )
    operation_id = payload.get("operation_id")
    state = payload.get("state")
    candidate_revision = payload.get("candidate_revision")
    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")
    _segment("operation_id", operation_id)
    _evidence(evidence)
    _positive("record revision", revision)
    if state not in _TRANSITIONS and state != "finalized":
        raise ExternalSeriesCandidateSagaError("series candidate operation state is invalid")
    if state == "prepared" and candidate_revision is not None:
        raise ExternalSeriesCandidateSagaError("prepared series candidate operation cannot have a candidate revision")
    if state != "prepared":
        _positive("candidate_revision", candidate_revision)
    if not isinstance(created_at, str) or not created_at or not isinstance(updated_at, str) or not updated_at:
        raise ExternalSeriesCandidateSagaError("series candidate operation timestamps are invalid")
    return ExternalSeriesCandidateOperation(
        operation_id,
        state,
        revision,
        evidence,
        candidate_revision,
        created_at,
        updated_at,
    )


def _evidence(value: object) -> None:
    if not isinstance(value, ExternalSeriesCandidateEvidence):
        raise ExternalSeriesCandidateSagaError("series candidate evidence is invalid")
    for label, item in (
        ("namespace_id", value.namespace_id),
        ("series_id", value.series_id),
        ("series_memory_id", value.series_memory_id),
        ("candidate_id", value.candidate_id),
    ):
        _segment(label, item)
    _non_negative("base_object_revision", value.base_object_revision)
    _non_negative("base_series_revision", value.base_series_revision)
    if not isinstance(value.payload_sha256, str) or not _SHA256.fullmatch(value.payload_sha256):
        raise ExternalSeriesCandidateSagaError("payload_sha256 is invalid")
    if value.authority_identity not in {"json:object-store-v1", "sqlite:structured-records-v1"}:
        raise ExternalSeriesCandidateSagaError("authority_identity is invalid")


def _segment(label: str, value: object) -> None:
    if not isinstance(value, str) or not _SAFE.fullmatch(value):
        raise ExternalSeriesCandidateSagaError(f"{label} is invalid")


def _positive(label: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ExternalSeriesCandidateSagaError(f"{label} must be positive")


def _non_negative(label: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ExternalSeriesCandidateSagaError(f"{label} must be non-negative")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
