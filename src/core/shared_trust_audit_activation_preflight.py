"""Internal-only staging seam for a verified Shared Trust Audit composite proof."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.shared_trust_audit_activation_service import (
    SharedTrustAuditActivationSagaService,
    SharedTrustAuditActivationServiceResult,
)
from core.shared_trust_audit_fixture_migration import (
    SharedTrustAuditFixtureMigrationResult,
    _AUTHORITY_MEMBERS,
    _COPIED_COLLECTIONS,
    _read_target_records,
    _source_records,
    _target_fingerprint,
    scan_shared_trust_audit_fixture_inventory,
)
from core.storage_provider import (
    AggregateAuthorityEvidence,
    AggregateAuthorityTransition,
    SQLiteAggregateAuthorityStore,
    SQLiteStructuredRecordStore,
    SharedTrustAuditActivationEvidence,
)

_MARKERS = "aggregate_authority_targets"
_ACTIVATION_METADATA = frozenset(
    (
        _MARKERS,
        "aggregate_authority_compound_activations",
        "shared_trust_audit_activation_operations",
    )
)


class SharedTrustAuditActivationPreflightError(ValueError):
    """Raised before staged authority can be proven safe and replayable."""


@dataclass(frozen=True, slots=True)
class SharedTrustAuditActivationPreflightResult:
    evidence: SharedTrustAuditActivationEvidence
    authority_revisions: dict[str, int]


def stage_shared_trust_audit_activation(
    *,
    object_store_root: Path,
    target_database_path: Path,
    authority: SQLiteAggregateAuthorityStore,
    namespace_id: str,
    activation_id: str,
    migration: SharedTrustAuditFixtureMigrationResult,
) -> SharedTrustAuditActivationPreflightResult:
    """Verify proof, write all markers, then atomically stage six authorities."""
    source_root = object_store_root.expanduser().resolve(strict=False)
    target = target_database_path.expanduser().resolve(strict=False)
    evidence = migration.activation_evidence(
        namespace_id=namespace_id,
        activation_id=activation_id,
    )
    _require_proof(source_root, target, namespace_id, migration)
    records = SQLiteStructuredRecordStore(target)
    expected_markers = {
        member: _marker(namespace_id, member, evidence)
        for member in _AUTHORITY_MEMBERS
    }
    _write_or_require_markers(records, expected_markers)
    _require_markers(records, expected_markers)
    staged = _stage_authorities(authority, evidence)
    return SharedTrustAuditActivationPreflightResult(
        evidence=evidence,
        authority_revisions={record.aggregate: record.revision for record in staged},
    )


def activate_verified_shared_trust_audit(
    *,
    object_store_root: Path,
    target_database_path: Path,
    authority: SQLiteAggregateAuthorityStore,
    namespace_id: str,
    activation_id: str,
    migration: SharedTrustAuditFixtureMigrationResult,
    service: SharedTrustAuditActivationSagaService,
    now: str | None = None,
) -> SharedTrustAuditActivationServiceResult:
    """Controlled composition only; no route or authentication policy is implied."""
    preflight = stage_shared_trust_audit_activation(
        object_store_root=object_store_root,
        target_database_path=target_database_path,
        authority=authority,
        namespace_id=namespace_id,
        activation_id=activation_id,
        migration=migration,
    )
    return service.activate(preflight.evidence, now=now)


def _require_proof(
    source_root: Path,
    target: Path,
    namespace_id: str,
    migration: SharedTrustAuditFixtureMigrationResult,
) -> None:
    inventory = scan_shared_trust_audit_fixture_inventory(
        source_root,
        namespace_id=namespace_id,
    )
    if inventory.issues or inventory.inventory.fingerprint != migration.source_fingerprint:
        raise SharedTrustAuditActivationPreflightError(
            "activation preflight source proof is missing or drifted"
        )
    if set(migration.member_migrations) != set(_AUTHORITY_MEMBERS) or set(
        migration.member_migrations.values()
    ) != {migration.migration_id}:
        raise SharedTrustAuditActivationPreflightError(
            "activation preflight member proof is invalid"
        )
    if not target.is_file():
        raise SharedTrustAuditActivationPreflightError(
            "activation preflight target is missing"
        )
    source = _source_records(source_root, namespace_id)
    target_records = _read_target_records(target)
    unexpected = set(target_records) - set(_COPIED_COLLECTIONS) - _ACTIVATION_METADATA
    if unexpected:
        raise SharedTrustAuditActivationPreflightError(
            "activation preflight target contains unexpected collections"
        )
    for collection in _COPIED_COLLECTIONS:
        expected = source[collection]
        actual = target_records.get(collection, {})
        if set(actual) != set(expected):
            raise SharedTrustAuditActivationPreflightError(
                f"activation preflight target object set drifted: {collection}"
            )
        for object_id, payload in expected.items():
            actual_payload, revision = actual[object_id]
            if revision != 1 or actual_payload != payload:
                raise SharedTrustAuditActivationPreflightError(
                    f"activation preflight target payload drifted: {collection}/{object_id}"
                )
    if _target_fingerprint(target) != migration.target_fingerprint:
        raise SharedTrustAuditActivationPreflightError(
            "activation preflight target fingerprint drifted"
        )


def _write_or_require_markers(records, expected_markers) -> None:
    current = {
        member: records.read(_MARKERS, _marker_id(marker))
        for member, marker in expected_markers.items()
    }
    if all(record is None for record in current.values()):
        try:
            with records.begin() as transaction:
                for member, marker in sorted(expected_markers.items()):
                    transaction.put(
                        _MARKERS,
                        _marker_id(marker),
                        marker,
                        expected_revision=0,
                    )
                transaction.commit()
        except Exception as error:
            raise SharedTrustAuditActivationPreflightError(
                f"activation preflight marker write failed: {error}"
            ) from error
        return
    if not all(
        record is not None and record.revision == 1 and dict(record.payload) == expected_markers[member]
        for member, record in current.items()
    ):
        raise SharedTrustAuditActivationPreflightError(
            "activation preflight target markers are partial or drifted"
        )


def _require_markers(records, expected_markers) -> None:
    for member, expected in expected_markers.items():
        record = records.read(_MARKERS, _marker_id(expected))
        if record is None or record.revision != 1 or dict(record.payload) != expected:
            raise SharedTrustAuditActivationPreflightError(
                f"activation preflight target marker is missing or drifted: {member}"
            )


def _stage_authorities(
    authority: SQLiteAggregateAuthorityStore,
    evidence: SharedTrustAuditActivationEvidence,
):
    records = []
    for member in _AUTHORITY_MEMBERS:
        current = authority.get(evidence.namespace_id, member)
        if current is None:
            current = authority.create_json_active(
                namespace_id=evidence.namespace_id,
                aggregate=member,
                reason="JSON remains active before verified composite staging",
            )
        records.append(current)
    expected = {
        member: AggregateAuthorityEvidence(
            evidence.member_migrations[member],
            evidence.source_fingerprint,
            evidence.target_fingerprint,
            evidence.target_identity,
        )
        for member in _AUTHORITY_MEMBERS
    }
    if all(record.state == "sqlite_staged" for record in records):
        if any(record.evidence != expected[record.aggregate] for record in records):
            raise SharedTrustAuditActivationPreflightError(
                "activation preflight staged authority evidence drifted"
            )
        return tuple(records)
    if all(record.state == "sqlite_active" for record in records):
        if any(record.evidence != expected[record.aggregate] for record in records):
            raise SharedTrustAuditActivationPreflightError(
                "activation preflight active authority evidence drifted"
            )
        return tuple(records)
    if not all(record.state == "json_active" and record.evidence is None for record in records):
        raise SharedTrustAuditActivationPreflightError(
            "activation preflight authority state is partial or incompatible"
        )
    try:
        return authority.transition_many(
            tuple(
                AggregateAuthorityTransition(
                    namespace_id=evidence.namespace_id,
                    aggregate=record.aggregate,
                    expected_revision=record.revision,
                    to_state="sqlite_staged",
                    reason="composite proof and target markers verified",
                    evidence=expected[record.aggregate],
                )
                for record in records
            )
        )
    except Exception as error:
        raise SharedTrustAuditActivationPreflightError(
            f"activation preflight authority staging failed: {error}"
        ) from error


def _marker(
    namespace_id: str,
    aggregate: str,
    evidence: SharedTrustAuditActivationEvidence,
) -> dict[str, object]:
    return {
        "namespace_id": namespace_id,
        "aggregate": aggregate,
        "migration_id": evidence.member_migrations[aggregate],
        "source_fingerprint": evidence.source_fingerprint,
        "target_fingerprint": evidence.target_fingerprint,
        "target_identity": evidence.target_identity,
    }


def _marker_id(marker: dict[str, object]) -> str:
    return f"{marker['namespace_id']}~{marker['aggregate']}"
