from __future__ import annotations

from pathlib import Path

import pytest

from core.aggregate_repository_factory import (
    AUTHORITY_DATABASE_NAME,
    STRUCTURED_DATABASE_NAME,
)
from core.fresh_vault_shared_trust_audit_bootstrap import (
    FRESH_VAULT_MIGRATION_ID,
    FreshVaultSharedTrustAuditBootstrapError,
    bootstrap_fresh_vault_shared_trust_audit,
)
from core.shared_trust_audit_activation_preflight import (
    stage_shared_trust_audit_activation,
)
from core.shared_trust_audit_fixture_migration import (
    execute_shared_trust_audit_fixture_migration,
    plan_shared_trust_audit_fixture_migration_dry_run,
    scan_shared_trust_audit_fixture_inventory,
)
from core.storage_provider import (
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteSharedTrustAuditActivationSagaStore,
    SQLiteStructuredRecordStore,
    SQLiteMigrationLedger,
)


MEMBERS = (
    "memory_atoms",
    "memory_publications",
    "memory_scenarios",
    "memory_series_memory",
    "memory_transitions",
    "project_skills",
)


def test_empty_fresh_vault_bootstraps_all_members_and_replays_without_revisions(
    tmp_path: Path,
) -> None:
    first = bootstrap_fresh_vault_shared_trust_audit(tmp_path)
    rebuild_root = tmp_path / ".rebuild-data"
    records = SQLiteStructuredRecordStore(rebuild_root / STRUCTURED_DATABASE_NAME)
    authority = SQLiteAggregateAuthorityStore(rebuild_root / AUTHORITY_DATABASE_NAME)
    operation = SQLiteSharedTrustAuditActivationSagaStore(records).list_recoverable()
    first_authority_revisions = {
        member: authority.get("default", member).revision for member in MEMBERS
    }
    attestations = records.list("aggregate_authority_compound_activations")

    second = bootstrap_fresh_vault_shared_trust_audit(tmp_path)

    assert first.outcome == "activated"
    assert first.object_count == 0
    assert second.outcome == "already_active"
    assert second.operation_id == first.operation_id
    assert second.operation_revision == first.operation_revision
    assert operation == ()
    assert {
        member: authority.get("default", member).revision for member in MEMBERS
    } == first_authority_revisions
    assert all(authority.get("default", member).state == "sqlite_active" for member in MEMBERS)
    assert len(attestations) == 1
    assert records.list("aggregate_authority_compound_activations") == attestations


def test_finalized_bootstrap_tolerates_later_runtime_collections(tmp_path: Path) -> None:
    first = bootstrap_fresh_vault_shared_trust_audit(tmp_path)
    rebuild_root = tmp_path / ".rebuild-data"
    records = SQLiteStructuredRecordStore(rebuild_root / STRUCTURED_DATABASE_NAME)
    authority = SQLiteAggregateAuthorityStore(rebuild_root / AUTHORITY_DATABASE_NAME)
    first_revisions = {
        member: authority.get("default", member).revision for member in MEMBERS
    }
    with records.begin() as transaction:
        transaction.put(
            "jobs",
            "job-after-bootstrap",
            {"id": "job-after-bootstrap", "status": "completed"},
            expected_revision=0,
        )
        transaction.commit()

    second = bootstrap_fresh_vault_shared_trust_audit(tmp_path)

    assert second.outcome == "already_active"
    assert second.operation_id == first.operation_id
    assert records.read("jobs", "job-after-bootstrap") is not None
    assert {
        member: authority.get("default", member).revision for member in MEMBERS
    } == first_revisions


def test_nonempty_composite_source_is_not_bootstrapped(tmp_path: Path) -> None:
    rebuild_root = tmp_path / ".rebuild-data"
    store = JsonObjectStore(rebuild_root, legacy_root=tmp_path / "library")
    store.write(
        "memory_atoms",
        "atom-existing",
        {"id": "atom-existing", "layer": "atom", "revision": 1},
        expected_revision=0,
    )

    result = bootstrap_fresh_vault_shared_trust_audit(tmp_path)

    assert result.outcome == "not_empty"
    assert result.object_count == 1
    assert not (rebuild_root / STRUCTURED_DATABASE_NAME).exists()
    assert not (rebuild_root / AUTHORITY_DATABASE_NAME).exists()


def test_existing_unrelated_structured_target_fails_closed(tmp_path: Path) -> None:
    rebuild_root = tmp_path / ".rebuild-data"
    rebuild_root.mkdir(parents=True)
    records = SQLiteStructuredRecordStore(rebuild_root / STRUCTURED_DATABASE_NAME)
    with records.begin() as transaction:
        transaction.put(
            "jobs",
            "job-existing",
            {"id": "job-existing", "status": "completed"},
            expected_revision=0,
        )
        transaction.commit()

    with pytest.raises(FreshVaultSharedTrustAuditBootstrapError):
        bootstrap_fresh_vault_shared_trust_audit(tmp_path)

    assert records.read("jobs", "job-existing") is not None
    authority_path = rebuild_root / AUTHORITY_DATABASE_NAME
    if authority_path.exists():
        authority = SQLiteAggregateAuthorityStore(authority_path)
        assert all(authority.get("default", member) is None for member in MEMBERS)


def test_bootstrap_resumes_an_empty_target_created_before_preflight(tmp_path: Path) -> None:
    migration = _create_empty_target(tmp_path)

    result = bootstrap_fresh_vault_shared_trust_audit(tmp_path)

    assert migration.object_count == 0
    assert result.outcome == "activated"
    authority = SQLiteAggregateAuthorityStore(
        tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME
    )
    assert all(authority.get("default", member).state == "sqlite_active" for member in MEMBERS)


def test_bootstrap_resumes_staged_authority_and_prepared_operation(tmp_path: Path) -> None:
    migration = _create_empty_target(tmp_path)
    rebuild_root = tmp_path / ".rebuild-data"
    records = SQLiteStructuredRecordStore(rebuild_root / STRUCTURED_DATABASE_NAME)
    authority = SQLiteAggregateAuthorityStore(rebuild_root / AUTHORITY_DATABASE_NAME)
    preflight = stage_shared_trust_audit_activation(
        object_store_root=rebuild_root,
        target_database_path=rebuild_root / STRUCTURED_DATABASE_NAME,
        authority=authority,
        namespace_id="default",
        activation_id="fresh-vault-shared-trust-audit-v1",
        migration=migration,
    )
    operations = SQLiteSharedTrustAuditActivationSagaStore(records)
    prepared = operations.prepare(preflight.evidence)

    result = bootstrap_fresh_vault_shared_trust_audit(tmp_path)

    assert prepared.state == "prepared"
    assert result.outcome == "activated"
    assert result.operation_id == prepared.operation_id
    assert operations.list_recoverable() == ()
    assert all(authority.get("default", member).state == "sqlite_active" for member in MEMBERS)


def test_partial_marker_is_rejected_without_creating_authority(tmp_path: Path) -> None:
    _create_empty_target(tmp_path)
    rebuild_root = tmp_path / ".rebuild-data"
    records = SQLiteStructuredRecordStore(rebuild_root / STRUCTURED_DATABASE_NAME)
    with records.begin() as transaction:
        transaction.put(
            "aggregate_authority_targets",
            "default~memory_atoms",
            {"namespace_id": "default", "aggregate": "memory_atoms", "drift": True},
            expected_revision=0,
        )
        transaction.commit()

    with pytest.raises(FreshVaultSharedTrustAuditBootstrapError):
        bootstrap_fresh_vault_shared_trust_audit(tmp_path)

    assert not (rebuild_root / AUTHORITY_DATABASE_NAME).exists()


def _create_empty_target(tmp_path: Path):
    rebuild_root = tmp_path / ".rebuild-data"
    inventory = scan_shared_trust_audit_fixture_inventory(
        rebuild_root,
        namespace_id="default",
    )
    ledger = SQLiteMigrationLedger(tmp_path / "bootstrap-test-ledger.sqlite3")
    dry_run = plan_shared_trust_audit_fixture_migration_dry_run(
        ledger=ledger,
        migration_id=FRESH_VAULT_MIGRATION_ID,
        target_schema_version=1,
        inventory=inventory,
        rollback_pointer="fresh-vault:test-no-user-data",
    )
    return execute_shared_trust_audit_fixture_migration(
        object_store_root=rebuild_root,
        target_database_path=rebuild_root / STRUCTURED_DATABASE_NAME,
        ledger=ledger,
        dry_run=dry_run,
    )
