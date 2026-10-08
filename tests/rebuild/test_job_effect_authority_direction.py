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
    EffectReceipt,
    EffectState,
    GateDecision,
    GateDecisionFact,
    shared_effect_runner,
)
from core.job_runner import (
    JobAdmissionAuthorization,
    JobAdmissionCommandKind,
    SQLiteJobAdmissionCommand,
    SQLiteJobLeaseConflict,
    SQLiteJobStore,
)


def _job(job_id: str = "job-effect-direction") -> dict[str, object]:
    return {
        "id": job_id,
        "job_type": "test",
        "status": "pending",
        "attempt": 0,
        "lease": None,
        "steps": [{"name": "work", "status": "pending"}],
        "updated_at": "2026-08-30T00:00:00Z",
    }


def _admit_v2_job(database) -> object:
    payload = {**_job(), "execution_version": EFFECT_V2}
    revisions = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
    revisions.update(policy="policy-job-effect-direction", handler="handler-job-effect-direction")
    authorization = JobAdmissionAuthorization(
        job_id="job-effect-direction",
        admission_ref="facts:job-effect-direction/attempt-0",
        command_kind=JobAdmissionCommandKind.ADMIT,
        gate_decision_id="gate:job-effect-direction/attempt-0",
        gate_fact=GateDecisionFact(
            decision=GateDecision.ALLOW,
            rule_ref="rule:job-effect-direction",
            scope_ref="scope:job-effect-direction",
            budget_after={},
            secret_scope="scope:job-effect-direction-secret",
            policy_revision="policy-job-effect-direction",
        ),
        revision_set=revisions,
        intent_refs={"execution": "intent:job-effect-direction/attempt-0"},
        admitted_at=1,
    )
    intent = EffectIntent(
        session_id="session-job-effect-direction",
        root_id="job-effect-direction",
        step_key="execution-attempt-0",
        kind="job_execution",
        effect_class=EffectClass.QUERYABLE,
        intent_ref="intent:job-effect-direction/attempt-0",
        gate_decision_id=authorization.gate_decision_id,
        rev_set=revisions,
        payload={
            "job_ref": authorization.admission_ref,
            "admission_ref": authorization.admission_ref,
            "mode": "admit",
            "attempt_index": 0,
        },
        contract_version=EFFECT_V2,
        intent_schema_version="job-execution/v2",
        expected_receipt_kind="job-execution.receipt",
        expected_receipt_schema_version="job-execution-receipt/v2",
    )
    return SQLiteJobAdmissionCommand(database).admit(
        payload=payload, authorization=authorization, intent=intent,
    )


def test_job_write_cannot_reverse_preexisting_effect_state_or_lease(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = SQLiteJobStore(database)
    admitted = _admit_v2_job(database)
    attempt_id = admitted.effect.operation_id
    effects = EffectLog(database)
    runner = shared_effect_runner(
        database, owner_role="job-effect-runner", lease_seconds=30,
    )
    runner.begin_planned(
        attempt_id,
        now=1,
        lease_expires_at=61,
    )
    before = effects.get(attempt_id)
    assert before is not None

    contradictory = {
        **_job(),
        "status": "completed",
        "lease": None,
        "steps": [{"name": "work", "status": "completed"}],
        "updated_at": "2026-08-30T00:00:02Z",
    }
    with pytest.raises(SQLiteJobLeaseConflict, match="effect-v2 admission"):
        store.save(
            {**contradictory, "execution_version": EFFECT_V2},
            expected_revision=admitted.record.revision,
        )

    after = effects.get(attempt_id)
    assert after is not None
    assert (after.state, after.lease_owner, after.result_ref) == (
        before.state,
        before.lease_owner,
        before.result_ref,
    )


def test_projection_rebuild_follows_effect_after_cache_and_rollback_rows_are_deleted(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = SQLiteJobStore(database)
    admitted = _admit_v2_job(database)
    attempt_id = admitted.effect.operation_id
    runner = shared_effect_runner(
        database, owner_role="job-effect-runner", lease_seconds=30,
    )
    runner.execute_planned(
        attempt_id,
        lambda _effect: EffectReceipt(
            "receipt:job-effect-direction/attempt-0",
            "job-execution.receipt",
            "job-execution-receipt/v2",
            "job-execution/v2",
        ),
        now=2,
    )

    expected = store.rebuild_projection(
        "job-effect-direction",
        rebuilt_at="2026-08-30T00:00:03Z",
    )
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM job_projection")
        connection.execute("DELETE FROM job_store")

    rebuilt = store.rebuild_projection(
        "job-effect-direction",
        rebuilt_at="2026-08-30T00:00:04Z",
    )

    assert rebuilt == expected
    assert rebuilt.payload["status"] == "completed"
    assert rebuilt.payload["lease"] is None
