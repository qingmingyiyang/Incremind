from __future__ import annotations

import sqlite3

import pytest

from core.effect_log import (
    EFFECT_V2,
    NOT_APPLICABLE,
    V2_REVISION_KEYS,
    EffectClass,
    EffectIntent,
    EffectLog,
    GateDecision,
    GateDecisionFact,
)
from core.job_runner.admission import JobAdmissionAuthorization, JobAdmissionCommandKind
from core.job_runner.execution_admission import JobExecutionAdmissionAuthority
from core.job_runner.job_projection import JobProjectionBuilder, initialize_job_projection_schema
from core.job_runner.legacy_history import LegacyJobHistoryProjection


def _revisions() -> dict[str, str]:
    values = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
    values.update(policy="policy-job-v2", handler="handler-job-v2", budget="budget-job-v2")
    return values


def _gate() -> GateDecisionFact:
    return GateDecisionFact(
        decision=GateDecision.ALLOW,
        rule_ref="rule:job-execution",
        scope_ref="scope:job-execution",
        budget_after={},
        secret_scope="scope:secret-job-execution",
        policy_revision="policy-job-v2",
    )


def _authorization() -> JobAdmissionAuthorization:
    return JobAdmissionAuthorization(
        job_id="job-execution-1",
        admission_ref="facts:job-admission-execution-1",
        command_kind=JobAdmissionCommandKind.ADMIT,
        gate_decision_id="gate:job-execution-1",
        gate_fact=_gate(),
        revision_set=_revisions(),
        intent_refs={"job_execution": "intent:job-execution-1"},
        admitted_at=100,
    )


def _intent(**changes: object) -> EffectIntent:
    values: dict[str, object] = {
        "session_id": "session-job-execution-1",
        "root_id": "job-execution-1",
        "step_key": "execution",
        "kind": "job_execution",
        "effect_class": EffectClass.IDEMPOTENT,
        "intent_ref": "intent:job-execution-1",
        "gate_decision_id": "gate:job-execution-1",
        "rev_set": _revisions(),
        "payload": {
            "job_ref": "facts:job-admission-execution-1",
            "admission_ref": "facts:job-admission-execution-1",
            "mode": "admit",
            "attempt_index": 0,
        },
        "contract_version": EFFECT_V2,
        "intent_schema_version": "job-execution/v2",
        "expected_receipt_kind": "job-execution.receipt",
        "expected_receipt_schema_version": "job-execution-receipt/v2",
    }
    values.update(changes)
    return EffectIntent(**values)  # type: ignore[arg-type]


def _payload(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": "job-execution-1",
        "attempt": 0,
        "job_type": "extract_memory",
        "created_at": "1970-01-01T00:01:40+00:00",
        "steps": [{"name": "execute", "status": "pending"}],
    }
    values.update(changes)
    return values


def _connection(log: EffectLog) -> sqlite3.Connection:
    connection = log._connect()
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    initialize_job_projection_schema(connection)
    return connection


def test_admission_binds_v2_gate_intent_fact_node_and_projection_atomically(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite3")
    builder = JobProjectionBuilder()
    authority = JobExecutionAdmissionAuthority(log, builder)
    with _connection(log) as connection:
        connection.execute("BEGIN IMMEDIATE")
        admitted = authority.admit_in_connection(
            connection, payload=_payload(), authorization=_authorization(), intent=_intent(),
        )
        assert admitted.created
        assert admitted.effect.operation_id.startswith("eff2_")
        assert admitted.record.payload["status"] == "pending"
        assert admitted.record.payload["attempt"] == 0
        assert admitted.record.payload["steps"] == [{"name": "execute", "status": "pending"}]
        for table in ("effect", "effect_gate_fact", "effect_intent_fact", "job_effect_fact", "job_effect_node", "job_projection"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
        assert connection.execute(
            "SELECT effect_operation_id FROM job_effect_node WHERE node_kind='attempt'"
        ).fetchone()[0] == admitted.effect.operation_id
        connection.commit()


def test_admission_maps_a_domain_specific_v2_effect_as_the_job_attempt(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite3")
    authority = JobExecutionAdmissionAuthority(log, JobProjectionBuilder())
    domain_intent = _intent(
        kind="memory_candidate_from_source_output",
        intent_schema_version="candidate-memory-job-execution-v2",
        expected_receipt_kind="candidate-memory-job-execution.receipt",
        expected_receipt_schema_version="candidate-memory-job-execution-receipt-v2",
    )
    with _connection(log) as connection:
        connection.execute("BEGIN IMMEDIATE")
        admitted = authority.admit_in_connection(
            connection,
            payload=_payload(job_type="extract_memory_candidate"),
            authorization=_authorization(),
            intent=domain_intent,
        )
        connection.commit()

    assert admitted.effect.kind == "memory_candidate_from_source_output"
    assert admitted.record.payload["status"] == "pending"


def test_admission_projection_deletion_rebuilds_from_effect_and_immutable_fact(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite3")
    builder = JobProjectionBuilder()
    authority = JobExecutionAdmissionAuthority(log, builder)
    with _connection(log) as connection:
        connection.execute("BEGIN IMMEDIATE")
        first = authority.admit_in_connection(
            connection, payload=_payload(), authorization=_authorization(), intent=_intent(),
        )
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM job_projection")
        rebuilt = builder.rebuild_in_connection(
            connection, job_id="job-execution-1", rebuilt_at="1970-01-01T00:01:41+00:00",
        )
        connection.commit()
    assert rebuilt == first.record


def test_admission_replay_is_idempotent_and_identity_drift_is_rejected(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite3")
    builder = JobProjectionBuilder()
    authority = JobExecutionAdmissionAuthority(log, builder)
    with _connection(log) as connection:
        connection.execute("BEGIN IMMEDIATE")
        first = authority.admit_in_connection(
            connection, payload=_payload(), authorization=_authorization(), intent=_intent(),
        )
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        replay = authority.admit_in_connection(
            connection, payload=_payload(), authorization=_authorization(), intent=_intent(),
        )
        assert not replay.created
        assert replay.effect.operation_id == first.effect.operation_id
        assert connection.execute("SELECT COUNT(*) FROM job_effect_fact").fetchone()[0] == 1
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="fact reference drifted"):
            authority.admit_in_connection(
                connection,
                payload=_payload(),
                authorization=_authorization(),
                intent=_intent(payload={
                    "job_ref": "facts:job-admission-execution-drift",
                    "admission_ref": "facts:job-admission-execution-1",
                    "mode": "admit",
                    "attempt_index": 0,
                }),
            )
        connection.rollback()
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 1


def test_admission_replay_rejects_job_metadata_and_attempt_drift(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite3")
    authority = JobExecutionAdmissionAuthority(log, JobProjectionBuilder())
    with _connection(log) as connection:
        connection.execute("BEGIN IMMEDIATE")
        authority.admit_in_connection(
            connection, payload=_payload(), authorization=_authorization(), intent=_intent(),
        )
        connection.commit()

        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="replay metadata drifted"):
            authority.admit_in_connection(
                connection,
                payload=_payload(job_type="media_hands"),
                authorization=_authorization(),
                intent=_intent(),
            )
        connection.rollback()

        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="attempt drifted"):
            authority.admit_in_connection(
                connection,
                payload=_payload(attempt=1),
                authorization=_authorization(),
                intent=_intent(),
            )
        connection.rollback()


def test_admission_leaves_all_facts_to_outer_rollback(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite3")
    authority = JobExecutionAdmissionAuthority(log, JobProjectionBuilder())
    with _connection(log) as connection:
        connection.execute("BEGIN IMMEDIATE")
        authority.admit_in_connection(
            connection, payload=_payload(), authorization=_authorization(), intent=_intent(),
        )
        connection.rollback()
        for table in ("effect", "effect_gate_fact", "effect_intent_fact", "job_effect_fact", "job_effect_node", "job_projection"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_admission_rejects_an_existing_readonly_legacy_identity(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite3")
    authority = JobExecutionAdmissionAuthority(log, JobProjectionBuilder())
    with _connection(log) as connection:
        connection.execute("BEGIN IMMEDIATE")
        LegacyJobHistoryProjection().import_in_connection(
            connection,
            migration_id="legacy-job-history-v1",
            source_kind="sqlite-job-store-v1",
            source_ref="job-execution-1",
            job_id="job-execution-1",
            payload=_payload(status="running"),
            revision=1,
            imported_at="2026-08-30T00:00:00Z",
        )
        with pytest.raises(ValueError, match="cannot be admitted"):
            authority.admit_in_connection(
                connection,
                payload=_payload(),
                authorization=_authorization(),
                intent=_intent(),
            )
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM legacy_job_history").fetchone()[0] == 1
        connection.rollback()
