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
from core.job_runner.job_projection import JobProjectionBuilder
from core.job_runner.legacy_history import LegacyJobHistorySnapshot
from core.job_runner.sqlite_admission import SQLiteJobAdmissionCommand
from core.job_runner.effect_commands import SQLiteJobEffectCommandAuthority
from core.job_runner.sqlite_store import SQLiteJobStore


def _revisions() -> dict[str, str]:
    values = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
    values.update(policy="policy-command-v2", handler="handler-command-v2")
    return values


def _authorization(
    *, attempt: int = 0, command_kind: JobAdmissionCommandKind = JobAdmissionCommandKind.ADMIT,
) -> JobAdmissionAuthorization:
    gate = GateDecisionFact(
        decision=GateDecision.ALLOW,
        rule_ref="rule:job-command",
        scope_ref=f"scope:job-command/attempt-{attempt}",
        budget_after={"attempt_index": attempt},
        secret_scope="scope:job-command-secret",
        policy_revision="policy-command-v2",
    )
    return JobAdmissionAuthorization(
        job_id="job-command-1",
        admission_ref=f"facts:job-command-1/attempt-{attempt}",
        command_kind=command_kind,
        gate_decision_id=f"gate:job-command-1/attempt-{attempt}",
        gate_fact=gate,
        revision_set=_revisions(),
        intent_refs={"execution": f"intent:job-command-1/attempt-{attempt}"},
        admitted_at=100,
    )


def _intent(
    *, attempt: int = 0, command_kind: JobAdmissionCommandKind = JobAdmissionCommandKind.ADMIT,
) -> EffectIntent:
    return EffectIntent(
        session_id="session-job-command-1",
        root_id="job-command-1",
        step_key=f"execution-attempt-{attempt}",
        kind="job_execution",
        effect_class=EffectClass.QUERYABLE,
        intent_ref=f"intent:job-command-1/attempt-{attempt}",
        gate_decision_id=f"gate:job-command-1/attempt-{attempt}",
        rev_set=_revisions(),
        payload={
            "job_ref": f"facts:job-command-1/attempt-{attempt}",
            "admission_ref": f"facts:job-command-1/attempt-{attempt}",
            "mode": command_kind.value,
            "attempt_index": attempt,
        },
        contract_version=EFFECT_V2,
        intent_schema_version="job-execution/v2",
        expected_receipt_kind="job-execution.receipt",
        expected_receipt_schema_version="job-execution-receipt/v2",
    )


def _payload(*, attempt: int = 0) -> dict[str, object]:
    return {
        "id": "job-command-1",
        "job_type": "extract_memory_candidate",
        "attempt": attempt,
        "created_at": "1970-01-01T00:01:40+00:00",
        "steps": [{"name": "create_candidate", "status": "pending"}],
    }


def test_sqlite_command_commits_core_facts_before_derived_projection(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    command = SQLiteJobAdmissionCommand(database)
    observed: list[bool] = []

    admitted = command.admit(
        payload=_payload(),
        authorization=_authorization(),
        intent=_intent(),
        preflight=lambda _connection, replay: observed.append(replay),
    )

    assert admitted.created is True
    assert admitted.effect.operation_id.startswith("eff2_")
    assert admitted.record.payload["status"] == "pending"
    assert observed == [False]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect_gate_fact").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM effect_intent_fact").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM job_effect_fact").fetchone()[0] == 1
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_store'"
        ).fetchone() is None


def test_sqlite_command_revalidates_exact_replay_without_capacity_recount(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    command = SQLiteJobAdmissionCommand(database)
    command.admit(payload=_payload(), authorization=_authorization(), intent=_intent())
    observed: list[bool] = []

    replay = command.admit(
        payload=_payload(), authorization=_authorization(), intent=_intent(),
        preflight=lambda _connection, exists: observed.append(exists),
    )

    assert replay.created is False
    assert observed == [True]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM job_effect_fact").fetchone()[0] == 1


@pytest.mark.parametrize(
    "command_kind",
    (JobAdmissionCommandKind.RETRY, JobAdmissionCommandKind.RESUME),
)
def test_sqlite_command_treats_a_new_authorized_attempt_as_creation_not_replay(
    tmp_path, command_kind: JobAdmissionCommandKind,
) -> None:
    database = tmp_path / "jobs.sqlite3"
    command = SQLiteJobAdmissionCommand(database)
    command.admit(payload=_payload(), authorization=_authorization(), intent=_intent())
    observed: list[bool] = []

    admitted = command.admit(
        payload=_payload(attempt=1),
        authorization=_authorization(attempt=1, command_kind=command_kind),
        intent=_intent(attempt=1, command_kind=command_kind),
        preflight=lambda _connection, replay: observed.append(replay),
    )

    assert admitted.created is True
    assert admitted.record.payload["attempt"] == 1
    assert observed == [False]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM job_effect_fact").fetchone()[0] == 2


def test_job_cancellation_records_only_core_effect_coordination_fact(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    admitted = SQLiteJobAdmissionCommand(database).admit(
        payload=_payload(), authorization=_authorization(), intent=_intent(),
    )

    result = SQLiteJobEffectCommandAuthority(database).request_cancellation(
        job_id="job-command-1",
        request_ref="crp://effects/cancellation/job-command-1/request-1",
        requested_at=101,
    )
    replay = SQLiteJobEffectCommandAuthority(database).request_cancellation(
        job_id="job-command-1",
        request_ref="crp://effects/cancellation/job-command-1/request-1",
        requested_at=102,
    )

    assert result.effect.operation_id == admitted.effect.operation_id
    assert result.record.payload["status"] == "pending"
    assert replay == result
    with sqlite3.connect(database) as connection:
        request = connection.execute(
            "SELECT operation_id,request_ref,requested_at FROM effect_cancellation_request"
        ).fetchone()
        assert request == (
            admitted.effect.operation_id,
            "crp://effects/cancellation/job-command-1/request-1",
            101,
        )
        assert connection.execute("SELECT COUNT(*) FROM job_effect_fact").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 1


def test_sqlite_command_rolls_back_gate_effect_and_projection_when_preflight_or_projection_fails(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    command = SQLiteJobAdmissionCommand(database)

    def reject(_connection: sqlite3.Connection, _replay: bool) -> None:
        raise ValueError("policy drift")

    with pytest.raises(ValueError, match="policy drift"):
        command.admit(
            payload=_payload(), authorization=_authorization(), intent=_intent(),
            preflight=reject,
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM job_effect_fact").fetchone()[0] == 0

    drifted = dict(_payload())
    drifted["attempt"] = 1
    with pytest.raises(ValueError, match="attempt drifted"):
        command.admit(
            payload=drifted, authorization=_authorization(), intent=_intent(),
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM effect_gate_fact").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM job_projection").fetchone()[0] == 0


def test_sqlite_command_projection_cache_can_be_rebuilt_without_job_store(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    SQLiteJobAdmissionCommand(database).admit(
        payload=_payload(), authorization=_authorization(), intent=_intent(),
    )
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("DELETE FROM job_projection")
        rebuilt = JobProjectionBuilder().rebuild_in_connection(
            connection,
            job_id="job-command-1",
            rebuilt_at="1970-01-01T00:01:41+00:00",
        )
        connection.commit()
    assert rebuilt.payload["status"] == "pending"
    assert rebuilt.payload["job_type"] == "extract_memory_candidate"


def test_sqlite_command_rejects_a_frozen_legacy_job_identity_atomically(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    SQLiteJobStore(database).import_legacy_history(
        (LegacyJobHistorySnapshot(
            source_ref="job-command-1",
            job_id="job-command-1",
            payload={**_payload(), "status": "running"},
            legacy_revision=1,
        ),),
        migration_id="legacy-job-history-v1",
        source_kind="sqlite-job-store-v1",
        imported_at="2026-08-30T00:00:00Z",
    )

    with pytest.raises(ValueError, match="cannot be admitted"):
        SQLiteJobAdmissionCommand(database).admit(
            payload=_payload(), authorization=_authorization(), intent=_intent(),
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM legacy_job_history").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM effect_gate_fact").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM job_effect_fact").fetchone()[0] == 0
