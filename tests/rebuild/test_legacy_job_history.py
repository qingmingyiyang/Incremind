from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from core.job_runner import LegacyJobHistorySnapshot, SQLiteJobStore
from core.job_runner.legacy_history import (
    LegacyJobHistoryProjection,
    initialize_legacy_job_history_schema,
)


def _payload(*, status: str = "completed", **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "legacy-job-1",
        "job_type": "legacy-import",
        "status": status,
        "attempt": 0,
    }
    value.update(changes)
    return value


def _connection(tmp_path) -> sqlite3.Connection:
    connection = sqlite3.connect(tmp_path / "legacy-history.sqlite3")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _import(
    projection: LegacyJobHistoryProjection,
    connection: sqlite3.Connection,
    *,
    payload: dict[str, object] | None = None,
    revision: int = 1,
    imported_at: str | None = None,
):
    return projection.import_in_connection(
        connection,
        migration_id="legacy-job-history-v1",
        source_kind="sqlite-job-store-v1",
        source_ref="legacy-job-1",
        job_id="legacy-job-1",
        payload=payload or _payload(),
        revision=revision,
        imported_at=imported_at,
    )


def test_import_freezes_snapshot_and_is_exactly_idempotent(tmp_path) -> None:
    projection = LegacyJobHistoryProjection()
    with _connection(tmp_path) as connection:
        first = _import(
            projection, connection, revision=7,
            imported_at="2026-08-30T00:00:00Z",
        )
        replay = _import(
            projection, connection, revision=7, imported_at="ignored-on-replay",
        )
        assert first == replay
        assert first.readonly is True
        assert first.display_status == "completed"
        assert first.requires_manual_resolution is False
        assert connection.execute("SELECT COUNT(*) FROM legacy_job_history").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM legacy_job_history_import_fence").fetchone()[0] == 1


@pytest.mark.parametrize("changed", [_payload(status="failed"), _payload(),])
def test_import_rejects_payload_or_revision_drift(tmp_path, changed) -> None:
    projection = LegacyJobHistoryProjection()
    with _connection(tmp_path) as connection:
        _import(projection, connection)
        revision = 1 if changed["status"] == "failed" else 2
        with pytest.raises(ValueError, match="import drifted"):
            _import(projection, connection, payload=changed, revision=revision)


def test_read_rejects_history_fence_revision_or_source_mapping_drift(tmp_path) -> None:
    projection = LegacyJobHistoryProjection()
    with _connection(tmp_path) as connection:
        _import(projection, connection, revision=3)
        connection.execute(
            "UPDATE legacy_job_history_import_fence SET legacy_revision=2 "
            "WHERE job_id='legacy-job-1'"
        )
        with pytest.raises(ValueError, match="fence is inconsistent"):
            projection.read_in_connection(connection, job_id="legacy-job-1")

        connection.execute(
            "UPDATE legacy_job_history_import_fence SET legacy_revision=3 "
            "WHERE job_id='legacy-job-1'"
        )
        connection.execute(
            "INSERT INTO legacy_job_history(job_id,payload_json,legacy_revision,imported_at) "
            "VALUES(?,?,?,?)",
            ("other-job", '{"id":"other-job","status":"completed"}', 3, "now"),
        )
        connection.execute(
            "INSERT INTO legacy_job_history_import_fence("
            "migration_id,source_kind,source_ref,job_id,legacy_revision,imported_at"
            ") VALUES(?,?,?,?,?,?)",
            (
                "legacy-job-history-v2", "sqlite-job-store-v1", "legacy-job-1",
                "other-job", 3, "2026-08-30T00:00:00Z",
            ),
        )
        with pytest.raises(ValueError, match="fence is inconsistent|source identity drifted"):
            projection.all_in_connection(connection)


@pytest.mark.parametrize(
    ("status", "expected", "manual"),
    [
        ("completed", "completed", False),
        ("running", "legacy_unknown", True),
        ("failed", "failed", False),
        ("cancelled", "cancelled", False),
        ("pending", "legacy_unknown", True),
    ],
)
def test_display_mapping_is_readonly_and_requires_manual_resolution(tmp_path, status, expected, manual) -> None:
    projection = LegacyJobHistoryProjection()
    with _connection(tmp_path) as connection:
        record = _import(projection, connection, payload=_payload(status=status))
        assert (record.display_status, record.requires_manual_resolution, record.readonly) == (expected, manual, True)


def test_effect_v2_identity_collision_fails_closed_without_creating_job_facts(tmp_path) -> None:
    projection = LegacyJobHistoryProjection()
    with _connection(tmp_path) as connection:
        connection.execute(
            "CREATE TABLE job_effect_fact(job_id TEXT NOT NULL,effect_operation_id TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO job_effect_fact VALUES('legacy-job-1','eff2_existing')"
        )
        with pytest.raises(ValueError, match="executable Job authority"):
            _import(projection, connection)
        assert connection.execute("SELECT COUNT(*) FROM legacy_job_history").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
            "('job_effect_node','job_projection')"
        ).fetchone()[0] == 0


def test_caller_transaction_can_roll_back_history_and_fence_together(tmp_path) -> None:
    projection = LegacyJobHistoryProjection()
    connection = _connection(tmp_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _import(projection, connection)
        connection.rollback()
        with pytest.raises(KeyError):
            projection.read_in_connection(connection, job_id="legacy-job-1")
    finally:
        connection.close()


@pytest.mark.parametrize("contract_version", ["effect-v2", "legacy-v1"])
def test_orphan_effect_root_identity_collision_fails_closed(
    tmp_path, contract_version: str,
) -> None:
    projection = LegacyJobHistoryProjection()
    with _connection(tmp_path) as connection:
        connection.execute(
            "CREATE TABLE effect(operation_id TEXT PRIMARY KEY,root_id TEXT NOT NULL,contract_version TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO effect VALUES('orphan-effect','legacy-job-1',?)",
            (contract_version,),
        )
        with pytest.raises(ValueError, match="executable Job authority"):
            _import(projection, connection)
        assert connection.execute("SELECT COUNT(*) FROM legacy_job_history").fetchone()[0] == 0


def test_bulk_import_rolls_back_every_row_when_later_snapshot_is_invalid(tmp_path) -> None:
    database = tmp_path / "legacy-bulk.sqlite3"
    store = SQLiteJobStore(database)
    snapshots = (
        LegacyJobHistorySnapshot(
            source_ref="legacy-job-1",
            job_id="legacy-job-1",
            payload=_payload(),
            legacy_revision=1,
        ),
        LegacyJobHistorySnapshot(
            source_ref="legacy-job-2",
            job_id="legacy-job-2",
            payload={**_payload(), "id": "drifted-job"},
            legacy_revision=1,
        ),
    )

    with pytest.raises(ValueError, match="payload identity"):
        store.import_legacy_history(
            snapshots,
            migration_id="legacy-job-history-v1",
            source_kind="sqlite-job-store-v1",
            imported_at="2026-08-30T00:00:00Z",
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM legacy_job_history").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM legacy_job_history_import_fence"
        ).fetchone()[0] == 0


def test_two_connections_share_one_exact_import_fence(tmp_path) -> None:
    database = tmp_path / "legacy-concurrent.sqlite3"
    with sqlite3.connect(database) as connection:
        initialize_legacy_job_history_schema(connection)

    def import_once(_index: int):
        connection = sqlite3.connect(database, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            record = LegacyJobHistoryProjection().import_in_connection(
                connection,
                migration_id="legacy-job-history-v1",
                source_kind="sqlite-job-store-v1",
                source_ref="legacy-job-1",
                job_id="legacy-job-1",
                payload=_payload(),
                revision=1,
                imported_at="2026-08-30T00:00:00Z",
            )
            connection.commit()
            return record
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        records = tuple(pool.map(import_once, range(2)))

    assert records[0] == records[1]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM legacy_job_history").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM legacy_job_history_import_fence"
        ).fetchone()[0] == 1
