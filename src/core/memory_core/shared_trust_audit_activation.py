"""Read-only proof that the shared Memory Trust Audit was compound-activated.

This record is an activation attestation produced by a future compound
activation workflow.  It is deliberately not an authority marker and this
module never creates or transitions authority state.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from core.storage_provider import SQLiteStructuredRecordStore


_COLLECTION = "aggregate_authority_compound_activations"
_KIND = "memory_publication_trust_audit"
_MEMBERS = (
    "memory_atoms",
    "memory_publications",
    "memory_scenarios",
    "memory_series_memory",
    "memory_transitions",
    "project_skills",
)
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_SAFE_MIGRATION = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_TARGET = re.compile(r"^sqlite:[A-Za-z0-9][A-Za-z0-9._:-]{0,239}$")


class SharedTrustAuditActivationError(ValueError):
    """Raised when a compound activation cannot prove shared audit readiness."""


@dataclass(frozen=True, slots=True)
class SharedTrustAuditActivation:
    namespace_id: str
    target_identity: str
    activation_id: str
    member_migrations: Mapping[str, str]
    source_fingerprint: str
    target_fingerprint: str
    activated_at: str


def shared_trust_audit_activation_id(namespace_id: str) -> str:
    _require_segment("namespace_id", namespace_id)
    return f"{namespace_id}~{_KIND}"


def shared_trust_audit_activation_payload(
    *,
    namespace_id: str,
    target_identity: str,
    activation_id: str,
    member_migrations: Mapping[str, str],
    source_fingerprint: str,
    target_fingerprint: str,
    activated_at: str,
) -> dict[str, object]:
    """Build a deterministic record for a future compound activation writer."""

    activation = _activation_from_payload(
        {
            "schema_version": "1.0.0",
            "id": shared_trust_audit_activation_id(namespace_id),
            "namespace_id": namespace_id,
            "kind": _KIND,
            "target_identity": target_identity,
            "activation_id": activation_id,
            "member_aggregates": list(_MEMBERS),
            "member_migrations": dict(member_migrations),
            "source_fingerprint": source_fingerprint,
            "target_fingerprint": target_fingerprint,
            "activated_at": activated_at,
        },
        namespace_id=namespace_id,
        target_identity=target_identity,
    )
    return {
        "schema_version": "1.0.0",
        "id": shared_trust_audit_activation_id(activation.namespace_id),
        "namespace_id": activation.namespace_id,
        "kind": _KIND,
        "target_identity": activation.target_identity,
        "activation_id": activation.activation_id,
        "member_aggregates": list(_MEMBERS),
        "member_migrations": dict(activation.member_migrations),
        "source_fingerprint": activation.source_fingerprint,
        "target_fingerprint": activation.target_fingerprint,
        "activated_at": activation.activated_at,
    }


def require_shared_trust_audit_activation(
    records: SQLiteStructuredRecordStore,
    *,
    namespace_id: str,
    target_identity: str,
) -> SharedTrustAuditActivation:
    """Return a verified compound attestation without mutating SQLite state."""

    _require_segment("namespace_id", namespace_id)
    _require_target_identity(target_identity)
    record = records.read(_COLLECTION, shared_trust_audit_activation_id(namespace_id))
    if record is None:
        raise SharedTrustAuditActivationError("shared Trust Audit compound activation is not ready")
    return _activation_from_payload(
        record.payload,
        namespace_id=namespace_id,
        target_identity=target_identity,
    )


def _activation_from_payload(
    payload: Mapping[str, object],
    *,
    namespace_id: str,
    target_identity: str,
) -> SharedTrustAuditActivation:
    required = {
        "schema_version",
        "id",
        "namespace_id",
        "kind",
        "target_identity",
        "activation_id",
        "member_aggregates",
        "member_migrations",
        "source_fingerprint",
        "target_fingerprint",
        "activated_at",
    }
    if set(payload) != required:
        raise SharedTrustAuditActivationError("shared Trust Audit compound activation payload is invalid")
    if payload.get("schema_version") != "1.0.0" or payload.get("kind") != _KIND:
        raise SharedTrustAuditActivationError("shared Trust Audit compound activation payload is invalid")
    if payload.get("namespace_id") != namespace_id or payload.get("id") != shared_trust_audit_activation_id(namespace_id):
        raise SharedTrustAuditActivationError("shared Trust Audit compound activation namespace is invalid")
    if payload.get("target_identity") != target_identity:
        raise SharedTrustAuditActivationError("shared Trust Audit compound activation target mismatches Project Skill authority")
    _require_target_identity(target_identity)
    activation_id = payload.get("activation_id")
    if not isinstance(activation_id, str) or not _SAFE_MIGRATION.fullmatch(activation_id):
        raise SharedTrustAuditActivationError("shared Trust Audit compound activation evidence is invalid")
    members = payload.get("member_aggregates")
    if not isinstance(members, list) or tuple(members) != _MEMBERS:
        raise SharedTrustAuditActivationError("shared Trust Audit compound activation members are invalid")
    migrations = payload.get("member_migrations")
    if not isinstance(migrations, Mapping) or set(migrations) != set(_MEMBERS):
        raise SharedTrustAuditActivationError("shared Trust Audit compound activation evidence is invalid")
    normalized_migrations: dict[str, str] = {}
    for member in _MEMBERS:
        migration_id = migrations.get(member)
        if not isinstance(migration_id, str) or not _SAFE_MIGRATION.fullmatch(migration_id):
            raise SharedTrustAuditActivationError("shared Trust Audit compound activation evidence is invalid")
        normalized_migrations[member] = migration_id
    source_fingerprint = _fingerprint(payload.get("source_fingerprint"))
    target_fingerprint = _fingerprint(payload.get("target_fingerprint"))
    activated_at = payload.get("activated_at")
    if not isinstance(activated_at, str) or not activated_at:
        raise SharedTrustAuditActivationError("shared Trust Audit compound activation evidence is invalid")
    return SharedTrustAuditActivation(
        namespace_id,
        target_identity,
        activation_id,
        normalized_migrations,
        source_fingerprint,
        target_fingerprint,
        activated_at,
    )


def _require_segment(label: str, value: str) -> None:
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise SharedTrustAuditActivationError(f"{label} is invalid")


def _require_target_identity(value: str) -> None:
    if not isinstance(value, str) or not _TARGET.fullmatch(value):
        raise SharedTrustAuditActivationError("target_identity is invalid")


def _fingerprint(value: object) -> str:
    if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
        raise SharedTrustAuditActivationError("shared Trust Audit compound activation evidence is invalid")
    return value
