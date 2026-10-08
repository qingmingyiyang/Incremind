"""Durable, side-effect-free state for Shared Trust Audit activation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from .sqlite_uow import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict

_COLLECTION = "shared_trust_audit_activation_operations"
_MEMBERS = ("memory_atoms", "memory_publications", "memory_scenarios", "memory_series_memory", "memory_transitions", "project_skills")
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_NEXT = {"prepared": "attestation_written", "attestation_written": "authorities_activated", "authorities_activated": "finalized"}


class SharedTrustAuditActivationSagaError(ValueError):
    pass


class SharedTrustAuditActivationSagaConflict(SharedTrustAuditActivationSagaError):
    pass


@dataclass(frozen=True, slots=True)
class SharedTrustAuditActivationEvidence:
    namespace_id: str
    activation_id: str
    member_migrations: Mapping[str, str]
    source_fingerprint: str
    target_fingerprint: str
    target_identity: str = "sqlite:structured-records-v1"


@dataclass(frozen=True, slots=True)
class SharedTrustAuditActivationOperation:
    operation_id: str
    state: str
    revision: int
    evidence: SharedTrustAuditActivationEvidence
    created_at: str
    updated_at: str


class SQLiteSharedTrustAuditActivationSagaStore:
    """CAS record only; services own every cross-store side effect."""

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        self._records = records

    def prepare(self, evidence: SharedTrustAuditActivationEvidence | Mapping[str, object], *, now: str | None = None) -> SharedTrustAuditActivationOperation:
        evidence = _evidence_from(evidence)
        _validate(evidence)
        operation_id = shared_trust_audit_activation_operation_id(evidence)
        current = self.get(operation_id)
        if current is not None:
            if current.evidence != evidence:
                raise SharedTrustAuditActivationSagaConflict("activation evidence drifted")
            return current
        timestamp = now or _now()
        try:
            with self._records.begin() as uow:
                record = uow.put(_COLLECTION, operation_id, _payload(operation_id, "prepared", evidence, timestamp, timestamp), expected_revision=0)
                uow.commit()
        except SQLiteUnitOfWorkConflict as exc:
            current = self.get(operation_id)
            if current is not None and current.evidence == evidence:
                return current
            raise SharedTrustAuditActivationSagaConflict("activation prepare conflicted") from exc
        return _operation(record.payload, record.revision)

    def get(self, operation_id: str) -> SharedTrustAuditActivationOperation | None:
        _segment(operation_id)
        record = self._records.read(_COLLECTION, operation_id)
        return None if record is None else _operation(record.payload, record.revision)

    def advance(self, operation_id: str, expected_revision: int, state: str, *, now: str | None = None) -> SharedTrustAuditActivationOperation:
        current = self.get(operation_id)
        if current is None:
            raise SharedTrustAuditActivationSagaError("activation operation does not exist")
        if current.revision != expected_revision:
            raise SharedTrustAuditActivationSagaConflict("activation revision conflicted")
        if _NEXT.get(current.state) != state:
            raise SharedTrustAuditActivationSagaConflict("illegal activation transition")
        try:
            with self._records.begin() as uow:
                record = uow.put(_COLLECTION, operation_id, _payload(operation_id, state, current.evidence, current.created_at, now or _now()), expected_revision=expected_revision)
                uow.commit()
        except SQLiteUnitOfWorkConflict as exc:
            raise SharedTrustAuditActivationSagaConflict("activation transition conflicted") from exc
        return _operation(record.payload, record.revision)

    def list_recoverable(self) -> tuple[SharedTrustAuditActivationOperation, ...]:
        return tuple(op for record in self._records.list(_COLLECTION) if (op := _operation(record.payload, record.revision)).state != "finalized")


def shared_trust_audit_activation_operation_id(evidence: SharedTrustAuditActivationEvidence | Mapping[str, object]) -> str:
    evidence = _evidence_from(evidence); _validate(evidence)
    material = f"{evidence.namespace_id}\n{evidence.activation_id}"
    return f"trust-audit-activate-{hashlib.sha256(material.encode()).hexdigest()[:24]}"


def _payload(operation_id, state, evidence, created_at, updated_at):
    return {"operation_id": operation_id, "state": state, **_evidence_payload(evidence), "created_at": created_at, "updated_at": updated_at}


def _evidence_payload(e):
    return {"namespace_id": e.namespace_id, "activation_id": e.activation_id, "member_migrations": dict(e.member_migrations), "source_fingerprint": e.source_fingerprint, "target_fingerprint": e.target_fingerprint, "target_identity": e.target_identity}


def _operation(payload, revision):
    if not isinstance(payload, Mapping) or payload.get("state") not in {*_NEXT, "finalized"} or not isinstance(revision, int) or revision < 1:
        raise SharedTrustAuditActivationSagaError("activation operation is invalid")
    e = _evidence_from(payload); _validate(e)
    operation_id = payload.get("operation_id"); _segment(operation_id)
    if operation_id != shared_trust_audit_activation_operation_id(e) or not isinstance(payload.get("created_at"), str) or not isinstance(payload.get("updated_at"), str):
        raise SharedTrustAuditActivationSagaError("activation operation is invalid")
    return SharedTrustAuditActivationOperation(operation_id, payload["state"], revision, e, payload["created_at"], payload["updated_at"])


def _evidence_from(value):
    if isinstance(value, SharedTrustAuditActivationEvidence): return value
    if not isinstance(value, Mapping): raise SharedTrustAuditActivationSagaError("activation evidence is invalid")
    return SharedTrustAuditActivationEvidence(value.get("namespace_id"), value.get("activation_id"), value.get("member_migrations"), value.get("source_fingerprint"), value.get("target_fingerprint"), value.get("target_identity"))


def _validate(e):
    if not isinstance(e, SharedTrustAuditActivationEvidence): raise SharedTrustAuditActivationSagaError("activation evidence is invalid")
    _segment(e.namespace_id); _segment(e.activation_id)
    if e.target_identity != "sqlite:structured-records-v1" or not isinstance(e.member_migrations, Mapping) or set(e.member_migrations) != set(_MEMBERS): raise SharedTrustAuditActivationSagaError("activation members are invalid")
    if any(not isinstance(e.member_migrations[m], str) or not _SAFE.fullmatch(e.member_migrations[m]) for m in _MEMBERS): raise SharedTrustAuditActivationSagaError("activation migration evidence is invalid")
    if not _SHA.fullmatch(e.source_fingerprint or "") or not _SHA.fullmatch(e.target_fingerprint or ""): raise SharedTrustAuditActivationSagaError("activation fingerprint is invalid")


def _segment(value):
    if not isinstance(value, str) or not _SAFE.fullmatch(value): raise SharedTrustAuditActivationSagaError("activation identifier is invalid")


def _now(): return datetime.now(timezone.utc).isoformat()
