from pathlib import Path

import pytest

from core.memory_core import (
    SharedTrustAuditActivationError,
    require_shared_trust_audit_activation,
    shared_trust_audit_activation_id,
    shared_trust_audit_activation_payload,
)
from core.storage_provider import SQLiteStructuredRecordStore


TARGET_IDENTITY = "sqlite:structured-records-v1"
MEMBERS = (
    "memory_atoms",
    "memory_publications",
    "memory_scenarios",
    "memory_series_memory",
    "memory_transitions",
    "project_skills",
)


def _records(tmp_path: Path) -> SQLiteStructuredRecordStore:
    return SQLiteStructuredRecordStore(tmp_path / "structured-records.sqlite3")


def _payload(**overrides: object) -> dict[str, object]:
    payload = shared_trust_audit_activation_payload(
        namespace_id="default",
        target_identity=TARGET_IDENTITY,
        activation_id="memory-publication-compound-v1",
        member_migrations={member: f"{member.replace('_', '-')}-v1" for member in MEMBERS},
        source_fingerprint="a" * 64,
        target_fingerprint="b" * 64,
        activated_at="2026-07-12T12:00:00+08:00",
    )
    return {**payload, **overrides}


def test_shared_trust_audit_activation_requires_exact_compound_attestation(tmp_path: Path) -> None:
    records = _records(tmp_path)
    with pytest.raises(SharedTrustAuditActivationError, match="not ready"):
        require_shared_trust_audit_activation(records, namespace_id="default", target_identity=TARGET_IDENTITY)

    with records.begin() as transaction:
        transaction.put("aggregate_authority_compound_activations", shared_trust_audit_activation_id("default"), _payload(), expected_revision=0)
        transaction.commit()

    activation = require_shared_trust_audit_activation(records, namespace_id="default", target_identity=TARGET_IDENTITY)
    assert activation.activation_id == "memory-publication-compound-v1"
    assert tuple(activation.member_migrations) == MEMBERS


@pytest.mark.parametrize(
    ("payload", "target_identity", "message"),
    [
        (_payload(target_identity="sqlite:other-v1"), TARGET_IDENTITY, "target mismatches"),
        (_payload(member_aggregates=list(MEMBERS[:-1])), TARGET_IDENTITY, "members are invalid"),
        (_payload(member_migrations={member: "valid-v1" for member in MEMBERS[:-1]}), TARGET_IDENTITY, "evidence is invalid"),
    ],
)
def test_shared_trust_audit_activation_rejects_drift(
    tmp_path: Path,
    payload: dict[str, object],
    target_identity: str,
    message: str,
) -> None:
    records = _records(tmp_path)
    with records.begin() as transaction:
        transaction.put("aggregate_authority_compound_activations", shared_trust_audit_activation_id("default"), payload, expected_revision=0)
        transaction.commit()

    with pytest.raises(SharedTrustAuditActivationError, match=message):
        require_shared_trust_audit_activation(records, namespace_id="default", target_identity=target_identity)
