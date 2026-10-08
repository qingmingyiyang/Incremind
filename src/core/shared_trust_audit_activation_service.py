"""Recoverable writer for the six-member Shared Trust Audit activation."""

from __future__ import annotations

from dataclasses import dataclass

from .memory_core import shared_trust_audit_activation_id, shared_trust_audit_activation_payload
from .storage_provider import (
    AggregateAuthorityEvidence,
    AggregateAuthorityTransition,
    SQLiteAggregateAuthorityStore,
    SQLiteSharedTrustAuditActivationSagaStore,
    SQLiteStructuredRecordStore,
    SharedTrustAuditActivationEvidence,
    SharedTrustAuditActivationOperation,
)


_ATTESTATIONS = "aggregate_authority_compound_activations"
_MARKERS = "aggregate_authority_targets"
_MEMBERS = (
    "memory_atoms",
    "memory_publications",
    "memory_scenarios",
    "memory_series_memory",
    "memory_transitions",
    "project_skills",
)


class SharedTrustAuditActivationServiceError(ValueError):
    """Raised when an activation cannot prove a safe next durable step."""


@dataclass(frozen=True, slots=True)
class SharedTrustAuditActivationServiceResult:
    operation: SharedTrustAuditActivationOperation
    attestation_id: str


class SharedTrustAuditActivationSagaService:
    """Advance one evidence-bound activation without bypassing CAS boundaries.

    The attestation and authority records intentionally reside in separate
    SQLite databases.  An interruption therefore leaves a durable operation
    in a recoverable state rather than attempting an unsafe implicit rollback.
    """

    def __init__(
        self,
        *,
        operations: SQLiteSharedTrustAuditActivationSagaStore,
        records: SQLiteStructuredRecordStore,
        authority: SQLiteAggregateAuthorityStore,
    ) -> None:
        self._operations = operations
        self._records = records
        self._authority = authority

    def activate(
        self,
        evidence: SharedTrustAuditActivationEvidence,
        *,
        now: str | None = None,
    ) -> SharedTrustAuditActivationServiceResult:
        operation = self._operations.prepare(evidence, now=now)
        attestation_id = shared_trust_audit_activation_id(operation.evidence.namespace_id)
        if operation.state == "finalized":
            return SharedTrustAuditActivationServiceResult(operation, attestation_id)

        if operation.state == "prepared":
            self._require_all(operation.evidence, "sqlite_staged")
            self._write_attestation(operation)
            operation = self._operations.advance(
                operation.operation_id,
                operation.revision,
                "attestation_written",
                now=now,
            )

        if operation.state == "attestation_written":
            self._require_attestation(operation)
            states = self._states(operation.evidence)
            if all(state == "sqlite_staged" for state in states):
                self._activate_authorities(operation.evidence)
            elif not all(state == "sqlite_active" for state in states):
                raise SharedTrustAuditActivationServiceError(
                    "activation authorities are partially transitioned"
                )
            operation = self._operations.advance(
                operation.operation_id,
                operation.revision,
                "authorities_activated",
                now=now,
            )

        if operation.state == "authorities_activated":
            self._require_attestation(operation)
            self._require_all(operation.evidence, "sqlite_active")
            operation = self._operations.advance(
                operation.operation_id,
                operation.revision,
                "finalized",
                now=now,
            )

        if operation.state != "finalized":  # pragma: no cover - store validates states
            raise SharedTrustAuditActivationServiceError("activation operation has invalid state")
        return SharedTrustAuditActivationServiceResult(operation, attestation_id)

    def _write_attestation(self, operation: SharedTrustAuditActivationOperation) -> None:
        expected = _attestation_payload(operation)
        attestation_id = shared_trust_audit_activation_id(operation.evidence.namespace_id)
        with self._records.begin() as uow:
            current = uow.read(_ATTESTATIONS, attestation_id)
            if current is None:
                uow.put(_ATTESTATIONS, attestation_id, expected, expected_revision=0)
                uow.commit()
                return
            if dict(current.payload) != expected:
                raise SharedTrustAuditActivationServiceError("activation attestation drifted")
            uow.commit()

    def _require_attestation(self, operation: SharedTrustAuditActivationOperation) -> None:
        record = self._records.read(
            _ATTESTATIONS,
            shared_trust_audit_activation_id(operation.evidence.namespace_id),
        )
        if record is None or dict(record.payload) != _attestation_payload(operation):
            raise SharedTrustAuditActivationServiceError("activation attestation is missing or drifted")

    def _states(self, evidence: SharedTrustAuditActivationEvidence) -> tuple[str, ...]:
        records = self._require_matching_records(evidence)
        return tuple(record.state for record in records)

    def _require_all(self, evidence: SharedTrustAuditActivationEvidence, state: str) -> None:
        states = self._states(evidence)
        if not all(current == state for current in states):
            raise SharedTrustAuditActivationServiceError(
                f"activation authorities are not all {state}"
            )

    def _require_matching_records(self, evidence: SharedTrustAuditActivationEvidence):
        expected_by_member = {
            member: AggregateAuthorityEvidence(
                evidence.member_migrations[member],
                evidence.source_fingerprint,
                evidence.target_fingerprint,
                evidence.target_identity,
            )
            for member in _MEMBERS
        }
        records = []
        for member in _MEMBERS:
            record = self._authority.get(evidence.namespace_id, member)
            if record is None or record.evidence != expected_by_member[member]:
                raise SharedTrustAuditActivationServiceError(
                    f"activation authority evidence is missing or drifted for {member}"
                )
            marker = self._records.read(_MARKERS, f"{evidence.namespace_id}~{member}")
            if marker is None or dict(marker.payload) != _marker_payload(
                evidence.namespace_id, member, expected_by_member[member]
            ):
                raise SharedTrustAuditActivationServiceError(
                    f"activation target marker is missing or drifted for {member}"
                )
            records.append(record)
        return tuple(records)

    def _activate_authorities(self, evidence: SharedTrustAuditActivationEvidence) -> None:
        staged = self._require_matching_records(evidence)
        if not all(record.state == "sqlite_staged" for record in staged):
            raise SharedTrustAuditActivationServiceError("activation authorities are not all staged")
        transitions = tuple(
            AggregateAuthorityTransition(
                namespace_id=evidence.namespace_id,
                aggregate=record.aggregate,
                expected_revision=record.revision,
                to_state="sqlite_active",
                reason=f"shared Trust Audit activation {evidence.activation_id} verified",
                evidence=record.evidence,
            )
            for record in staged
        )
        self._authority.transition_many(transitions)


def _attestation_payload(operation: SharedTrustAuditActivationOperation) -> dict[str, object]:
    evidence = operation.evidence
    return shared_trust_audit_activation_payload(
        namespace_id=evidence.namespace_id,
        target_identity=evidence.target_identity,
        activation_id=evidence.activation_id,
        member_migrations=evidence.member_migrations,
        source_fingerprint=evidence.source_fingerprint,
        target_fingerprint=evidence.target_fingerprint,
        activated_at=operation.created_at,
    )


def _marker_payload(
    namespace_id: str,
    aggregate: str,
    evidence: AggregateAuthorityEvidence,
) -> dict[str, object]:
    return {
        "namespace_id": namespace_id,
        "aggregate": aggregate,
        "migration_id": evidence.migration_id,
        "source_fingerprint": evidence.source_fingerprint,
        "target_fingerprint": evidence.target_fingerprint,
        "target_identity": evidence.target_identity,
    }
