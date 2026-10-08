from pathlib import Path

import pytest

from core.shared_trust_audit_activation_preflight import (
    SharedTrustAuditActivationPreflightError,
    activate_verified_shared_trust_audit,
    stage_shared_trust_audit_activation,
)
from core.shared_trust_audit_activation_service import SharedTrustAuditActivationSagaService
from core.shared_trust_audit_fixture_migration import (
    execute_shared_trust_audit_fixture_migration,
)
from core.storage_provider import (
    AggregateAuthorityEvidence,
    SQLiteAggregateAuthorityStore,
    SQLiteSharedTrustAuditActivationSagaStore,
    SQLiteStructuredRecordStore,
    SQLiteStructuredRecordUnitOfWork,
)
from tests.rebuild.test_shared_trust_audit_fixture_migration import MEMBERS, _fixture


def _migrated(tmp_path: Path):
    root, store, _inventory, ledger, dry_run, *_ = _fixture(tmp_path)
    target = tmp_path / "target" / "shared.sqlite3"
    migration = execute_shared_trust_audit_fixture_migration(
        object_store_root=root,
        target_database_path=target,
        ledger=ledger,
        dry_run=dry_run,
    )
    authority = SQLiteAggregateAuthorityStore(tmp_path / "authority.sqlite3")
    return root, store, target, migration, authority


def test_preflight_writes_exact_markers_and_atomically_stages_all_authorities(tmp_path: Path) -> None:
    root, _store, target, migration, authority = _migrated(tmp_path)

    first = stage_shared_trust_audit_activation(
        object_store_root=root,
        target_database_path=target,
        authority=authority,
        namespace_id="default",
        activation_id="controlled-activation-v1",
        migration=migration,
    )
    replay = stage_shared_trust_audit_activation(
        object_store_root=root,
        target_database_path=target,
        authority=authority,
        namespace_id="default",
        activation_id="controlled-activation-v1",
        migration=migration,
    )

    assert first == replay
    assert set(first.authority_revisions) == MEMBERS
    assert set(first.authority_revisions.values()) == {2}
    records = SQLiteStructuredRecordStore(target)
    for member in MEMBERS:
        marker = records.read("aggregate_authority_targets", f"default~{member}")
        assert marker is not None and marker.revision == 1
        assert marker.payload["migration_id"] == migration.migration_id
        state = authority.get("default", member)
        assert state is not None and state.state == "sqlite_staged"


def test_preflight_rejects_source_target_and_member_proof_drift(tmp_path: Path) -> None:
    root, store, target, migration, authority = _migrated(tmp_path)
    atom = store.read("memory_atoms", "atom-fixture")
    assert atom is not None
    atom["updated_at"] = "2026-07-13T01:00:00+08:00"
    store.write("memory_atoms", "atom-fixture", atom, expected_revision=1)
    with pytest.raises(SharedTrustAuditActivationPreflightError, match="source proof"):
        stage_shared_trust_audit_activation(
            object_store_root=root,
            target_database_path=target,
            authority=authority,
            namespace_id="default",
            activation_id="controlled-activation-v1",
            migration=migration,
        )
    assert authority.list_records() == ()

    root, _store, target, migration, authority = _migrated(tmp_path / "target-drift")
    records = SQLiteStructuredRecordStore(target)
    with records.begin() as transaction:
        atom = transaction.read("memory_atoms", "atom-fixture")
        assert atom is not None
        transaction.put(
            "memory_atoms",
            "atom-fixture",
            {**atom.payload, "updated_at": "drift"},
            expected_revision=atom.revision,
        )
        transaction.commit()
    with pytest.raises(SharedTrustAuditActivationPreflightError, match="payload drifted"):
        stage_shared_trust_audit_activation(
            object_store_root=root,
            target_database_path=target,
            authority=authority,
            namespace_id="default",
            activation_id="controlled-activation-v1",
            migration=migration,
        )
    assert authority.list_records() == ()

    root, _store, target, migration, authority = _migrated(tmp_path / "member-drift")
    migration.member_migrations["memory_atoms"] = "different-migration"
    with pytest.raises(SharedTrustAuditActivationPreflightError, match="member proof"):
        stage_shared_trust_audit_activation(
            object_store_root=root,
            target_database_path=target,
            authority=authority,
            namespace_id="default",
            activation_id="controlled-activation-v1",
            migration=migration,
        )


def test_marker_batch_failure_leaves_no_partial_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _store, target, migration, authority = _migrated(tmp_path)
    original_put = SQLiteStructuredRecordUnitOfWork.put
    marker_calls = 0

    def fail_third_marker(self, collection, *args, **kwargs):
        nonlocal marker_calls
        if collection == "aggregate_authority_targets":
            marker_calls += 1
            if marker_calls == 3:
                raise RuntimeError("injected marker batch failure")
        return original_put(self, collection, *args, **kwargs)

    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", fail_third_marker)
    with pytest.raises(SharedTrustAuditActivationPreflightError, match="injected marker"):
        stage_shared_trust_audit_activation(
            object_store_root=root,
            target_database_path=target,
            authority=authority,
            namespace_id="default",
            activation_id="controlled-activation-v1",
            migration=migration,
        )
    records = SQLiteStructuredRecordStore(target)
    assert records.list("aggregate_authority_targets") == ()
    assert authority.list_records() == ()


def test_partial_marker_or_authority_state_fails_closed(tmp_path: Path) -> None:
    root, _store, target, migration, authority = _migrated(tmp_path)
    evidence = migration.activation_evidence(
        namespace_id="default",
        activation_id="controlled-activation-v1",
    )
    records = SQLiteStructuredRecordStore(target)
    member = sorted(MEMBERS)[0]
    with records.begin() as transaction:
        transaction.put(
            "aggregate_authority_targets",
            f"default~{member}",
            {
                "namespace_id": "default",
                "aggregate": member,
                "migration_id": migration.migration_id,
                "source_fingerprint": migration.source_fingerprint,
                "target_fingerprint": migration.target_fingerprint,
                "target_identity": evidence.target_identity,
            },
            expected_revision=0,
        )
        transaction.commit()
    with pytest.raises(SharedTrustAuditActivationPreflightError, match="partial or drifted"):
        stage_shared_trust_audit_activation(
            object_store_root=root,
            target_database_path=target,
            authority=authority,
            namespace_id="default",
            activation_id="controlled-activation-v1",
            migration=migration,
        )

    root, _store, target, migration, authority = _migrated(tmp_path / "authority")
    for item in sorted(MEMBERS):
        authority.create_json_active(namespace_id="default", aggregate=item, reason="test")
    first = sorted(MEMBERS)[0]
    authority.transition(
        namespace_id="default",
        aggregate=first,
        expected_revision=1,
        to_state="sqlite_staged",
        reason="partial test",
        evidence=AggregateAuthorityEvidence(
            migration.migration_id,
            migration.source_fingerprint,
            migration.target_fingerprint,
            "sqlite:structured-records-v1",
        ),
    )
    with pytest.raises(SharedTrustAuditActivationPreflightError, match="partial or incompatible"):
        stage_shared_trust_audit_activation(
            object_store_root=root,
            target_database_path=target,
            authority=authority,
            namespace_id="default",
            activation_id="controlled-activation-v1",
            migration=migration,
        )


def test_controlled_preflight_reuses_durable_activation_service(tmp_path: Path) -> None:
    root, _store, target, migration, authority = _migrated(tmp_path)
    records = SQLiteStructuredRecordStore(target)
    service = SharedTrustAuditActivationSagaService(
        operations=SQLiteSharedTrustAuditActivationSagaStore(records),
        records=records,
        authority=authority,
    )

    completed = activate_verified_shared_trust_audit(
        object_store_root=root,
        target_database_path=target,
        authority=authority,
        namespace_id="default",
        activation_id="controlled-activation-v1",
        migration=migration,
        service=service,
        now="2026-07-13T00:00:00Z",
    )
    replayed = activate_verified_shared_trust_audit(
        object_store_root=root,
        target_database_path=target,
        authority=authority,
        namespace_id="default",
        activation_id="controlled-activation-v1",
        migration=migration,
        service=service,
        now="2026-07-13T01:00:00Z",
    )

    assert completed.operation.state == "finalized"
    assert replayed == completed
    assert {authority.get("default", member).state for member in MEMBERS} == {"sqlite_active"}
    assert {authority.get("default", member).revision for member in MEMBERS} == {3}
