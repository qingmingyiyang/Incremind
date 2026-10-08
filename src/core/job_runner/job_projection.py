from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.effect_log import EffectState
from core.task_reference_contract import (
    WORKBENCH_CONTENT_TRANSFORM_KIND,
    task_updated_utc_key,
    workbench_transform_task_tie_key,
)


_JOB_STATUS = {
    EffectState.PLANNED.value: "pending",
    EffectState.INFLIGHT.value: "running",
    EffectState.SETTLED_OK.value: "completed",
    EffectState.SETTLED_ERR.value: "failed",
    EffectState.UNKNOWN.value: "waiting_user",
    EffectState.COMPENSATED.value: "cancelled",
    EffectState.ABANDONED.value: "cancelled",
}

_STEP_STATUS = {
    EffectState.PLANNED.value: "pending",
    EffectState.INFLIGHT.value: "running",
    EffectState.SETTLED_OK.value: "completed",
    EffectState.SETTLED_ERR.value: "failed",
    EffectState.UNKNOWN.value: "waiting_user",
    EffectState.COMPENSATED.value: "cancelled",
    EffectState.ABANDONED.value: "cancelled",
}


@dataclass(frozen=True, slots=True)
class JobProjectionRecord:
    payload: Mapping[str, object]
    revision: int


def initialize_job_projection_schema(connection: sqlite3.Connection) -> None:
    # ``executescript`` commits an already-open SQLite transaction before it
    # runs.  Keep each schema statement enlisted in the caller-owned unit of
    # work so ``SQLiteJobStore.bind()`` cannot silently break atomicity.
    connection.execute(
        """CREATE TABLE IF NOT EXISTS job_effect_fact (
          job_id TEXT NOT NULL,
          sequence INTEGER NOT NULL CHECK(sequence > 0),
          effect_operation_id TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          recorded_at TEXT NOT NULL,
          PRIMARY KEY(job_id, sequence),
          FOREIGN KEY(effect_operation_id) REFERENCES effect(operation_id)
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_job_effect_fact_effect "
        "ON job_effect_fact(effect_operation_id)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS job_effect_node (
          job_id TEXT NOT NULL,
          node_kind TEXT NOT NULL CHECK(node_kind IN ('root','attempt','step')),
          node_key TEXT NOT NULL,
          attempt INTEGER NOT NULL CHECK(attempt >= 0),
          effect_operation_id TEXT NOT NULL UNIQUE,
          PRIMARY KEY(job_id, node_kind, node_key, attempt),
          FOREIGN KEY(effect_operation_id) REFERENCES effect(operation_id)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS job_projection (
          job_id TEXT PRIMARY KEY,
          payload_json TEXT NOT NULL,
          revision INTEGER NOT NULL CHECK(revision > 0),
          rebuilt_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS job_task_candidate_projection (
          job_id TEXT PRIMARY KEY,
          effect_kind TEXT NOT NULL,
          contract_version TEXT NOT NULL,
          updated_at_key TEXT NOT NULL,
          tie_key TEXT NOT NULL,
          fact_sequence INTEGER NOT NULL CHECK(fact_sequence > 0)
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_job_task_candidate_projection_page "
        "ON job_task_candidate_projection"
        "(effect_kind,contract_version,updated_at_key DESC,tie_key DESC)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS job_task_candidate_projection_meta "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


class JobProjectionBuilder:
    """Rebuild the Job query view from immutable facts and an Effect subtree."""

    def append_fact_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        effect_operation_id: str,
        payload: Mapping[str, object],
        recorded_at: str,
        minimum_sequence: int = 1,
    ) -> int:
        _require_non_empty("job_id", job_id)
        _require_non_empty("effect_operation_id", effect_operation_id)
        _require_non_empty("recorded_at", recorded_at)
        if payload.get("id") != job_id:
            raise ValueError("Job fact identity does not match job_id")
        effect = connection.execute(
            "SELECT root_id FROM effect WHERE operation_id=?",
            (effect_operation_id,),
        ).fetchone()
        if effect is None or str(effect[0]) != job_id:
            raise ValueError("Job fact must reference an Effect in the Job subtree")
        if minimum_sequence <= 0:
            raise ValueError("Job fact sequence must be positive")
        next_sequence = int(connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM job_effect_fact WHERE job_id=?",
            (job_id,),
        ).fetchone()[0])
        sequence = max(next_sequence, minimum_sequence)
        encoded = json.dumps(
            job_fact_payload(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            "INSERT INTO job_effect_fact(job_id,sequence,effect_operation_id,payload_json,recorded_at) "
            "VALUES(?,?,?,?,?)",
            (job_id, sequence, effect_operation_id, encoded, recorded_at),
        )
        return sequence

    def register_node_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        node_kind: str,
        node_key: str,
        attempt: int,
        effect_operation_id: str,
    ) -> None:
        if node_kind not in {"root", "attempt", "step"}:
            raise ValueError("invalid Job Effect node kind")
        if attempt < 0:
            raise ValueError("Job Effect node attempt must be non-negative")
        for name, value in (
            ("job_id", job_id),
            ("node_key", node_key),
            ("effect_operation_id", effect_operation_id),
        ):
            _require_non_empty(name, value)
        effect = connection.execute(
            "SELECT root_id FROM effect WHERE operation_id=?",
            (effect_operation_id,),
        ).fetchone()
        if effect is None or str(effect[0]) != job_id:
            raise ValueError("Job node must reference an Effect in the Job subtree")
        connection.execute(
            "INSERT OR IGNORE INTO job_effect_node"
            "(job_id,node_kind,node_key,attempt,effect_operation_id) VALUES(?,?,?,?,?)",
            (job_id, node_kind, node_key, attempt, effect_operation_id),
        )
        row = connection.execute(
            "SELECT effect_operation_id FROM job_effect_node "
            "WHERE job_id=? AND node_kind=? AND node_key=? AND attempt=?",
            (job_id, node_kind, node_key, attempt),
        ).fetchone()
        if row is None or str(row[0]) != effect_operation_id:
            raise ValueError("Job Effect node identity drifted")

    def rebuild_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        rebuilt_at: str,
    ) -> JobProjectionRecord:
        """Explicitly materialize the derived query cache for rollback tooling."""

        record = self.derive_in_connection(connection, job_id=job_id)
        encoded = json.dumps(
            record.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            "INSERT INTO job_projection(job_id,payload_json,revision,rebuilt_at) VALUES(?,?,?,?) "
            "ON CONFLICT(job_id) DO UPDATE SET payload_json=excluded.payload_json,"
            "revision=excluded.revision,rebuilt_at=excluded.rebuilt_at",
            (job_id, encoded, record.revision, rebuilt_at),
        )
        _rebuild_task_candidate_in_connection(connection, record)
        _mark_fresh_candidate_projection_ready_in_connection(connection)
        return record

    @staticmethod
    def derive_in_connection(
        connection: sqlite3.Connection,
        *,
        job_id: str,
    ) -> JobProjectionRecord:
        """Derive one Job read model without updating compatibility caches."""

        fact = connection.execute(
            "SELECT payload_json,sequence FROM job_effect_fact "
            "WHERE job_id=? ORDER BY sequence DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if fact is None:
            raise KeyError(job_id)
        payload = json.loads(str(fact[0]))
        if not isinstance(payload, dict) or payload.get("id") != job_id:
            raise ValueError("stored Job fact payload is invalid")
        nodes = connection.execute(
            "SELECT n.node_kind,n.node_key,n.attempt,e.state,e.lease_owner,e.lease_expires_at "
            "FROM job_effect_node n JOIN effect e ON e.operation_id=n.effect_operation_id "
            "WHERE n.job_id=? ORDER BY n.attempt,n.node_kind,n.node_key",
            (job_id,),
        ).fetchall()
        attempts = [row for row in nodes if str(row[0]) == "attempt"]
        if not attempts:
            raise ValueError("Job projection has no Effect attempt node")
        latest_attempt = max(attempts, key=lambda row: int(row[2]))
        attempt = int(latest_attempt[2])
        payload["attempt"] = attempt
        payload["status"] = _JOB_STATUS[str(latest_attempt[3])]
        lease_projection = payload.pop("_lease_projection", None)
        payload["lease"] = _project_lease(lease_projection, latest_attempt)
        step_states = {
            str(row[1]): _STEP_STATUS[str(row[3])]
            for row in nodes
            if str(row[0]) == "step" and int(row[2]) == attempt
        }
        attempt_step_status = _STEP_STATUS[str(latest_attempt[3])]
        steps = payload.get("steps")
        if isinstance(steps, list):
            projected_steps: list[object] = []
            for index, value in enumerate(steps):
                if not isinstance(value, Mapping):
                    projected_steps.append(value)
                    continue
                step = dict(value)
                key = _step_key(index, step)
                if key in step_states:
                    step["status"] = step_states[key]
                elif not step_states:
                    # A single domain Effect may represent the entire Job
                    # attempt. Pure display steps then inherit that Effect
                    # state instead of becoming a second execution authority.
                    step["status"] = attempt_step_status
                projected_steps.append(step)
            payload["steps"] = projected_steps
        return JobProjectionRecord(payload, int(fact[1]))

    def rebuild_all_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        rebuilt_at: str,
    ) -> tuple[JobProjectionRecord, ...]:
        job_ids = tuple(str(row[0]) for row in connection.execute(
            "SELECT DISTINCT job_id FROM job_effect_fact ORDER BY job_id"
        ).fetchall())
        return tuple(
            self.rebuild_in_connection(connection, job_id=job_id, rebuilt_at=rebuilt_at)
            for job_id in job_ids
        )

    @staticmethod
    def derive_all_in_connection(
        connection: sqlite3.Connection,
    ) -> tuple[JobProjectionRecord, ...]:
        """Derive every Job read model without materializing a cache row."""

        job_ids = tuple(str(row[0]) for row in connection.execute(
            "SELECT DISTINCT job_id FROM job_effect_fact ORDER BY job_id"
        ).fetchall())
        return tuple(
            JobProjectionBuilder.derive_in_connection(connection, job_id=job_id)
            for job_id in job_ids
        )


def rebuild_task_candidate_projection_in_connection(
    connection: sqlite3.Connection,
) -> None:
    """Rebuild the disposable candidate index from immutable Job facts.

    The caller owns the transaction. If any fact cannot be derived, the prior
    index and its coverage marker remain intact after rollback.
    """
    connection.execute("DELETE FROM job_task_candidate_projection")
    records = JobProjectionBuilder.derive_all_in_connection(connection)
    for record in records:
        _rebuild_task_candidate_in_connection(connection, record)
    connection.execute(
        "INSERT INTO job_task_candidate_projection_meta(key,value) VALUES('coverage','ready-v1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def task_candidate_projection_is_ready_in_connection(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT value FROM job_task_candidate_projection_meta WHERE key='coverage'"
    ).fetchone()
    return row is not None and str(row[0]) == "ready-v1"


def _mark_fresh_candidate_projection_ready_in_connection(connection: sqlite3.Connection) -> None:
    """Mark a complete one-fact authority ready, otherwise mark it unready.

    Schema initialization itself must not start a transaction: admission
    callers initialize then own ``BEGIN IMMEDIATE``. Older databases with any
    larger pre-existing history remain on the read-only scan fallback until
    explicit backfill or a full projection rebuild. The marker makes the
    coverage decision once, avoiding a whole-history count on later writes.
    """
    covered = connection.execute(
        "SELECT value FROM job_task_candidate_projection_meta WHERE key='coverage'"
    ).fetchone()
    if covered is not None:
        return
    facts = connection.execute(
        "SELECT job_id FROM job_effect_fact LIMIT 2"
    ).fetchall()
    if len(facts) == 1:
        connection.execute(
            "INSERT INTO job_task_candidate_projection_meta(key,value) VALUES('coverage','ready-v1')"
        )
    else:
        connection.execute(
            "INSERT INTO job_task_candidate_projection_meta(key,value) VALUES('coverage','unready-v1')"
        )


def _rebuild_task_candidate_in_connection(
    connection: sqlite3.Connection, record: JobProjectionRecord,
) -> None:
    payload = record.payload
    job_id = payload.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("Job candidate projection identity is invalid")
    effect = connection.execute(
        """SELECT effect.kind,effect.contract_version
             FROM job_effect_fact AS fact JOIN effect
               ON effect.operation_id=fact.effect_operation_id
            WHERE fact.job_id=? ORDER BY fact.sequence DESC LIMIT 1""",
        (job_id,),
    ).fetchone()
    if effect is None:
        raise ValueError("Job candidate projection has no latest Effect")
    effect_kind, contract_version = str(effect[0]), str(effect[1])
    if (
        payload.get("job_type") != WORKBENCH_CONTENT_TRANSFORM_KIND
        or payload.get("execution_version") != "effect-v2"
        or effect_kind != WORKBENCH_CONTENT_TRANSFORM_KIND
        or contract_version != "effect-v2"
    ):
        connection.execute(
            "DELETE FROM job_task_candidate_projection WHERE job_id=?", (job_id,)
        )
        return
    updated_at = payload.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at:
        connection.execute(
            "DELETE FROM job_task_candidate_projection WHERE job_id=?", (job_id,)
        )
        return
    connection.execute(
        """INSERT INTO job_task_candidate_projection
           (job_id,effect_kind,contract_version,updated_at_key,tie_key,fact_sequence)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET
             effect_kind=excluded.effect_kind, contract_version=excluded.contract_version,
             updated_at_key=excluded.updated_at_key, tie_key=excluded.tie_key,
             fact_sequence=excluded.fact_sequence""",
        (
            job_id, effect_kind, contract_version,
            task_updated_utc_key(updated_at), workbench_transform_task_tie_key(job_id),
            record.revision,
        ),
    )


def load_effect_execution_projection(
    database: str | Path, job_id: str,
) -> dict[str, object]:
    """Return the redacted Core Effect subtree used by one Job read model.

    This read-only DTO assembler deliberately excludes intent payloads, Receipt
    bodies and the legacy Job cache.  Detail and SSE can therefore expose the
    execution truth without introducing another authority.
    """

    _require_non_empty("job_id", job_id)
    path = Path(database).expanduser().resolve(strict=False)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT n.node_kind,n.node_key,n.attempt,e.operation_id,
                   e.effect_class,e.state,r.operation_id AS receipt_operation_id,
                   CASE WHEN c.operation_id IS NULL THEN 0 ELSE 1 END AS cancellation_requested
              FROM job_effect_node n
              JOIN effect e ON e.operation_id=n.effect_operation_id
              LEFT JOIN effect_receipt r ON r.operation_id=e.operation_id
              LEFT JOIN effect_cancellation_request c ON c.operation_id=e.operation_id
             WHERE n.job_id=?
             ORDER BY n.attempt,n.node_kind,n.node_key
            """,
            (job_id,),
        ).fetchall()
        history_available = False
        history_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_job_history'"
        ).fetchone()
        if history_table is not None:
            history_available = connection.execute(
                "SELECT 1 FROM legacy_job_history WHERE job_id=? LIMIT 1", (job_id,),
            ).fetchone() is not None
    finally:
        connection.close()

    if not rows and history_available:
        return {
            "schema_version": 1,
            "authority": "legacy_history",
            "available": True,
            "nodes": [],
        }

    nodes: list[dict[str, object]] = []
    for row in rows:
        nodes.append({
            "node_kind": str(row["node_kind"]),
            "node_key": str(row["node_key"]),
            "attempt": int(row["attempt"]),
            "operation_id": str(row["operation_id"]),
            "effect_class": str(row["effect_class"]),
            "state": str(row["state"]),
            "has_receipt": row["receipt_operation_id"] is not None,
            "cancellation_requested": bool(row["cancellation_requested"]),
        })
    return {
        "schema_version": 1,
        "authority": "core_effect_log",
        "available": True,
        "nodes": nodes,
    }


def load_companion_execution_projection(
    database: str | Path, *, now: int,
) -> dict[str, str]:
    """Return the bounded pet signal directly from Effect-backed Job attempts.

    The query never reads the legacy Job payload, identifiers, owners or lease
    values into the response.  A stale INFLIGHT attempt remains visible as an
    attention state, while only an unexpired Effect lease means active work.
    """

    if not isinstance(now, int) or isinstance(now, bool) or now < 0:
        raise ValueError("now must be a non-negative integer")
    path = Path(database).expanduser().resolve(strict=False)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            """
            SELECT
              MAX(CASE WHEN e.lease_owner IS NOT NULL AND e.lease_expires_at>? THEN 1 ELSE 0 END),
              COUNT(*)
              FROM job_effect_node n
              JOIN effect e ON e.operation_id=n.effect_operation_id
             WHERE n.node_kind='attempt' AND e.state='INFLIGHT'
            """,
            (now,),
        ).fetchone()
    finally:
        connection.close()
    has_valid_lease = bool(row and row[0])
    inflight_count = int(row[1]) if row else 0
    if has_valid_lease:
        return {"effect": "active", "lease": "valid"}
    if inflight_count > 0:
        return {"effect": "active", "lease": "unavailable"}
    return {"effect": "inactive", "lease": "none"}


def job_root_operation_id(job_id: str) -> str:
    _require_non_empty("job_id", job_id)
    return f"job-root:{job_id}"


def job_attempt_operation_id(job_id: str, attempt: int) -> str:
    _require_non_empty("job_id", job_id)
    if attempt < 0:
        raise ValueError("Job attempt must be non-negative")
    return f"job-attempt:{job_id}:{attempt}"


def job_step_operation_id(job_id: str, attempt: int, index: int) -> str:
    _require_non_empty("job_id", job_id)
    if attempt < 0 or index < 0:
        raise ValueError("Job step coordinates must be non-negative")
    return f"job-step:{job_id}:{attempt}:{index}"


def job_execution_operation_id(job_id: str, attempt: int) -> str:
    _require_non_empty("job_id", job_id)
    if attempt < 0:
        raise ValueError("Job attempt must be non-negative")
    return f"job-execution:{job_id}:{attempt}"


class JobEffectProjectionAuthority:
    """Materialize explicitly non-executable Job projection records.

    Executable Job facts and nodes are admitted by the caller-owned v2 command.
    This compatibility writer is deliberately unable to plan or mutate Effects.
    """

    def __init__(self) -> None:
        pass

    def record_snapshot_in_connection(
        self,
        connection: sqlite3.Connection,
        payload: Mapping[str, object],
        *,
        minimum_sequence: int = 1,
    ) -> JobProjectionRecord:
        job_id = str(payload.get("id", ""))
        _require_non_empty("job_id", job_id)
        if payload.get("execution_version") != "projection-only":
            raise ValueError("snapshot writer accepts only projection-only Jobs")
        now_text = _snapshot_time(payload)
        record = JobProjectionRecord(dict(payload), minimum_sequence)
        encoded = json.dumps(
            record.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        connection.execute(
            "INSERT INTO job_projection(job_id,payload_json,revision,rebuilt_at) VALUES(?,?,?,?) "
            "ON CONFLICT(job_id) DO UPDATE SET payload_json=excluded.payload_json,"
            "revision=excluded.revision,rebuilt_at=excluded.rebuilt_at",
            (job_id, encoded, record.revision, now_text),
        )
        return record

def _step_key(index: int, step: Mapping[str, object]) -> str:
    name = step.get("name")
    return f"{index}:{name}" if isinstance(name, str) and name else str(index)


def _project_lease(source: object, attempt_row: sqlite3.Row) -> Mapping[str, object] | None:
    if str(attempt_row[3]) != EffectState.INFLIGHT.value:
        return None
    expires_at = attempt_row[5]
    if expires_at is None:
        raise ValueError("INFLIGHT Job Effect has no valid lease")
    if isinstance(source, Mapping):
        worker_id = source.get("worker_id")
        lease_token = source.get("lease_token")
        acquired_at = source.get("acquired_at")
        expires_at_text = source.get("expires_at")
        if all(
            isinstance(value, str) and value
            for value in (worker_id, lease_token, acquired_at, expires_at_text)
        ):
            if _expiry_timestamp(str(expires_at_text)) != float(expires_at):
                raise ValueError("INFLIGHT Job Effect lease expiry projection drifted")
            return {
                "worker_id": worker_id,
                "lease_token": lease_token,
                "acquired_at": acquired_at,
                "expires_at": expires_at_text,
            }
    owner = attempt_row[4]
    if not isinstance(owner, str) or not owner:
        raise ValueError("INFLIGHT Job Effect has no lease owner")
    return {
        "authority": "core_effect_log",
        "worker_id": owner,
        "expires_at": datetime.fromtimestamp(
            float(expires_at), timezone.utc,
        ).isoformat(),
    }


def _projection_record(row: sqlite3.Row) -> JobProjectionRecord:
    payload = json.loads(str(row[0]))
    if not isinstance(payload, dict):
        raise ValueError("stored Job projection is invalid")
    return JobProjectionRecord(payload, int(row[1]))


def _require_non_empty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")


def job_fact_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Remove fields derived from Effect state before storing an immutable fact."""

    fact = dict(payload)
    fact.pop("status", None)
    fact.pop("attempt", None)
    lease = fact.pop("lease", None)
    if isinstance(lease, Mapping):
        fact["_lease_projection"] = dict(lease)
    steps = fact.get("steps")
    if isinstance(steps, list):
        projected: list[object] = []
        for value in steps:
            if not isinstance(value, Mapping):
                projected.append(value)
                continue
            step = dict(value)
            step.pop("status", None)
            projected.append(step)
        fact["steps"] = projected
    return fact


def _non_authoritative_fact_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Compatibility alias for the admission module's internal replay check."""

    return job_fact_payload(payload)


def _snapshot_time(payload: Mapping[str, object]) -> str:
    lease = payload.get("lease")
    if isinstance(lease, Mapping):
        acquired_at = lease.get("acquired_at")
        if isinstance(acquired_at, str) and acquired_at:
            _timestamp(acquired_at)
            return acquired_at
    for value in (
        payload.get("updated_at"),
        payload.get("created_at"),
        payload.get("occurred_at"),
    ):
        if isinstance(value, str) and value:
            _timestamp(value)
            return value
    return "1970-01-01T00:00:00+00:00"


def _timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Job fact time is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("Job fact time must include timezone")
    return parsed.astimezone(timezone.utc).timestamp()


def _expiry_timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Job lease expiry is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("Job lease expiry must include timezone")
    return parsed.astimezone(timezone.utc).timestamp()
