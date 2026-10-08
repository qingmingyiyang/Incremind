"""Proof-bound Shared Trust Audit initialization for a genuinely empty Vault."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.aggregate_repository_factory import (
    AUTHORITY_DATABASE_NAME,
    STRUCTURED_DATABASE_NAME,
)
from core.memory_core import require_shared_trust_audit_activation
from core.shared_trust_audit_activation_preflight import (
    activate_verified_shared_trust_audit,
)
from core.shared_trust_audit_activation_service import (
    SharedTrustAuditActivationSagaService,
)
from core.shared_trust_audit_fixture_migration import (
    SharedTrustAuditFixtureMigrationResult,
    execute_shared_trust_audit_fixture_migration,
    plan_shared_trust_audit_fixture_migration_dry_run,
    scan_shared_trust_audit_fixture_inventory,
    shared_trust_audit_target_fingerprint,
)
from core.storage_provider import (
    AggregateAuthorityEvidence,
    SQLiteAggregateAuthorityStore,
    SQLiteMigrationLedger,
    SQLiteSharedTrustAuditActivationSagaStore,
    SQLiteStructuredRecordStore,
)


FRESH_VAULT_MIGRATION_ID = "fresh-vault-shared-trust-audit-v1"
FRESH_VAULT_ACTIVATION_ID = "fresh-vault-shared-trust-audit-v1"
FRESH_VAULT_LEDGER_NAME = "fresh-vault-migration-ledger.sqlite3"
_AUTHORITY_MEMBERS = (
    "memory_atoms",
    "memory_publications",
    "memory_scenarios",
    "memory_series_memory",
    "memory_transitions",
    "project_skills",
)


class FreshVaultSharedTrustAuditBootstrapError(ValueError):
    """Raised when an allegedly fresh Vault cannot be initialized safely."""


@dataclass(frozen=True, slots=True)
class FreshVaultSharedTrustAuditBootstrapResult:
    outcome: str
    object_count: int
    operation_id: str | None = None
    operation_revision: int | None = None
    authority_revisions: dict[str, int] | None = None


def bootstrap_fresh_vault_shared_trust_audit(
    runtime_root: Path,
    *,
    namespace_id: str = "default",
) -> FreshVaultSharedTrustAuditBootstrapResult:
    """Activate the six-member SQLite authority only for an empty composite source.

    Existing business records are never migrated by this entry. A previously
    created empty target may be resumed because activation preflight rechecks
    the source, copied collection set, target fingerprint, markers and authority.
    """

    root = runtime_root.expanduser().resolve(strict=False)
    rebuild_root = root / ".rebuild-data"
    inventory = scan_shared_trust_audit_fixture_inventory(
        rebuild_root,
        namespace_id=namespace_id,
    )
    if inventory.issues or inventory.inventory.object_count != 0:
        return FreshVaultSharedTrustAuditBootstrapResult(
            outcome="not_empty",
            object_count=inventory.inventory.object_count,
        )

    rebuild_root.mkdir(parents=True, exist_ok=True)
    target = rebuild_root / STRUCTURED_DATABASE_NAME
    if target.exists():
        existing = _validated_existing_activation(
            rebuild_root,
            target,
            namespace_id,
        )
        if existing is not None:
            return existing
    migration = (
        _recover_empty_migration(target, inventory.inventory.fingerprint)
        if target.exists()
        else _create_empty_migration(rebuild_root, target, inventory)
    )
    records = SQLiteStructuredRecordStore(target)
    authority = SQLiteAggregateAuthorityStore(rebuild_root / AUTHORITY_DATABASE_NAME)
    service = SharedTrustAuditActivationSagaService(
        operations=SQLiteSharedTrustAuditActivationSagaStore(records),
        records=records,
        authority=authority,
    )
    try:
        activation = activate_verified_shared_trust_audit(
            object_store_root=rebuild_root,
            target_database_path=target,
            authority=authority,
            namespace_id=namespace_id,
            activation_id=FRESH_VAULT_ACTIVATION_ID,
            migration=migration,
            service=service,
        )
    except Exception as error:
        raise FreshVaultSharedTrustAuditBootstrapError(str(error)) from error

    authority_revisions = {
        member: _required_authority_revision(authority, namespace_id, member)
        for member in _AUTHORITY_MEMBERS
    }
    return FreshVaultSharedTrustAuditBootstrapResult(
        outcome="activated",
        object_count=0,
        operation_id=activation.operation.operation_id,
        operation_revision=activation.operation.revision,
        authority_revisions=authority_revisions,
    )


def _validated_existing_activation(
    rebuild_root: Path,
    target: Path,
    namespace_id: str,
) -> FreshVaultSharedTrustAuditBootstrapResult | None:
    """Accept an existing bootstrap only when every durable proof still agrees."""

    records = SQLiteStructuredRecordStore(target)
    matching = [
        record
        for record in records.list("shared_trust_audit_activation_operations")
        if record.payload.get("namespace_id") == namespace_id
        and record.payload.get("activation_id") == FRESH_VAULT_ACTIVATION_ID
    ]
    if not matching:
        return None
    if len(matching) != 1:
        raise FreshVaultSharedTrustAuditBootstrapError(
            "fresh Vault activation operation is ambiguous"
        )
    if matching[0].payload.get("state") != "finalized":
        return None
    operations = SQLiteSharedTrustAuditActivationSagaStore(records)
    operation_id = matching[0].payload.get("operation_id")
    try:
        operation = operations.get(operation_id)
        if operation is None or operation.state != "finalized":
            raise FreshVaultSharedTrustAuditBootstrapError(
                "fresh Vault activation operation is incomplete"
            )
        attestation = require_shared_trust_audit_activation(
            records,
            namespace_id=namespace_id,
            target_identity=operation.evidence.target_identity,
        )
    except FreshVaultSharedTrustAuditBootstrapError:
        raise
    except Exception as error:
        raise FreshVaultSharedTrustAuditBootstrapError(str(error)) from error
    evidence = operation.evidence
    if (
        attestation.activation_id != evidence.activation_id
        or dict(attestation.member_migrations) != dict(evidence.member_migrations)
        or attestation.source_fingerprint != evidence.source_fingerprint
        or attestation.target_fingerprint != evidence.target_fingerprint
    ):
        raise FreshVaultSharedTrustAuditBootstrapError(
            "fresh Vault activation attestation drifted"
        )
    authority = SQLiteAggregateAuthorityStore(rebuild_root / AUTHORITY_DATABASE_NAME)
    expected_evidence = {
        member: AggregateAuthorityEvidence(
            evidence.member_migrations[member],
            evidence.source_fingerprint,
            evidence.target_fingerprint,
            evidence.target_identity,
        )
        for member in _AUTHORITY_MEMBERS
    }
    authority_revisions: dict[str, int] = {}
    for member in _AUTHORITY_MEMBERS:
        record = authority.get(namespace_id, member)
        if (
            record is None
            or record.state != "sqlite_active"
            or record.evidence != expected_evidence[member]
        ):
            raise FreshVaultSharedTrustAuditBootstrapError(
                "fresh Vault active authority drifted"
            )
        marker = records.read("aggregate_authority_targets", f"{namespace_id}~{member}")
        expected_marker = {
            "namespace_id": namespace_id,
            "aggregate": member,
            "migration_id": evidence.member_migrations[member],
            "source_fingerprint": evidence.source_fingerprint,
            "target_fingerprint": evidence.target_fingerprint,
            "target_identity": evidence.target_identity,
        }
        if marker is None or marker.revision != 1 or dict(marker.payload) != expected_marker:
            raise FreshVaultSharedTrustAuditBootstrapError(
                "fresh Vault activation marker drifted"
            )
        authority_revisions[member] = record.revision
    return FreshVaultSharedTrustAuditBootstrapResult(
        outcome="already_active",
        object_count=0,
        operation_id=operation.operation_id,
        operation_revision=operation.revision,
        authority_revisions=authority_revisions,
    )


def _create_empty_migration(rebuild_root, target, inventory):
    ledger = SQLiteMigrationLedger(rebuild_root / FRESH_VAULT_LEDGER_NAME)
    dry_run = plan_shared_trust_audit_fixture_migration_dry_run(
        ledger=ledger,
        migration_id=FRESH_VAULT_MIGRATION_ID,
        target_schema_version=1,
        inventory=inventory,
        rollback_pointer="fresh-vault:no-user-data",
    )
    try:
        return execute_shared_trust_audit_fixture_migration(
            object_store_root=rebuild_root,
            target_database_path=target,
            ledger=ledger,
            dry_run=dry_run,
        )
    except Exception as error:
        raise FreshVaultSharedTrustAuditBootstrapError(str(error)) from error


def _recover_empty_migration(
    target: Path,
    source_fingerprint: str,
) -> SharedTrustAuditFixtureMigrationResult:
    try:
        target_fingerprint = shared_trust_audit_target_fingerprint(target)
    except Exception as error:
        raise FreshVaultSharedTrustAuditBootstrapError(str(error)) from error
    return SharedTrustAuditFixtureMigrationResult(
        migration_id=FRESH_VAULT_MIGRATION_ID,
        member_migrations={member: FRESH_VAULT_MIGRATION_ID for member in _AUTHORITY_MEMBERS},
        source_fingerprint=source_fingerprint,
        target_fingerprint=target_fingerprint,
        object_count=0,
    )


def _required_authority_revision(authority, namespace_id: str, member: str) -> int:
    record = authority.get(namespace_id, member)
    if record is None or record.state != "sqlite_active":
        raise FreshVaultSharedTrustAuditBootstrapError(
            "fresh Vault authority did not converge"
        )
    return record.revision
