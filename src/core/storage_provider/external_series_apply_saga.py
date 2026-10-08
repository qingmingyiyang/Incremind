from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .sqlite_uow import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


_COLLECTION = "external_series_apply_operations"
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRANSITIONS = {"prepared": "series_applied", "series_applied": "finalized"}


class ExternalSeriesApplySagaError(ValueError):
    pass


class ExternalSeriesApplySagaConflict(ExternalSeriesApplySagaError):
    pass


@dataclass(frozen=True, slots=True)
class ExternalSeriesApplyEvidence:
    namespace_id: str
    series_id: str
    series_memory_id: str
    base_object_revision: int
    base_series_revision: int
    payload_sha256: str
    authority_identity: str = "json:object-store-v1"


@dataclass(frozen=True, slots=True)
class ExternalSeriesApplyOperation:
    operation_id: str
    state: str
    revision: int
    evidence: ExternalSeriesApplyEvidence
    applied_series_revision: int | None
    created_at: str
    updated_at: str


class SQLiteExternalSeriesApplySagaStore:
    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        self._records = records

    def prepare(self, *, operation_id: str, evidence: ExternalSeriesApplyEvidence, now: str | None = None):
        _segment("operation_id", operation_id); _evidence(evidence)
        current = self.get(operation_id)
        if current is not None:
            if current.evidence != evidence:
                raise ExternalSeriesApplySagaConflict("series apply operation evidence drifted")
            return current
        timestamp = now or _utc_now()
        try:
            with self._records.begin() as uow:
                record = uow.put(_COLLECTION, operation_id, _payload(operation_id, "prepared", evidence, None, timestamp, timestamp), expected_revision=0)
                uow.commit()
        except SQLiteUnitOfWorkConflict as exc:
            concurrent = self.get(operation_id)
            if concurrent is not None and concurrent.evidence == evidence:
                return concurrent
            raise ExternalSeriesApplySagaConflict("series apply prepare conflicted") from exc
        return _operation(record.payload, record.revision)

    def get(self, operation_id: str):
        _segment("operation_id", operation_id)
        record = self._records.read(_COLLECTION, operation_id)
        return _operation(record.payload, record.revision) if record is not None else None

    def mark_series_applied(self, operation_id: str, *, expected_revision: int, applied_series_revision: int, now: str | None = None):
        _positive("applied_series_revision", applied_series_revision)
        return self._transition(operation_id, expected_revision, "series_applied", applied_series_revision, now)

    def finalize(self, operation_id: str, *, expected_revision: int, now: str | None = None):
        return self._transition(operation_id, expected_revision, "finalized", None, now)

    def list_recoverable(self):
        return tuple(operation for record in self._records.list(_COLLECTION)
                     if (operation := _operation(record.payload, record.revision)).state != "finalized")

    def _transition(self, operation_id, expected_revision, to_state, applied_revision, now):
        _positive("expected_revision", expected_revision)
        current = self.get(operation_id)
        if current is None:
            raise ExternalSeriesApplySagaError("series apply operation does not exist")
        if current.revision != expected_revision:
            raise ExternalSeriesApplySagaConflict(f"expected revision {expected_revision}, found {current.revision}")
        if _TRANSITIONS.get(current.state) != to_state:
            raise ExternalSeriesApplySagaConflict(f"illegal series apply transition {current.state} -> {to_state}")
        applied = applied_revision if to_state == "series_applied" else current.applied_series_revision
        try:
            with self._records.begin() as uow:
                record = uow.put(_COLLECTION, operation_id, _payload(operation_id, to_state, current.evidence, applied, current.created_at, now or _utc_now()), expected_revision=expected_revision)
                uow.commit()
        except SQLiteUnitOfWorkConflict as exc:
            raise ExternalSeriesApplySagaConflict("series apply transition conflicted") from exc
        return _operation(record.payload, record.revision)


def _payload(operation_id, state, evidence, applied, created_at, updated_at):
    return {"operation_id": operation_id, "state": state, "namespace_id": evidence.namespace_id,
            "series_id": evidence.series_id, "series_memory_id": evidence.series_memory_id,
            "base_object_revision": evidence.base_object_revision, "base_series_revision": evidence.base_series_revision,
            "payload_sha256": evidence.payload_sha256, "authority_identity": evidence.authority_identity,
            "applied_series_revision": applied, "created_at": created_at, "updated_at": updated_at}


def _operation(payload, revision):
    evidence = ExternalSeriesApplyEvidence(payload.get("namespace_id"), payload.get("series_id"), payload.get("series_memory_id"),
                                           payload.get("base_object_revision"), payload.get("base_series_revision"),
                                           payload.get("payload_sha256"), payload.get("authority_identity"))
    operation_id, state, applied = payload.get("operation_id"), payload.get("state"), payload.get("applied_series_revision")
    created_at, updated_at = payload.get("created_at"), payload.get("updated_at")
    _segment("operation_id", operation_id); _evidence(evidence); _positive("record revision", revision)
    if state not in {"prepared", "series_applied", "finalized"} or (state == "prepared" and applied is not None):
        raise ExternalSeriesApplySagaError("series apply operation state is invalid")
    if state != "prepared": _positive("applied_series_revision", applied)
    if not isinstance(created_at, str) or not created_at or not isinstance(updated_at, str) or not updated_at:
        raise ExternalSeriesApplySagaError("series apply operation timestamps are invalid")
    return ExternalSeriesApplyOperation(operation_id, state, revision, evidence, applied, created_at, updated_at)


def _evidence(value):
    if not isinstance(value, ExternalSeriesApplyEvidence): raise ExternalSeriesApplySagaError("series apply evidence is invalid")
    _segment("namespace_id", value.namespace_id); _segment("series_id", value.series_id); _segment("series_memory_id", value.series_memory_id)
    _non_negative("base_object_revision", value.base_object_revision); _non_negative("base_series_revision", value.base_series_revision)
    if not isinstance(value.payload_sha256, str) or not _SHA256.fullmatch(value.payload_sha256): raise ExternalSeriesApplySagaError("payload_sha256 is invalid")
    if value.authority_identity != "json:object-store-v1": raise ExternalSeriesApplySagaError("authority_identity is invalid")


def _segment(label, value):
    if not isinstance(value, str) or not _SAFE.fullmatch(value): raise ExternalSeriesApplySagaError(f"{label} is invalid")


def _positive(label, value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1: raise ExternalSeriesApplySagaError(f"{label} must be positive")


def _non_negative(label, value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0: raise ExternalSeriesApplySagaError(f"{label} must be non-negative")


def _utc_now(): return datetime.now(timezone.utc).isoformat()
