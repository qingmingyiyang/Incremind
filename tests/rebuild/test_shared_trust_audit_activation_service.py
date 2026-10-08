from pathlib import Path

import pytest

from core.memory_core import (
    require_shared_trust_audit_activation,
    shared_trust_audit_activation_id,
)
from core.shared_trust_audit_activation_service import (
    SharedTrustAuditActivationSagaService,
    SharedTrustAuditActivationServiceError,
)
from core.storage_provider import (
    AggregateAuthorityEvidence,
    AggregateAuthorityTransition,
    SQLiteAggregateAuthorityStore,
    SQLiteSharedTrustAuditActivationSagaStore,
    SQLiteStructuredRecordStore,
    SharedTrustAuditActivationEvidence,
)


MEMBERS = (
    "memory_atoms",
    "memory_publications",
    "memory_scenarios",
    "memory_series_memory",
    "memory_transitions",
    "project_skills",
)
TARGET = "sqlite:structured-records-v1"


def _evidence() -> SharedTrustAuditActivationEvidence:
    return SharedTrustAuditActivationEvidence(
        namespace_id="default",
        activation_id="compound-activation-v1",
        member_migrations={member: f"{member}-v1" for member in MEMBERS},
        source_fingerprint="a" * 64,
        target_fingerprint="b" * 64,
    )


def _setup(tmp_path: Path):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    authority = SQLiteAggregateAuthorityStore(tmp_path / "authority.sqlite3")
    evidence = _evidence()
    with records.begin() as uow:
        for member in MEMBERS:
            uow.put(
                "aggregate_authority_targets",
                f"default~{member}",
                {
                    "namespace_id": "default",
                    "aggregate": member,
                    "migration_id": evidence.member_migrations[member],
                    "source_fingerprint": evidence.source_fingerprint,
                    "target_fingerprint": evidence.target_fingerprint,
                    "target_identity": TARGET,
                },
                expected_revision=0,
            )
        uow.commit()
    for member in MEMBERS:
        initial = authority.create_json_active(
            namespace_id="default", aggregate=member, reason="test setup"
        )
        authority.transition(
            namespace_id="default",
            aggregate=member,
            expected_revision=initial.revision,
            to_state="sqlite_staged",
            reason="test setup",
            evidence=AggregateAuthorityEvidence(
                evidence.member_migrations[member], "a" * 64, "b" * 64, TARGET
            ),
        )
    operations = SQLiteSharedTrustAuditActivationSagaStore(records)
    return records, authority, operations, evidence


def _service(records, authority, operations):
    return SharedTrustAuditActivationSagaService(
        operations=operations, records=records, authority=authority
    )


def test_activation_writes_exact_attestation_and_is_replay_safe(tmp_path: Path) -> None:
    records, authority, operations, evidence = _setup(tmp_path)
    service = _service(records, authority, operations)

    completed = service.activate(evidence, now="2026-07-12T00:00:00Z")
    replayed = service.activate(evidence, now="2026-07-12T01:00:00Z")

    assert completed.operation.state == "finalized"
    assert replayed.operation == completed.operation
    assert [authority.get("default", member).revision for member in MEMBERS] == [3] * 6
    activation = require_shared_trust_audit_activation(
        records, namespace_id="default", target_identity=TARGET
    )
    assert activation.activated_at == "2026-07-12T00:00:00Z"


def test_prepared_operation_recovers_after_attestation_write_interruption(tmp_path: Path) -> None:
    records, authority, operations, evidence = _setup(tmp_path)
    service = _service(records, authority, operations)
    prepared = operations.prepare(evidence, now="2026-07-12T00:00:00Z")

    service._write_attestation(prepared)
    completed = service.activate(evidence)

    assert completed.operation.state == "finalized"
    assert [authority.get("default", member).state for member in MEMBERS] == ["sqlite_active"] * 6


def test_authority_activation_interruption_finalizes_without_second_revision(tmp_path: Path) -> None:
    records, authority, operations, evidence = _setup(tmp_path)
    service = _service(records, authority, operations)
    prepared = operations.prepare(evidence, now="2026-07-12T00:00:00Z")
    service._write_attestation(prepared)
    attested = operations.advance(
        prepared.operation_id, prepared.revision, "attestation_written"
    )
    staged = tuple(authority.get("default", member) for member in MEMBERS)
    authority.transition_many(
        tuple(
            AggregateAuthorityTransition(
                "default",
                record.aggregate,
                record.revision,
                "sqlite_active",
                "test interruption",
                record.evidence,
            )
            for record in staged
        )
    )

    completed = service.activate(evidence)

    assert attested.state == "attestation_written"
    assert completed.operation.state == "finalized"
    assert [authority.get("default", member).revision for member in MEMBERS] == [3] * 6


def test_partial_authority_or_marker_drift_fails_closed_without_new_activation(tmp_path: Path) -> None:
    records, authority, operations, evidence = _setup(tmp_path)
    service = _service(records, authority, operations)
    prepared = operations.prepare(evidence)
    service._write_attestation(prepared)
    operations.advance(prepared.operation_id, prepared.revision, "attestation_written")
    first = authority.get("default", MEMBERS[0])
    authority.transition(
        namespace_id="default",
        aggregate=first.aggregate,
        expected_revision=first.revision,
        to_state="sqlite_active",
        reason="partial interruption",
        evidence=first.evidence,
    )

    with pytest.raises(SharedTrustAuditActivationServiceError, match="partially transitioned"):
        service.activate(evidence)
    assert [authority.get("default", member).state for member in MEMBERS] == [
        "sqlite_active",
        *(["sqlite_staged"] * 5),
    ]
    operation = operations.get(prepared.operation_id)
    assert operation is not None and operation.state == "attestation_written"
    with records.begin() as uow:
        marker = uow.read("aggregate_authority_targets", f"default~{MEMBERS[1]}")
        uow.put(
            "aggregate_authority_targets",
            marker.object_id,
            {**marker.payload, "target_fingerprint": "c" * 64},
            expected_revision=marker.revision,
        )
        uow.commit()
    with pytest.raises(SharedTrustAuditActivationServiceError, match="marker is missing or drifted"):
        service.activate(evidence)
    assert records.read(
        "aggregate_authority_compound_activations",
        shared_trust_audit_activation_id("default"),
    ) is not None


def test_attestation_drift_fails_closed_before_any_authority_transition(tmp_path: Path) -> None:
    records, authority, operations, evidence = _setup(tmp_path)
    service = _service(records, authority, operations)
    prepared = operations.prepare(evidence)
    service._write_attestation(prepared)
    attested = operations.advance(
        prepared.operation_id, prepared.revision, "attestation_written"
    )
    with records.begin() as uow:
        attestation = uow.read(
            "aggregate_authority_compound_activations",
            shared_trust_audit_activation_id("default"),
        )
        uow.put(
            "aggregate_authority_compound_activations",
            attestation.object_id,
            {**attestation.payload, "target_fingerprint": "c" * 64},
            expected_revision=attestation.revision,
        )
        uow.commit()

    with pytest.raises(SharedTrustAuditActivationServiceError, match="attestation is missing or drifted"):
        service.activate(evidence)
    assert attested.state == "attestation_written"
    assert [authority.get("default", member).state for member in MEMBERS] == ["sqlite_staged"] * 6
