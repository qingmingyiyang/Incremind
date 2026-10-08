from __future__ import annotations

import sqlite3
import json

import pytest

from core.effect_log import EffectClass, EffectIntent, EffectLog, EffectState
from core.job_runner import (
    JobProjectionBuilder,
    initialize_job_projection_schema,
    job_attempt_operation_id,
    job_root_operation_id,
    job_step_operation_id,
)
from core.job_runner.job_projection import load_effect_execution_projection


def _intent(job_id: str, operation_id: str, *, kind: str, step_key: str) -> EffectIntent:
    return EffectIntent(
        session_id=f"job-session:{job_id}",
        root_id=job_id,
        step_key=step_key,
        kind=kind,
        effect_class=EffectClass.IDEMPOTENT,
        intent_ref=f"crp://effects/job-intents/{operation_id}",
        gate_decision_id="job-projection:v1",
        rev_set={"job_projection_schema": "1"},
        payload={"job_id": job_id, "operation_id": operation_id},
        operation_id_override=operation_id,
    )


def _connection(database) -> sqlite3.Connection:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    initialize_job_projection_schema(connection)
    return connection


def test_projection_rebuilds_only_from_effect_subtree_and_immutable_facts(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    effect_log = EffectLog(database)
    job_id = "job-1"
    root_id = job_root_operation_id(job_id)
    attempt_id = job_attempt_operation_id(job_id, 0)
    step_id = job_step_operation_id(job_id, 0, 0)
    for operation_id, kind, step_key in (
        (root_id, "job_root", "job:root"),
        (attempt_id, "job_attempt", "job:attempt:0"),
        (step_id, "job_step", "job:attempt:0:step:0"),
    ):
        effect_log.plan(
            _intent(job_id, operation_id, kind=kind, step_key=step_key),
            now=100,
        )
    effect_log.transition(
        attempt_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner=json.dumps({
            "worker_id": "worker-a",
            "lease_token": "token-a",
            "acquired_at": "1970-01-01T00:01:40+00:00",
            "expires_at": "1970-01-01T00:03:20+00:00",
        }, sort_keys=True, separators=(",", ":")),
        lease_expires_at=200,
    )
    effect_log.transition(
        step_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="worker-a:token-a",
        lease_expires_at=200,
    )
    effect_log.transition(
        step_id,
        expected=EffectState.INFLIGHT,
        target=EffectState.SETTLED_OK,
        now=110,
        result_ref="crp://receipts/job-1/step-0",
    )
    builder = JobProjectionBuilder()
    with _connection(database) as connection:
        builder.register_node_in_connection(
            connection,
            job_id=job_id,
            node_kind="root",
            node_key="root",
            attempt=0,
            effect_operation_id=root_id,
        )
        builder.register_node_in_connection(
            connection,
            job_id=job_id,
            node_kind="attempt",
            node_key="attempt",
            attempt=0,
            effect_operation_id=attempt_id,
        )
        builder.register_node_in_connection(
            connection,
            job_id=job_id,
            node_kind="step",
            node_key="0:work",
            attempt=0,
            effect_operation_id=step_id,
        )
        builder.append_fact_in_connection(
            connection,
            job_id=job_id,
            effect_operation_id=attempt_id,
            payload={
                "id": job_id,
                "job_type": "test",
                "status": "failed",
                "attempt": 99,
                "lease": {
                    "worker_id": "worker-a",
                    "lease_token": "token-a",
                    "acquired_at": "1970-01-01T00:01:40+00:00",
                    "expires_at": "1970-01-01T00:03:20+00:00",
                },
                "budget": {
                    "input_tokens": 4096,
                    "output_tokens": 1024,
                    "wall_seconds": 300,
                },
                "steps": [{"name": "work", "status": "failed"}],
            },
            recorded_at="2026-08-28T00:00:00Z",
        )
        first = builder.rebuild_in_connection(
            connection,
            job_id=job_id,
            rebuilt_at="2026-08-28T00:00:01Z",
        )
        connection.execute(
            "CREATE TABLE job_store(job_id TEXT PRIMARY KEY,payload_json TEXT,revision INTEGER)"
        )
        connection.execute(
            "INSERT INTO job_store VALUES(?,?,?)",
            (job_id, '{"id":"job-1","status":"completed"}', 777),
        )
        connection.execute("DELETE FROM job_projection")
        rebuilt = builder.rebuild_in_connection(
            connection,
            job_id=job_id,
            rebuilt_at="2026-08-28T00:00:02Z",
        )

    assert first == rebuilt
    assert rebuilt.revision == 1
    assert rebuilt.payload["status"] == "running"
    assert rebuilt.payload["attempt"] == 0
    assert rebuilt.payload["steps"][0]["status"] == "completed"
    assert rebuilt.payload["lease"]["worker_id"] == "worker-a"
    assert rebuilt.payload["lease"]["lease_token"] == "token-a"
    assert rebuilt.payload["lease"]["expires_at"] == "1970-01-01T00:03:20+00:00"
    assert rebuilt.payload["budget"] == {
        "input_tokens": 4096,
        "output_tokens": 1024,
        "wall_seconds": 300,
    }
    execution = load_effect_execution_projection(database, job_id=job_id)
    assert {
        node["node_kind"]: node["operation_id"] for node in execution["nodes"]
    } == {"root": root_id, "attempt": attempt_id, "step": step_id}


def test_fact_and_node_must_reference_the_same_job_effect_subtree(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    effect_log = EffectLog(database)
    operation_id = job_attempt_operation_id("other-job", 0)
    effect_log.plan(
        _intent("other-job", operation_id, kind="job_attempt", step_key="attempt:0"),
        now=100,
    )
    builder = JobProjectionBuilder()
    with _connection(database) as connection:
        with pytest.raises(ValueError, match="Job subtree"):
            builder.append_fact_in_connection(
                connection,
                job_id="job-1",
                effect_operation_id=operation_id,
                payload={"id": "job-1"},
                recorded_at="2026-08-28T00:00:00Z",
            )
        with pytest.raises(ValueError, match="Job subtree"):
            builder.register_node_in_connection(
                connection,
                job_id="job-1",
                node_kind="attempt",
                node_key="attempt",
                attempt=0,
                effect_operation_id=operation_id,
            )
