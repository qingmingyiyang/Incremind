from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from core.effect_log import (
    EFFECT_V2,
    NOT_APPLICABLE,
    V2_REVISION_KEYS,
    EffectClass,
    EffectIntent,
    EffectLog,
    EffectReceipt,
    EffectRunner,
    GateDecision,
    GateDecisionFact,
)
from core.job_runner import (
    JobAdmissionAuthorization,
    JobAdmissionCommandKind,
    JobProjectionBuilder,
    LegacyJobHistorySnapshot,
    SQLiteJobAdmissionCommand,
    SQLiteJobLeaseConflict,
    SQLiteJobStore,
)


def _job(job_id: str = "job-sqlite-1") -> dict[str, object]:
    return {"id": job_id, "status": "pending", "attempt": 0, "steps": []}


def _freeze_legacy_history(
    store: SQLiteJobStore, payload: dict[str, object],
) -> None:
    job_id = str(payload["id"])
    store.import_legacy_history(
        (LegacyJobHistorySnapshot(
            source_ref=job_id,
            job_id=job_id,
            payload=payload,
            legacy_revision=1,
        ),),
        migration_id="legacy-job-history-v1",
        source_kind="sqlite-job-store-v1",
        imported_at="2026-08-30T00:00:00Z",
    )


def _inject_pre_fence_execution_collision(
    database, *, job_id: str, collision_kind: str,
) -> None:
    """Model the 13b3 upgrade window: history and old executable rows coexist."""

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO effect("
            "operation_id,session_id,root_id,step_key,kind,effect_class,purpose,"
            "intent_ref,intent_digest,gate_decision_id,rev_set,state,occurred_at,recorded_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-pre-fence-effect", "legacy-session", job_id,
                "legacy-execution", "job_execution", "PURE", "primary",
                "intent:legacy", "digest:legacy", "gate:legacy", "{}", "PLANNED", 1, 1,
            ),
        )
        if collision_kind == "fact":
            connection.execute(
                "INSERT INTO job_effect_fact(job_id,sequence,effect_operation_id,payload_json,recorded_at) "
                "VALUES(?,?,?,?,?)",
                (job_id, 1, "legacy-pre-fence-effect", json.dumps({"id": job_id}), "legacy"),
            )
        elif collision_kind == "node":
            connection.execute(
                "INSERT INTO job_effect_node(job_id,node_kind,node_key,attempt,effect_operation_id) "
                "VALUES(?,?,?,?,?)",
                (job_id, "attempt", "legacy", 0, "legacy-pre-fence-effect"),
            )
        elif collision_kind != "effect":
            raise ValueError("unknown collision fixture")
        connection.execute(
            "INSERT INTO job_projection(job_id,payload_json,revision,rebuilt_at) VALUES(?,?,?,?)",
            (job_id, json.dumps({"id": job_id, "status": "pending"}), 1, "before"),
        )


def _inject_adopted_legacy_projection(
    database, *, payload: dict[str, object], revision: int = 1,
) -> None:
    """Model the complete legacy-to-Effect projection left by the old cutover."""

    job_id = str(payload["id"])
    attempt = int(payload["attempt"])
    root_operation = f"job-root:{job_id}"
    attempt_operation = f"job-attempt:{job_id}:{attempt}"
    with sqlite3.connect(database) as connection:
        for operation_id, step_key, kind, state in (
            (root_operation, "job:root", "job_root", "PLANNED"),
            (attempt_operation, f"job:attempt:{attempt}", "job_attempt", "PLANNED"),
        ):
            connection.execute(
                "INSERT INTO effect("
                "operation_id,session_id,root_id,step_key,kind,effect_class,purpose,"
                "intent_ref,intent_digest,gate_decision_id,rev_set,state,occurred_at,recorded_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id, "legacy-session", job_id, step_key, kind,
                    "PURE", "primary", f"intent:{operation_id}",
                    f"digest:{operation_id}", "gate:legacy", "{}", state, 1, 1,
                ),
            )
        projection = JobProjectionBuilder()
        projection.register_node_in_connection(
            connection,
            job_id=job_id,
            node_kind="root",
            node_key="root",
            attempt=0,
            effect_operation_id=root_operation,
        )
        projection.register_node_in_connection(
            connection,
            job_id=job_id,
            node_kind="attempt",
            node_key="attempt",
            attempt=attempt,
            effect_operation_id=attempt_operation,
        )
        projection.append_fact_in_connection(
            connection,
            job_id=job_id,
            effect_operation_id=attempt_operation,
            payload=payload,
            recorded_at="2026-08-29T00:00:00Z",
            minimum_sequence=revision,
        )


def _admit_v2_job(database) -> object:
    revisions = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
    revisions.update(policy="policy-job-store-v2", handler="handler-job-store-v2")
    gate = GateDecisionFact(
        decision=GateDecision.ALLOW,
        rule_ref="rule:job-store-v2",
        scope_ref="scope:job-store-v2",
        budget_after={},
        secret_scope="scope:job-store-secret",
        policy_revision="policy-job-store-v2",
    )
    authorization = JobAdmissionAuthorization(
        job_id="job-sqlite-1",
        admission_ref="facts:job-sqlite-1/attempt-0",
        command_kind=JobAdmissionCommandKind.ADMIT,
        gate_decision_id="gate:job-sqlite-1/attempt-0",
        gate_fact=gate,
        revision_set=revisions,
        intent_refs={"execution": "intent:job-sqlite-1/attempt-0"},
        admitted_at=100,
    )
    intent = EffectIntent(
        session_id="session-job-sqlite-1",
        root_id="job-sqlite-1",
        step_key="execution-attempt-0",
        kind="job_execution",
        effect_class=EffectClass.QUERYABLE,
        intent_ref="intent:job-sqlite-1/attempt-0",
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
        payload={
            **_job(),
            "job_type": "extract_memory",
            "created_at": "1970-01-01T00:01:40+00:00",
            "execution_version": "effect-v2",
        },
        authorization=authorization,
        intent=intent,
    )


def _settle_v2_job(database, operation_id: str) -> None:
    EffectRunner(EffectLog(database), owner_id="worker-job-store-v2").execute_planned(
        operation_id,
        lambda _effect: EffectReceipt(
            "receipt:job-sqlite-1/attempt-0",
            "job-execution.receipt",
            "job-execution-receipt/v2",
            "job-execution/v2",
        ),
        now=101,
    )


def test_legacy_history_is_readonly_for_retry_and_cancel(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    payload = _job("legacy-job")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE job_store(job_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,revision INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO job_store VALUES(?,?,?)", ("legacy-job", json.dumps(payload), 3),
        )
    store = SQLiteJobStore(database)
    store.import_legacy_job_store_history(
        migration_id="sqlite-job-store-history-v1",
        imported_at="2026-08-30T00:00:00Z",
    )

    for operation in (
        lambda: store.retry("legacy-job", now="2026-08-30T00:00:01Z"),
        lambda: store.request_cancel(
            "legacy-job", request_id="cancel-history", now="2026-08-30T00:00:01Z",
        ),
    ):
        with pytest.raises(SQLiteJobLeaseConflict, match="legacy Job is read-only"):
            operation()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM job_effect_fact").fetchone()[0] == 0


def test_capture_snapshot_is_explicit_projection_only_and_creates_no_effect(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = SQLiteJobStore(database)

    saved = store.create({
        **_job("capture-projection"),
        "job_type": "capture",
        "status": "completed",
        "updated_at": "2026-08-30T00:00:00Z",
    })

    assert saved.payload["execution_version"] == "projection-only"
    assert store.read("capture-projection") == saved
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM job_effect_fact").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM job_effect_node").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='job_store'"
        ).fetchone()[0] == 0


def test_workbench_coordination_snapshot_is_normalized_without_executable_effect(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = SQLiteJobStore(database)

    saved = store.create({
        **_job("workbench-coordination"),
        "job_type": "workbench_auto_intake",
        "status": "pending",
        "updated_at": "2026-08-30T00:00:00Z",
    })

    assert saved.payload["execution_version"] == "projection-only"
    assert saved.payload["projection_role"] == "workbench_auto_intake_coordination"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 0


@pytest.mark.parametrize("status", ("pending", "running"))
def test_direct_executable_job_snapshots_fail_closed_without_effects(tmp_path, status: str) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = SQLiteJobStore(database)

    with pytest.raises(SQLiteJobLeaseConflict, match="effect-v2 admission"):
        store.create({**_job("ordinary-job"), "status": status, "job_type": "extract_memory"})

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM job_effect_fact").fetchone()[0] == 0


def test_projection_deletion_rebuilds_from_effect_subtree_without_job_store(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    admitted = _admit_v2_job(database)
    store = SQLiteJobStore(database)
    _settle_v2_job(database, admitted.effect.operation_id)
    expected = store.read("job-sqlite-1")
    assert expected is not None
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM job_projection")

    rebuilt = store.rebuild_projection(
        "job-sqlite-1",
        rebuilt_at="2026-07-11T00:02:00Z",
    )

    assert rebuilt == expected
    assert store.read("job-sqlite-1") == expected


def test_legacy_job_store_import_is_safe_when_retired_table_never_existed(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = SQLiteJobStore(database)

    assert store.import_legacy_job_store_history(
        migration_id="sqlite-job-store-history-v2",
        imported_at="2026-09-01T00:00:00Z",
    ) == ()

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='job_store'"
        ).fetchone()[0] == 0


def test_current_projection_only_save_never_writes_retired_job_store(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = SQLiteJobStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE job_store(job_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,revision INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO job_store(job_id,payload_json,revision) VALUES(?,?,?)",
            ("retired-source", json.dumps(_job("retired-source")), 1),
        )

    store.create({
        **_job("current-projection-only"),
        "job_type": "capture",
        "status": "completed",
        "updated_at": "2026-09-01T00:00:00Z",
    })

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT job_id FROM job_store ORDER BY job_id"
        ).fetchall()
    assert [str(row[0]) for row in rows] == ["retired-source"]


def test_retired_job_store_projection_only_duplicate_is_skipped_without_import(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = SQLiteJobStore(database)
    saved = store.create({
        **_job("retired-projection-only"),
        "job_type": "capture",
        "status": "completed",
        "updated_at": "2026-09-01T00:00:00Z",
    })
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE job_store(job_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,revision INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO job_store(job_id,payload_json,revision) VALUES(?,?,?)",
            ("retired-projection-only", json.dumps(dict(saved.payload)), saved.revision),
        )

    assert store.import_legacy_job_store_history(
        migration_id="sqlite-job-store-history-v2",
        imported_at="2026-09-01T00:00:01Z",
    ) == ()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM legacy_job_history").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM legacy_job_history_import_fence"
        ).fetchone()[0] == 0
        source = connection.execute(
            "SELECT payload_json,revision FROM job_store WHERE job_id='retired-projection-only'"
        ).fetchone()
    assert source is not None
    assert json.loads(str(source[0])) == dict(saved.payload)
    assert int(source[1]) == saved.revision


def test_read_derives_latest_effect_state_without_materializing_cache(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    admitted = _admit_v2_job(database)
    store = SQLiteJobStore(database)
    with sqlite3.connect(database) as connection:
        cached = json.loads(connection.execute(
            "SELECT payload_json FROM job_projection WHERE job_id=?", ("job-sqlite-1",)
        ).fetchone()[0])
        assert cached["status"] == "pending"

    _settle_v2_job(database, admitted.effect.operation_id)

    derived = store.read("job-sqlite-1")
    assert derived is not None
    assert derived.payload["status"] == "completed"
    with sqlite3.connect(database) as connection:
        cached_after = json.loads(connection.execute(
            "SELECT payload_json FROM job_projection WHERE job_id=?", ("job-sqlite-1",)
        ).fetchone()[0])
    assert cached_after["status"] == "pending"


def test_read_and_all_ignore_stale_or_missing_projection_caches(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    _admit_v2_job(database)
    store = SQLiteJobStore(database)
    with sqlite3.connect(database) as connection:
        stale = json.dumps({"id": "job-sqlite-1", "status": "failed", "attempt": 999})
        connection.execute(
            "UPDATE job_projection SET payload_json=?,revision=? WHERE job_id=?",
            (stale, 999, "job-sqlite-1"),
        )

    record = store.read("job-sqlite-1")
    assert record is not None
    assert record.payload["status"] == "pending"
    assert record.revision == 1
    assert [item.payload["status"] for item in store.all()] == ["pending"]

    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM job_projection")

    assert store.read("job-sqlite-1") == record
    assert store.all() == (record,)


def test_derive_does_not_write_projection_cache(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    _admit_v2_job(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM job_projection")
        before = connection.execute("SELECT COUNT(*) FROM job_projection").fetchone()[0]
        derived = JobProjectionBuilder.derive_in_connection(
            connection, job_id="job-sqlite-1",
        )
        after = connection.execute("SELECT COUNT(*) FROM job_projection").fetchone()[0]

    assert derived.payload["status"] == "pending"
    assert before == after == 0


def test_legacy_job_store_upgrade_is_explicit_readonly_and_preserves_source(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    payload = _job("legacy-job")
    payload["status"] = "completed"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE job_store(job_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,revision INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO job_store VALUES(?,?,?)",
            ("legacy-job", json.dumps(payload), 7),
        )

    store = SQLiteJobStore(database)
    assert store.read("legacy-job") is None
    imported = store.import_legacy_job_store_history(
        migration_id="sqlite-job-store-history-v1",
        imported_at="2026-08-30T00:00:00Z",
    )
    replayed = store.import_legacy_job_store_history(
        migration_id="sqlite-job-store-history-v1",
        imported_at="2026-08-30T00:00:01Z",
    )
    with sqlite3.connect(database) as monitor:
        before = monitor.execute("PRAGMA data_version").fetchone()[0]
        first = store.read("legacy-job")
        listed = store.all()
        after = monitor.execute("PRAGMA data_version").fetchone()[0]
    with sqlite3.connect(database) as connection:
        fact_count = connection.execute(
            "SELECT COUNT(*) FROM job_effect_fact WHERE job_id='legacy-job'"
        ).fetchone()[0]
        effect_count = connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0]
        receipt_count = connection.execute("SELECT COUNT(*) FROM effect_receipt").fetchone()[0]
        history_count = connection.execute("SELECT COUNT(*) FROM legacy_job_history").fetchone()[0]
        fence_count = connection.execute(
            "SELECT COUNT(*) FROM legacy_job_history_import_fence"
        ).fetchone()[0]
        rollback = connection.execute(
            "SELECT payload_json,revision FROM job_store WHERE job_id='legacy-job'"
        ).fetchone()

    assert imported == replayed
    assert first is not None and first.revision == 7
    assert first.payload["status"] == "completed"
    assert first.payload["execution_version"] == "legacy-v1-readonly"
    assert listed == (first,)
    assert before == after
    assert fact_count == effect_count == receipt_count == 0
    assert history_count == fence_count == 1
    assert rollback is not None
    assert int(rollback[1]) == 7
    assert json.loads(str(rollback[0])) == payload


def test_legacy_job_store_upgrade_recovers_after_process_exit_before_commit(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    payload = {**_job("legacy-crash-job"), "status": "running"}
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE job_store(job_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,revision INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO job_store VALUES(?,?,?)",
            ("legacy-crash-job", json.dumps(payload), 9),
        )

    crash_script = """
import os
import sqlite3
import sys
from core.job_runner import LegacyJobHistorySnapshot
from core.job_runner.legacy_history import LegacyJobHistoryProjection

database = sys.argv[1]
connection = sqlite3.connect(database)
connection.row_factory = sqlite3.Row
connection.execute("PRAGMA foreign_keys=ON")
connection.execute("BEGIN IMMEDIATE")
LegacyJobHistoryProjection().import_in_connection(
    connection,
    migration_id="sqlite-job-store-history-v1",
    source_kind="sqlite-job-store-v1",
    source_ref="legacy-crash-job",
    job_id="legacy-crash-job",
    payload={"id": "legacy-crash-job", "status": "running", "attempt": 0, "steps": []},
    revision=9,
    imported_at="2026-08-30T00:00:00Z",
)
os._exit(73)
"""
    crashed = subprocess.run(
        [sys.executable, "-c", crash_script, str(database)],
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                filter(
                    None,
                    (
                        str(Path(__file__).resolve().parents[2] / "src"),
                        os.environ.get("PYTHONPATH", ""),
                    ),
                )
            ),
        },
    )
    assert crashed.returncode == 73

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name IN ('legacy_job_history','legacy_job_history_import_fence')"
        ).fetchone()[0] == 0
        source = connection.execute(
            "SELECT payload_json,revision FROM job_store WHERE job_id='legacy-crash-job'"
        ).fetchone()
    assert source is not None
    assert json.loads(str(source[0])) == payload
    assert int(source[1]) == 9

    store = SQLiteJobStore(database)
    first = store.import_legacy_job_store_history(
        migration_id="sqlite-job-store-history-v1",
        imported_at="2026-08-30T00:00:01Z",
    )
    replay = store.import_legacy_job_store_history(
        migration_id="sqlite-job-store-history-v1",
        imported_at="2026-08-30T00:00:02Z",
    )
    assert first == replay
    assert first[0].payload["status"] == "legacy_unknown"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM legacy_job_history").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM legacy_job_history_import_fence"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 0


def test_legacy_job_store_upgrade_rejects_prior_executable_backfill(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = SQLiteJobStore(database)
    _admit_v2_job(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE job_store(job_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,revision INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO job_store(job_id,payload_json,revision) VALUES(?,?,?)",
            ("job-sqlite-1", json.dumps(_job()), 1),
        )

    with pytest.raises(ValueError, match="executable Job authority"):
        store.import_legacy_job_store_history(
            migration_id="sqlite-job-store-history-v1",
            imported_at="2026-08-30T00:00:00Z",
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM legacy_job_history"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM job_effect_fact WHERE job_id='job-sqlite-1'"
        ).fetchone()[0] == 1


def test_legacy_job_store_upgrade_skips_exact_effect_adoption_on_restart(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    payload = {
        **_job("legacy-adopted-job"),
        "status": "running",
        "lease": {
            "worker_id": "retired-worker",
            "lease_token": "retired-token",
            "acquired_at": "2026-08-29T00:00:00Z",
            "expires_at": "2026-08-29T00:00:30Z",
        },
    }
    store = SQLiteJobStore(database)
    assert store.import_legacy_job_store_history(
        migration_id="schema-bootstrap", imported_at="2026-08-30T23:59:59Z",
    ) == ()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE job_store(job_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,revision INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO job_store(job_id,payload_json,revision) VALUES(?,?,?)",
            (payload["id"], json.dumps(payload), 1),
        )
    _inject_adopted_legacy_projection(database, payload=payload)

    first = store.import_legacy_job_store_history(
        migration_id="sqlite-job-store-history-v1",
        imported_at="2026-08-31T00:00:00Z",
    )
    replay = store.import_legacy_job_store_history(
        migration_id="sqlite-job-store-history-v1",
        imported_at="2026-08-31T00:00:01Z",
    )

    assert first == replay == ()
    projected = store.read("legacy-adopted-job")
    assert projected is not None
    assert projected.payload["status"] == "pending"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM legacy_job_history").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM legacy_job_history_import_fence"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM job_store WHERE job_id='legacy-adopted-job'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("damage", ("missing_root", "revision_gap", "payload_drift"))
def test_legacy_job_store_upgrade_rejects_incomplete_effect_adoption(
    tmp_path, damage: str,
) -> None:
    database = tmp_path / "jobs.sqlite3"
    payload = _job("legacy-partial-job")
    store = SQLiteJobStore(database)
    assert store.import_legacy_job_store_history(
        migration_id="schema-bootstrap", imported_at="2026-08-30T23:59:59Z",
    ) == ()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE job_store(job_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,revision INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO job_store(job_id,payload_json,revision) VALUES(?,?,?)",
            (payload["id"], json.dumps(payload), 1),
        )
    _inject_adopted_legacy_projection(database, payload=payload)
    with sqlite3.connect(database) as connection:
        if damage == "missing_root":
            connection.execute(
                "DELETE FROM job_effect_node WHERE job_id=? AND node_kind='root'",
                (payload["id"],),
            )
        elif damage == "revision_gap":
            connection.execute(
                "UPDATE job_store SET revision=2 WHERE job_id=?", (payload["id"],),
            )
        else:
            drifted = {**payload, "steps": [{"name": "drifted"}]}
            connection.execute(
                "UPDATE job_store SET payload_json=? WHERE job_id=?",
                (json.dumps(drifted), payload["id"]),
            )

    with pytest.raises(ValueError, match="executable Job authority"):
        store.import_legacy_job_store_history(
            migration_id="sqlite-job-store-history-v1",
            imported_at="2026-08-31T00:00:00Z",
        )


@pytest.mark.parametrize(
    "write_kind",
    ("store_create", "store_save", "transaction_create", "transaction_save"),
)
def test_frozen_legacy_identity_rejects_every_store_write_without_effects(
    tmp_path, write_kind: str,
) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = SQLiteJobStore(database)
    payload = _job("frozen-legacy-job")
    _freeze_legacy_history(store, payload)

    with pytest.raises(SQLiteJobLeaseConflict, match="legacy Job is read-only"):
        if write_kind == "store_create":
            store.create(payload)
        elif write_kind == "store_save":
            store.save(payload, expected_revision=0)
        else:
            with sqlite3.connect(database) as connection:
                connection.execute("BEGIN IMMEDIATE")
                transaction = store.bind(connection)
                if write_kind == "transaction_create":
                    transaction.create(payload)
                else:
                    transaction.save(payload, expected_revision=0)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM legacy_job_history").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM job_effect_fact").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM job_effect_node").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM job_projection").fetchone()[0] == 0


@pytest.mark.parametrize("collision_kind", ("fact", "node", "effect"))
def test_pre_fence_history_execution_collision_fails_closed_before_reads_or_rebuilds(
    tmp_path, collision_kind: str,
) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = SQLiteJobStore(database)
    payload = {**_job("legacy-pre-fence-job"), "status": "completed"}
    _freeze_legacy_history(store, payload)
    _inject_pre_fence_execution_collision(
        database, job_id="legacy-pre-fence-job", collision_kind=collision_kind,
    )

    for read in (
        lambda: store.read("legacy-pre-fence-job"),
        store.all,
        lambda: store.rebuild_projection(
            "legacy-pre-fence-job", rebuilt_at="after-single",
        ),
        lambda: store.rebuild_all_projections(rebuilt_at="after-all"),
        lambda: store.import_legacy_job_store_history(
            migration_id="startup-inventory-v1", imported_at="after-startup",
        ),
    ):
        with pytest.raises(ValueError, match="executable Job authority"):
            read()

    with sqlite3.connect(database) as connection:
        cache = connection.execute(
            "SELECT payload_json,revision,rebuilt_at FROM job_projection "
            "WHERE job_id='legacy-pre-fence-job'"
        ).fetchone()
        assert cache is not None
        assert json.loads(str(cache[0])) == {
            "id": "legacy-pre-fence-job", "status": "pending",
        }
        assert (int(cache[1]), str(cache[2])) == (1, "before")
        assert connection.execute(
            "SELECT COUNT(*) FROM legacy_job_history WHERE job_id='legacy-pre-fence-job'"
        ).fetchone()[0] == 1


def test_job_store_bind_does_not_commit_the_caller_transaction(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = SQLiteJobStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE caller_fact(value TEXT NOT NULL)")
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO caller_fact VALUES('must-rollback')")

        store.bind(connection)
        connection.rollback()

        assert connection.execute("SELECT COUNT(*) FROM caller_fact").fetchone()[0] == 0


def test_mixed_v2_and_legacy_history_rebuilds_without_rewriting_legacy_source(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    legacy_payload = {**_job("legacy-job"), "status": "running"}
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE job_store(job_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,revision INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO job_store VALUES(?,?,?)",
            ("legacy-job", json.dumps(legacy_payload), 4),
        )
    store = SQLiteJobStore(database)
    store.import_legacy_job_store_history(
        migration_id="sqlite-job-store-history-v1",
        imported_at="2026-08-30T00:00:00Z",
    )
    _admit_v2_job(database)

    rebuilt = store.rebuild_all_projections(rebuilt_at="2026-08-30T00:00:01Z")

    assert {str(record.payload["id"]) for record in rebuilt} == {
        "job-sqlite-1", "legacy-job",
    }
    assert store.read("legacy-job").payload["status"] == "legacy_unknown"
    assert store.read("job-sqlite-1").payload["execution_version"] == "effect-v2"
    with sqlite3.connect(database) as connection:
        source = connection.execute(
            "SELECT payload_json,revision FROM job_store WHERE job_id='legacy-job'"
        ).fetchone()
        cached_ids = {
            str(row[0]) for row in connection.execute("SELECT job_id FROM job_projection")
        }
    assert source is not None
    assert json.loads(str(source[0])) == legacy_payload
    assert int(source[1]) == 4
    assert cached_ids == {"job-sqlite-1"}
