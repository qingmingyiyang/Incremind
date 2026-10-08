from __future__ import annotations

from core.storage_provider.connection_scope import reusable_connection

from collections.abc import Mapping, Sequence
from collections import OrderedDict
from threading import RLock
from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3
from core.storage_provider.observability import observe_connection
from uuid import uuid4
from datetime import datetime, timezone

from core.effect_log import (
    EffectClass,
    EffectIntent,
    EffectPurpose,
    EffectRunner,
    EffectState,
    InvalidEffectTransition,
    shared_effect_runner,
)

from .contracts import (
    validate_event_transition,
    validate_governed_payload,
    validate_model_wire_attempt_dispatch,
    validate_model_wire_attempt_receipt,
    validate_turn_request,
)
from .event_store import RunLeaseRevoked, TurnEventConflict
from .ports import (
    RecoveryDecision,
    RecoveryQueueItem,
    PublicRecoveryReview,
    RecoveryReviewAuthorization,
    RunLeaseRecord,
    RunLeaseRecoveryDisposition,
    RunLeaseToken,
    ApprovalBundleReceipt,
    HookReceiptBundleReceipt,
    ImmutablePayloadAppendReceipt,
    IntentBundleReceipt,
    ModelAttemptDispatchBundleReceipt,
    ModelAttemptTerminalBundleReceipt,
    ModelTerminalBundleReceipt,
    ToolOutcomeBundleReceipt,
    TurnReceipt,
    validate_run_lease_record,
    validate_recovery_decision,
    validate_recovery_queue_item,
    validate_recovery_review_authorization,
    validate_recovery_review_identity,
    validate_public_recovery_review,
    validate_run_lease_time,
    validate_run_lease_token,
)
from .state_store import TurnStateConflict
from .external_agent_context import validate_external_agent_safe_projection


class SQLiteAITurnStore:
    """Single durable authority for AI Turn state, events and referenced payloads."""

    schema_version = 1

    @classmethod
    def has_recovery_or_expert_wait_candidate(
        cls, database_path: Path, *, now: datetime,
    ) -> bool:
        """Read the narrow startup candidate set without creating or migrating a database.

        Recovery polling must retain its one-second lease deadline, but an
        empty fresh root has no durable Turn authority to recover.  Opening it
        through the normal constructor would create directories, run every
        schema migration and compose an Effect runner on every poll.  This
        probe deliberately uses SQLite read-only mode.  Only an absent path
        proves there is no authority to recover.  A readable legacy schema,
        a malformed database, or a transient lock remains a candidate so the
        normal constructor keeps its migration and retry behavior.
        """
        path = database_path.expanduser().resolve(strict=False)
        if not path.is_file():
            return False
        try:
            connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
            observe_connection(connection)
            try:
                return connection.execute(
                    "SELECT EXISTS("
                    "SELECT 1 FROM ai_turn_run_leases lease "
                    "LEFT JOIN ai_turn_recovery_audit audit "
                    "ON audit.turn_id=lease.turn_id AND audit.generation=lease.generation "
                    "WHERE audit.turn_id IS NULL AND "
                    "(lease.status='recovery_required' OR (lease.status='active' AND lease.stale_after<=?)) LIMIT 1"
                    ") OR EXISTS("
                    "SELECT 1 FROM ai_turn_recovery_queue q "
                    "JOIN ai_turn_run_leases l ON l.turn_id=q.turn_id AND l.generation=q.generation "
                    "WHERE q.status='pending' AND ("
                    "(q.reason_code='ai.recovery_no_effect_started' AND EXISTS(SELECT 1 FROM ai_turn_recovery_audit a WHERE a.turn_id=q.turn_id AND a.generation=q.generation AND a.disposition='safe_resume' AND a.reason_code=q.reason_code)) "
                    "OR (q.reason_code='ai.recovery_manual_confirmed_no_effect' AND EXISTS(SELECT 1 FROM ai_turn_recovery_reviews r WHERE r.turn_id=q.turn_id AND r.generation=q.generation AND r.status='resume_queued'))"
                    ") AND l.status='recovery_required' LIMIT 1"
                    ") OR EXISTS("
                    "SELECT 1 FROM ai_expert_job_waits WHERE status IN ('waiting','wake_enqueued') LIMIT 1"
                    ")",
                    (_lease_time(now),),
                ).fetchone()[0] == 1
            finally:
                connection.close()
        except sqlite3.Error:
            # Do not turn an old schema, a corruption diagnosis, or a lock
            # into a permanent empty verdict.  The existing constructor owns
            # schema migration and the one-second recovery pass retries any
            # transient open failure.
            return True

    def __init__(self, database_path: Path, *, effect_runner: EffectRunner | None = None, cache_immutable_reads: bool = False) -> None:
        self._immutable_reads = OrderedDict() if cache_immutable_reads else None
        self._immutable_read_lock = RLock()
        self._path = database_path.expanduser().resolve(strict=False)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            _initialize_schema(connection)
        finally:
            connection.close()
        self._effect_runner = effect_runner or shared_effect_runner(
            self._path, owner_role="ai-effect-runner",
        )
        self._effect_log = self._effect_runner.log

    @property
    def effect_runner(self) -> EffectRunner:
        return self._effect_runner

    def claim_turn(self, request: Mapping[str, object]) -> tuple[str, bool]:
        payload = dict(request)
        turn_id = str(payload["turn_id"])
        key = str(payload["idempotency_key"])
        encoded = _encode(payload)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT turn_id, request_json FROM ai_turns WHERE idempotency_key=?", (key,)).fetchone()
            if row is not None:
                if str(row[1]) != encoded:
                    raise TurnStateConflict("turn idempotency identity conflict")
                connection.execute("COMMIT")
                return str(row[0]), False
            if connection.execute("SELECT 1 FROM ai_turns WHERE turn_id=?", (turn_id,)).fetchone():
                raise TurnStateConflict("turn identity already exists")
            connection.execute("INSERT INTO ai_turns(turn_id, session_id, operation_id, idempotency_key, request_json) VALUES(?,?,?,?,?)", (turn_id, str(payload["session_id"]), str(payload["operation_id"]), key, encoded))
            connection.execute("COMMIT")
            return turn_id, True
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def _cached_immutable_read(self, key, sql, parameters):
        with self._immutable_read_lock:
            if self._immutable_reads is not None and key in self._immutable_reads:
                self._immutable_reads.move_to_end(key)
                return self._immutable_reads[key]
        connection = self._connect()
        try:
            row = connection.execute(sql, parameters).fetchone()
        finally:
            connection.close()
        if row is not None:
            with self._immutable_read_lock:
                if self._immutable_reads is not None:
                    self._immutable_reads[key] = row
                    if len(self._immutable_reads) > 256:
                        self._immutable_reads.popitem(last=False)
        return row

    def get_request(self, turn_id: str) -> Mapping[str, object] | None:
        row = self._cached_immutable_read(("request", turn_id),
            "SELECT request_json FROM ai_turns WHERE turn_id=?", (turn_id,))
        return _mapping_json(row[0]) if row else None

    def try_claim_run_lease(self, turn_id: str, owner_id: str) -> int | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM ai_turn_run_leases WHERE turn_id=?", (turn_id,)).fetchone():
                connection.execute("COMMIT")
                return None
            if not connection.execute("SELECT 1 FROM ai_turns WHERE turn_id=?", (turn_id,)).fetchone():
                raise TurnStateConflict("turn was not found")
            row = connection.execute("SELECT generation FROM ai_turn_run_generations WHERE turn_id=?", (turn_id,)).fetchone()
            generation = (int(row[0]) if row else 0) + 1
            connection.execute("INSERT INTO ai_turn_run_generations(turn_id, generation) VALUES(?,?) ON CONFLICT(turn_id) DO UPDATE SET generation=excluded.generation", (turn_id, generation))
            connection.execute("INSERT INTO ai_turn_run_leases(turn_id, owner_id, generation) VALUES(?,?,?)", (turn_id, owner_id, generation))
            connection.execute("COMMIT")
            return generation
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def release_run_lease(self, turn_id: str, owner_id: str, generation: int) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM ai_turn_run_leases WHERE turn_id=? AND owner_id=? AND generation=? AND status IS NULL", (turn_id, owner_id, generation))
            connection.execute("COMMIT")
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def try_acquire_run_lease(self, turn_id: str, owner_id: str, *, now: datetime, stale_after: datetime) -> RunLeaseToken | None:
        _lease_times(now, stale_after)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM ai_turn_run_leases WHERE turn_id=?", (turn_id,)).fetchone():
                connection.execute("COMMIT")
                return None
            if not connection.execute("SELECT 1 FROM ai_turns WHERE turn_id=?", (turn_id,)).fetchone():
                raise TurnStateConflict("turn was not found")
            row = connection.execute("SELECT generation FROM ai_turn_run_generations WHERE turn_id=?", (turn_id,)).fetchone()
            generation = (int(row[0]) if row else 0) + 1
            token = RunLeaseToken(turn_id, owner_id, generation)
            validate_run_lease_token(token)
            connection.execute("INSERT INTO ai_turn_run_generations(turn_id, generation) VALUES(?,?) ON CONFLICT(turn_id) DO UPDATE SET generation=excluded.generation", (turn_id, generation))
            connection.execute("INSERT INTO ai_turn_run_leases(turn_id, owner_id, generation, status, acquired_at, heartbeat_at, stale_after) VALUES(?,?,?,?,?,?,?)", (turn_id, owner_id, generation, "active", _lease_time(now), _lease_time(now), _lease_time(stale_after)))
            connection.execute("COMMIT")
            return token
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def renew_run_lease(self, token: RunLeaseToken, *, now: datetime, stale_after: datetime) -> RunLeaseRecord | None:
        validate_run_lease_token(token)
        _lease_times(now, stale_after)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT acquired_at, heartbeat_at FROM ai_turn_run_leases WHERE turn_id=? AND owner_id=? AND generation=? AND status='active'", (token.turn_id, token.owner_id, token.generation)).fetchone()
            if row is None or now < _lease_datetime(row[1]):
                connection.execute("COMMIT")
                return None
            connection.execute("UPDATE ai_turn_run_leases SET heartbeat_at=?, stale_after=? WHERE turn_id=? AND owner_id=? AND generation=? AND status='active'", (_lease_time(now), _lease_time(stale_after), token.turn_id, token.owner_id, token.generation))
            for effect_row in connection.execute(
                "SELECT operation_id FROM effect WHERE turn_id=? AND kind='model_call' "
                "AND state='INFLIGHT' AND lease_owner=?",
                (token.turn_id, self._effect_runner.owner_id),
            ).fetchall():
                effect = self._effect_log.get_in_connection(connection, str(effect_row[0]))
                self._effect_runner.renew(
                    effect, connection=connection,
                    lease_expires_at=stale_after.timestamp(),
                    now=now.timestamp(),
                )
            record = _lease_record(token, "active", _lease_datetime(row[0]), now, stale_after)
            connection.execute("COMMIT")
            return record
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def mark_run_lease_stale(self, token: RunLeaseToken, *, now: datetime) -> RunLeaseRecord | None:
        validate_run_lease_token(token)
        validate_run_lease_time(now, name="run lease stale check time")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT acquired_at, heartbeat_at, stale_after FROM ai_turn_run_leases WHERE turn_id=? AND owner_id=? AND generation=? AND status='active'", (token.turn_id, token.owner_id, token.generation)).fetchone()
            if row is None or now < _lease_datetime(row[2]):
                connection.execute("COMMIT")
                return None
            connection.execute("UPDATE ai_turn_run_leases SET status='recovery_required' WHERE turn_id=? AND owner_id=? AND generation=? AND status='active'", (token.turn_id, token.owner_id, token.generation))
            record = _lease_record(token, "recovery_required", _lease_datetime(row[0]), _lease_datetime(row[1]), _lease_datetime(row[2]))
            connection.execute("COMMIT")
            return record
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def takeover_run_lease(self, turn_id: str, *, expected_generation: int, owner_id: str, now: datetime, stale_after: datetime, disposition: RunLeaseRecoveryDisposition) -> RunLeaseRecord | None:
        _lease_times(now, stale_after)
        if disposition not in {"safe", "quarantined"}:
            raise ValueError("run lease recovery disposition is invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT owner_id, generation, acquired_at, heartbeat_at, stale_after FROM ai_turn_run_leases WHERE turn_id=? AND generation=? AND status='recovery_required'", (turn_id, expected_generation)).fetchone()
            if row is None or now < _lease_datetime(row[4]):
                connection.execute("COMMIT")
                return None
            old_token = RunLeaseToken(turn_id, str(row[0]), int(row[1]))
            if disposition == "quarantined":
                connection.execute("UPDATE ai_turn_run_leases SET status='quarantined' WHERE turn_id=? AND generation=? AND status='recovery_required'", (turn_id, expected_generation))
                record = _lease_record(old_token, "quarantined", _lease_datetime(row[2]), _lease_datetime(row[3]), _lease_datetime(row[4]))
            else:
                generation = expected_generation + 1
                token = RunLeaseToken(turn_id, owner_id, generation)
                validate_run_lease_token(token)
                connection.execute("INSERT INTO ai_turn_run_generations(turn_id, generation) VALUES(?,?) ON CONFLICT(turn_id) DO UPDATE SET generation=excluded.generation", (turn_id, generation))
                updated = connection.execute("UPDATE ai_turn_run_leases SET owner_id=?, generation=?, status='active', acquired_at=?, heartbeat_at=?, stale_after=? WHERE turn_id=? AND generation=? AND status='recovery_required'", (owner_id, generation, _lease_time(now), _lease_time(now), _lease_time(stale_after), turn_id, expected_generation))
                if updated.rowcount != 1:
                    connection.execute("ROLLBACK")
                    return None
                record = _lease_record(token, "active", now, now, stale_after)
            connection.execute("COMMIT")
            return record
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def assert_active_run_lease(self, token: RunLeaseToken) -> RunLeaseRecord | None:
        validate_run_lease_token(token)
        connection = self._connect()
        try:
            row = connection.execute("SELECT acquired_at, heartbeat_at, stale_after FROM ai_turn_run_leases WHERE turn_id=? AND owner_id=? AND generation=? AND status='active'", (token.turn_id, token.owner_id, token.generation)).fetchone()
            return _lease_record(token, "active", _lease_datetime(row[0]), _lease_datetime(row[1]), _lease_datetime(row[2])) if row else None
        finally:
            connection.close()

    def get_run_lease(self, turn_id: str) -> RunLeaseRecord | None:
        connection = self._connect()
        try:
            row = connection.execute("SELECT owner_id, generation, status, acquired_at, heartbeat_at, stale_after FROM ai_turn_run_leases WHERE turn_id=? AND status IS NOT NULL", (turn_id,)).fetchone()
            if row is None:
                return None
            token = RunLeaseToken(turn_id, str(row[0]), int(row[1]))
            return _lease_record(token, str(row[2]), _lease_datetime(row[3]), _lease_datetime(row[4]), _lease_datetime(row[5]))
        finally:
            connection.close()

    def claim_due_run_leases(self, *, now: datetime, limit: int) -> tuple[RunLeaseRecord, ...]:
        validate_run_lease_time(now, name="run lease claim time")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 256:
            raise ValueError("run lease claim limit is invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            candidates = connection.execute(
                "SELECT lease.turn_id FROM ai_turn_run_leases lease "
                "LEFT JOIN ai_turn_recovery_audit audit "
                "ON audit.turn_id=lease.turn_id AND audit.generation=lease.generation "
                "WHERE audit.turn_id IS NULL AND "
                "(lease.status='recovery_required' OR (lease.status='active' AND lease.stale_after<=?)) "
                "ORDER BY COALESCE(lease.recovery_attempts,0), "
                "COALESCE(lease.recovery_attempted_at,''), lease.stale_after, lease.turn_id LIMIT ?",
                (_lease_time(now), limit),
            ).fetchall()
            turn_ids = tuple(str(row[0]) for row in candidates)
            if not turn_ids:
                connection.execute("COMMIT")
                return ()
            placeholders = ",".join("?" for _ in turn_ids)
            updated = connection.execute(
                f"UPDATE ai_turn_run_leases SET status='recovery_required', "
                "recovery_attempts=COALESCE(recovery_attempts,0)+1, recovery_attempted_at=? "
                f"WHERE turn_id IN ({placeholders}) AND "
                "(status='recovery_required' OR (status='active' AND stale_after<=?))",
                (_lease_time(now), *turn_ids, _lease_time(now)),
            )
            if updated.rowcount != len(turn_ids):
                raise RuntimeError("run lease recovery claim lost atomic identity")
            rows = connection.execute(
                "SELECT turn_id, owner_id, generation, status, acquired_at, heartbeat_at, stale_after "
                f"FROM ai_turn_run_leases WHERE turn_id IN ({placeholders}) ORDER BY turn_id",
                turn_ids,
            ).fetchall()
            connection.execute("COMMIT")
            return tuple(_lease_record(RunLeaseToken(str(row[0]), str(row[1]), int(row[2])), str(row[3]), _lease_datetime(row[4]), _lease_datetime(row[5]), _lease_datetime(row[6])) for row in rows)
        except Exception:
            _rollback(connection); raise
        finally:
            connection.close()

    def record_recovery_decision(self, decision: RecoveryDecision, *, observed_at: datetime) -> bool:
        validate_recovery_decision(decision)
        validate_run_lease_time(observed_at, name="recovery observation time")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT owner_id,status,acquired_at,heartbeat_at,stale_after "
                "FROM ai_turn_run_leases WHERE turn_id=? AND generation=?",
                (decision.turn_id, decision.generation),
            ).fetchone()
            latest = connection.execute(
                "SELECT sequence,event_id,event_json FROM ai_turn_events "
                "WHERE turn_id=? ORDER BY sequence DESC LIMIT 1",
                (decision.turn_id,),
            ).fetchone()
            latest_identity = (
                (int(latest[0]), str(latest[1]), str(_mapping_json(latest[2]).get("type", "")))
                if latest is not None else (0, "", "")
            )
            if (
                lease is None
                or str(lease[1]) != "recovery_required"
                or latest_identity != (
                    decision.last_sequence,
                    decision.last_event_id,
                    decision.last_event_type,
                )
            ):
                connection.execute("COMMIT"); return False
            inserted = connection.execute(
                "INSERT OR IGNORE INTO ai_turn_recovery_audit("
                "turn_id,generation,old_owner_id,old_acquired_at,old_heartbeat_at,old_stale_after,"
                "disposition,reason_code,last_sequence,last_event_id,last_event_type,"
                "classifier_version,scanner_actor,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision.turn_id, decision.generation, str(lease[0]), str(lease[2]),
                    str(lease[3]), str(lease[4]), decision.disposition,
                    decision.reason_code, decision.last_sequence, decision.last_event_id,
                    decision.last_event_type, "ai-recovery-v1", "startup-scanner", _lease_time(observed_at),
                ),
            )
            if inserted.rowcount != 1:
                connection.execute("COMMIT"); return False
            if decision.disposition in {"terminal_noop", "waiting_noop"}:
                transitioned = connection.execute("DELETE FROM ai_turn_run_leases WHERE turn_id=? AND generation=? AND status='recovery_required'", (decision.turn_id, decision.generation))
            elif decision.disposition == "quarantine":
                transitioned = connection.execute("UPDATE ai_turn_run_leases SET status='quarantined' WHERE turn_id=? AND generation=? AND status='recovery_required'", (decision.turn_id, decision.generation))
            else:
                transitioned = connection.execute(
                    "INSERT INTO ai_turn_recovery_queue(turn_id,generation,reason_code,status,attempts,created_at) "
                    "VALUES(?,?,?,'pending',0,?)",
                    (decision.turn_id, decision.generation, decision.reason_code, _lease_time(observed_at)),
                )
            if transitioned.rowcount != 1:
                raise RuntimeError("recovery decision state transition lost identity")
            if decision.disposition == "quarantine":
                self._create_recovery_review(
                    connection, decision.turn_id, decision.generation, decision.reason_code,
                    decision.last_sequence, decision.last_event_id, decision.last_event_type, observed_at,
                )
            connection.execute("COMMIT"); return True
        except Exception:
            _rollback(connection); raise
        finally:
            connection.close()

    def claim_safe_recovery_queue(self, *, now: datetime, stale_after: datetime, limit: int) -> tuple[RecoveryQueueItem, ...]:
        _lease_times(now, stale_after)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 256:
            raise ValueError("recovery queue claim limit is invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT q.turn_id,q.generation,q.reason_code,q.attempts FROM ai_turn_recovery_queue q "
                "JOIN ai_turn_run_leases l ON l.turn_id=q.turn_id AND l.generation=q.generation "
                "WHERE q.status='pending' AND ("
                "(q.reason_code='ai.recovery_no_effect_started' AND EXISTS(SELECT 1 FROM ai_turn_recovery_audit a WHERE a.turn_id=q.turn_id AND a.generation=q.generation AND a.disposition='safe_resume' AND a.reason_code=q.reason_code)) "
                "OR (q.reason_code='ai.recovery_manual_confirmed_no_effect' AND EXISTS(SELECT 1 FROM ai_turn_recovery_reviews r WHERE r.turn_id=q.turn_id AND r.generation=q.generation AND r.status='resume_queued'))"
                ") "
                "AND l.status='recovery_required' "
                "ORDER BY q.created_at,q.turn_id LIMIT ?", (limit,)
            ).fetchall()
            claimed: list[RecoveryQueueItem] = []
            for row in rows:
                turn_id, prior, reason, attempts = str(row[0]), int(row[1]), str(row[2]), int(row[3])
                prior_generation = connection.execute("SELECT generation FROM ai_turn_run_generations WHERE turn_id=?", (turn_id,)).fetchone()
                if prior_generation is None or int(prior_generation[0]) != prior:
                    continue
                generation = prior + 1
                owner_id = f"recovery-{uuid4().hex}"
                if connection.execute(
                    "UPDATE ai_turn_run_leases SET owner_id=?,generation=?,status='active',acquired_at=?,heartbeat_at=?,stale_after=? WHERE turn_id=? AND generation=? AND status='recovery_required'",
                    (owner_id, generation, _lease_time(now), _lease_time(now), _lease_time(stale_after), turn_id, prior),
                ).rowcount != 1:
                    continue
                if connection.execute("UPDATE ai_turn_run_generations SET generation=? WHERE turn_id=? AND generation=?", (generation, turn_id, prior)).rowcount != 1:
                    raise RuntimeError("recovery generation authority lost")
                if connection.execute("UPDATE ai_turn_recovery_queue SET status='running',attempts=attempts+1 WHERE turn_id=? AND generation=? AND status='pending'", (turn_id, prior)).rowcount != 1:
                    raise RuntimeError("recovery queue claim lost atomic identity")
                connection.execute("INSERT INTO ai_turn_recovery_queue_audit(turn_id,generation,attempt,status,reason_code,owner_id,new_generation,observed_at) VALUES(?,?,?,'claimed',?,?,?,?)", (turn_id, prior, attempts + 1, reason, owner_id, generation, _lease_time(now)))
                claimed.append(RecoveryQueueItem(turn_id, prior, reason, attempts + 1, RunLeaseToken(turn_id, owner_id, generation)))
            connection.execute("COMMIT")
            return tuple(claimed)
        except Exception:
            _rollback(connection); raise
        finally:
            connection.close()

    def has_pending_safe_recovery(self) -> bool:
        connection = self._connect()
        try:
            return connection.execute(
                "SELECT 1 FROM ai_turn_recovery_queue q "
                "JOIN ai_turn_run_leases l ON l.turn_id=q.turn_id AND l.generation=q.generation "
                "WHERE q.status='pending' AND ("
                "(q.reason_code='ai.recovery_no_effect_started' AND EXISTS(SELECT 1 FROM ai_turn_recovery_audit a WHERE a.turn_id=q.turn_id AND a.generation=q.generation AND a.disposition='safe_resume' AND a.reason_code=q.reason_code)) "
                "OR (q.reason_code='ai.recovery_manual_confirmed_no_effect' AND EXISTS(SELECT 1 FROM ai_turn_recovery_reviews r WHERE r.turn_id=q.turn_id AND r.generation=q.generation AND r.status='resume_queued'))"
                ") "
                "AND l.status='recovery_required' LIMIT 1"
            ).fetchone() is not None
        finally:
            connection.close()

    def complete_recovery_queue(self, item: RecoveryQueueItem, *, observed_at: datetime, result_status: str = "completed") -> bool:
        validate_recovery_queue_item(item)
        validate_run_lease_time(observed_at, name="recovery completion time")
        _manual_review_terminal_status(result_status)
        return self._finish_recovery_queue(item, status="completed", reason_code=item.reason_code, observed_at=observed_at, quarantine=False, result_status=result_status)

    def quarantine_recovery_queue(self, item: RecoveryQueueItem, *, reason_code: str, observed_at: datetime) -> bool:
        validate_recovery_queue_item(item)
        if reason_code != "ai.recovery_execution_unknown":
            raise ValueError("recovery quarantine reason is invalid")
        validate_run_lease_time(observed_at, name="recovery quarantine time")
        return self._finish_recovery_queue(item, status="quarantined", reason_code=reason_code, observed_at=observed_at, quarantine=True, result_status=None)

    def _finish_recovery_queue(self, item: RecoveryQueueItem, *, status: str, reason_code: str, observed_at: datetime, quarantine: bool, result_status: str | None) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            queue_row = connection.execute(
                "SELECT reason_code FROM ai_turn_recovery_queue WHERE turn_id=? AND generation=? AND attempts=? AND status='running'",
                (item.turn_id, item.prior_generation, item.attempt),
            ).fetchone()
            changed = connection.execute(
                "UPDATE ai_turn_recovery_queue SET status=? WHERE turn_id=? AND generation=? AND attempts=? AND status='running' "
                "AND EXISTS(SELECT 1 FROM ai_turn_run_leases WHERE turn_id=? AND owner_id=? AND generation=? AND status='active')",
                ("failed" if quarantine else "completed", item.turn_id, item.prior_generation, item.attempt,
                 item.run_lease.turn_id, item.run_lease.owner_id, item.run_lease.generation),
            )
            if changed.rowcount:
                queue_reason = str(queue_row[0]) if queue_row is not None else ""
                if quarantine:
                    if connection.execute("UPDATE ai_turn_run_leases SET status='quarantined' WHERE turn_id=? AND owner_id=? AND generation=? AND status='active'", (item.run_lease.turn_id, item.run_lease.owner_id, item.run_lease.generation)).rowcount != 1:
                        raise RuntimeError("recovery quarantine lease identity lost")
                    if queue_reason == "ai.recovery_manual_confirmed_no_effect":
                        self._finish_manual_recovery_review(
                            connection, item.turn_id, item.prior_generation, "resume_failed",
                            "recovery_execution_unknown", observed_at,
                        )
                    latest = connection.execute(
                        "SELECT sequence,event_id,event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence DESC LIMIT 1",
                        (item.turn_id,),
                    ).fetchone()
                    last_sequence, last_event_id, last_event_type = _latest_event_identity(latest)
                    self._create_recovery_review(
                        connection, item.turn_id, item.run_lease.generation, reason_code,
                        last_sequence, last_event_id, last_event_type, observed_at,
                    )
                elif queue_reason == "ai.recovery_manual_confirmed_no_effect":
                    self._finish_manual_recovery_review(
                        connection, item.turn_id, item.prior_generation,
                        _manual_review_terminal_status(result_status or ""),
                        f"recovery_turn_{result_status}", observed_at,
                    )
                connection.execute("INSERT INTO ai_turn_recovery_queue_audit(turn_id,generation,attempt,status,reason_code,owner_id,new_generation,observed_at) VALUES(?,?,?,?,?,?,?,?)", (item.turn_id, item.prior_generation, item.attempt, status, reason_code, item.run_lease.owner_id, item.run_lease.generation, _lease_time(observed_at)))
            connection.execute("COMMIT")
            return changed.rowcount == 1
        except Exception:
            _rollback(connection); raise
        finally:
            connection.close()

    def list_recovery_reviews(self, *, project_id: str | None = None, limit: int = 128) -> tuple[PublicRecoveryReview, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 256:
            raise ValueError("recovery review list limit is invalid")
        if project_id is not None:
            validate_recovery_review_identity(project_id, name="recovery review project identity")
        connection = self._connect()
        try:
            if project_id is None:
                rows = connection.execute(
                    "SELECT review_id,project_id,status,revision,reason_code,created_at,updated_at FROM ai_turn_recovery_reviews ORDER BY updated_at DESC,review_id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT review_id,project_id,status,revision,reason_code,created_at,updated_at FROM ai_turn_recovery_reviews WHERE project_id=? ORDER BY updated_at DESC,review_id DESC LIMIT ?", (project_id, limit)
                ).fetchall()
            return tuple(_public_review_row(row) for row in rows)
        finally:
            connection.close()

    def get_recovery_review(self, review_id: str) -> PublicRecoveryReview | None:
        validate_recovery_review_identity(review_id)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT review_id,project_id,status,revision,reason_code,created_at,updated_at FROM ai_turn_recovery_reviews WHERE review_id=?", (review_id,)
            ).fetchone()
            return _public_review_row(row) if row is not None else None
        finally:
            connection.close()

    def get_recovery_review_request(self, review_id: str) -> Mapping[str, object] | None:
        validate_recovery_review_identity(review_id)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT t.request_json,r.project_id FROM ai_turn_recovery_reviews r JOIN ai_turns t ON t.turn_id=r.turn_id WHERE r.review_id=?", (review_id,)
            ).fetchone()
            if row is None:
                return None
            request = _mapping_json(row[0])
            return request if _request_project_id(request) == row[1] else None
        finally:
            connection.close()

    def keep_recovery_review(self, review_id: str, *, expected_revision: int, authorization: RecoveryReviewAuthorization, observed_at: datetime) -> PublicRecoveryReview | None:
        return self._decide_recovery_review(
            review_id, expected_revision=expected_revision, authorization=authorization,
            observed_at=observed_at, action="keep_quarantined",
        )

    def confirm_no_effect_and_queue_recovery_review(self, review_id: str, *, expected_revision: int, authorization: RecoveryReviewAuthorization, observed_at: datetime) -> PublicRecoveryReview | None:
        if not authorization.human_confirmed:
            raise ValueError("recovery no-effect confirmation is required")
        return self._decide_recovery_review(
            review_id, expected_revision=expected_revision, authorization=authorization,
            observed_at=observed_at, action="confirm_no_effect_and_queue",
        )

    def _decide_recovery_review(self, review_id: str, *, expected_revision: int, authorization: RecoveryReviewAuthorization, observed_at: datetime, action: str) -> PublicRecoveryReview | None:
        validate_recovery_review_identity(review_id)
        validate_recovery_review_authorization(authorization)
        validate_run_lease_time(observed_at, name="recovery review decision time")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
            raise ValueError("recovery review expected revision is invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT turn_id,generation,project_id,reason_code,last_sequence,last_event_id,last_event_type FROM ai_turn_recovery_reviews WHERE review_id=? AND revision=? AND status='quarantined'",
                (review_id, expected_revision),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT"); return None
            turn_id, generation, project_id = str(row[0]), int(row[1]), row[2]
            request_row = connection.execute("SELECT request_json FROM ai_turns WHERE turn_id=?", (turn_id,)).fetchone()
            latest = connection.execute("SELECT sequence,event_id,event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence DESC LIMIT 1", (turn_id,)).fetchone()
            lease = connection.execute("SELECT 1 FROM ai_turn_run_leases WHERE turn_id=? AND generation=? AND status='quarantined'", (turn_id, generation)).fetchone()
            if request_row is None or lease is None or _request_project_id(_mapping_json(request_row[0])) != project_id or _latest_event_identity(latest) != (int(row[4]), str(row[5]), str(row[6])):
                connection.execute("COMMIT"); return None
            if action == "confirm_no_effect_and_queue":
                if connection.execute("UPDATE ai_turn_run_leases SET status='recovery_required' WHERE turn_id=? AND generation=? AND status='quarantined'", (turn_id, generation)).rowcount != 1:
                    connection.execute("COMMIT"); return None
                connection.execute(
                    "INSERT INTO ai_turn_recovery_queue(turn_id,generation,reason_code,status,attempts,created_at) VALUES(?,?,?,'pending',0,?)",
                    (turn_id, generation, "ai.recovery_manual_confirmed_no_effect", _lease_time(observed_at)),
                )
                next_status = "resume_queued"
            else:
                next_status = "kept_quarantined"
            changed = connection.execute(
                "UPDATE ai_turn_recovery_reviews SET status=?,revision=revision+1,updated_at=? WHERE review_id=? AND revision=? AND status='quarantined'",
                (next_status, _lease_time(observed_at), review_id, expected_revision),
            )
            if changed.rowcount != 1:
                raise RuntimeError("recovery review decision lost atomic identity")
            next_revision = expected_revision + 1
            connection.execute(
                "INSERT INTO ai_turn_recovery_review_audit(review_id,revision,action,actor_id,boundary_outcome,boundary_reason_codes,policy_revision,observed_at) VALUES(?,?,?,?,?,?,?,?)",
                (review_id, next_revision, action, authorization.actor_id, authorization.boundary_outcome, _encode(authorization.boundary_reason_codes), authorization.policy_revision, _lease_time(observed_at)),
            )
            created = connection.execute("SELECT created_at FROM ai_turn_recovery_reviews WHERE review_id=?", (review_id,)).fetchone()
            result = PublicRecoveryReview(review_id, project_id if isinstance(project_id, str) else None, next_status, next_revision, str(row[3]), _lease_datetime(created[0]), observed_at)
            connection.execute("COMMIT")
            return validate_public_recovery_review(result)
        except Exception:
            _rollback(connection); raise
        finally:
            connection.close()

    def _create_recovery_review(self, connection: sqlite3.Connection, turn_id: str, generation: int, reason_code: str, last_sequence: int, last_event_id: str, last_event_type: str, observed_at: datetime) -> None:
        request_row = connection.execute("SELECT request_json FROM ai_turns WHERE turn_id=?", (turn_id,)).fetchone()
        if request_row is None:
            raise TurnStateConflict("turn was not found")
        project_id = _request_project_id(_mapping_json(request_row[0]))
        connection.execute(
            "INSERT INTO ai_turn_recovery_reviews(review_id,turn_id,generation,project_id,reason_code,last_sequence,last_event_id,last_event_type,status,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?, 'quarantined',1,?,?)",
            (f"review-{uuid4().hex}", turn_id, generation, project_id, reason_code, last_sequence, last_event_id, last_event_type, _lease_time(observed_at), _lease_time(observed_at)),
        )

    def _finish_manual_recovery_review(self, connection: sqlite3.Connection, turn_id: str, generation: int, status: str, action: str, observed_at: datetime) -> None:
        row = connection.execute(
            "SELECT review_id,revision FROM ai_turn_recovery_reviews WHERE turn_id=? AND generation=? AND status='resume_queued'",
            (turn_id, generation),
        ).fetchone()
        if row is None:
            raise RuntimeError("manual recovery review identity lost")
        review_id, revision = str(row[0]), int(row[1])
        if connection.execute(
            "UPDATE ai_turn_recovery_reviews SET status=?,revision=revision+1,updated_at=? WHERE review_id=? AND revision=? AND status='resume_queued'",
            (status, _lease_time(observed_at), review_id, revision),
        ).rowcount != 1:
            raise RuntimeError("manual recovery review update lost identity")
        connection.execute(
            "INSERT INTO ai_turn_recovery_review_audit(review_id,revision,action,actor_id,boundary_outcome,boundary_reason_codes,policy_revision,observed_at) VALUES(?,?,?,?,?,?,?,?)",
            (review_id, revision + 1, action, "recovery-worker", "allow", _encode((action,)), 1, _lease_time(observed_at)),
        )

    def release_strict_run_lease(self, token: RunLeaseToken) -> None:
        validate_run_lease_token(token)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM ai_turn_run_leases WHERE turn_id=? AND owner_id=? AND generation=? AND status='active'", (token.turn_id, token.owner_id, token.generation))
            connection.execute("COMMIT")
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def append(self, event: Mapping[str, object], *, expected_sequence: int, run_lease: RunLeaseToken | None = None) -> Mapping[str, object]:
        turn_id = str(event.get("turn_id", ""))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if run_lease is not None:
                validate_run_lease_token(run_lease)
                if run_lease.turn_id != turn_id or not connection.execute(
                    "SELECT 1 FROM ai_turn_run_leases WHERE turn_id=? AND owner_id=? AND generation=? AND status='active'",
                    (run_lease.turn_id, run_lease.owner_id, run_lease.generation),
                ).fetchone():
                    raise RunLeaseRevoked()
            validated = self._append_event_in_transaction(
                connection, event, expected_sequence=expected_sequence,
            )
            connection.execute("COMMIT")
            return deepcopy(validated)
        except sqlite3.IntegrityError as error:
            _rollback(connection)
            raise TurnEventConflict("turn event identity conflict") from error
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def append_event_with_immutable_payload(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        immutable_kind: str,
        immutable_payload: object,
        run_lease: RunLeaseToken | None = None,
    ) -> ImmutablePayloadAppendReceipt:
        """Append an event and its immutable evidence in one SQLite commit."""
        turn_id = str(event.get("turn_id", ""))
        _immutable_payload_identity(turn_id, immutable_kind)
        validate_governed_payload(immutable_payload)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_run_lease(connection, turn_id, run_lease)
            immutable_ref = self._get_or_create_immutable_payload_in_transaction(
                connection, turn_id, immutable_kind, immutable_payload,
            )
            appended = self._append_event_in_transaction(
                connection,
                _event_with_evidence_ref(event, immutable_ref),
                expected_sequence=expected_sequence,
            )
            connection.execute("COMMIT")
            return ImmutablePayloadAppendReceipt(deepcopy(appended), immutable_ref)
        except sqlite3.IntegrityError as error:
            _rollback(connection)
            raise TurnEventConflict("turn event identity conflict") from error
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def append_intent_bundle(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        intent_kind: str,
        intent_payload: object,
        run_lease: RunLeaseToken | None = None,
    ) -> IntentBundleReceipt:
        """Persist a tool intent before exposing its event to recovery."""
        turn_id = str(event.get("turn_id", ""))
        _immutable_payload_identity(turn_id, intent_kind)
        validate_governed_payload(intent_payload)
        intent_ref = _payload_ref(turn_id, intent_kind)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_run_lease(connection, turn_id, run_lease)
            self._put_payload_in_transaction(
                connection, intent_ref, turn_id, intent_kind, intent_payload,
            )
            appended = self._append_event_in_transaction(
                connection,
                _event_with_payload_ref(event, intent_ref),
                expected_sequence=expected_sequence,
            )
            connection.execute("COMMIT")
            return IntentBundleReceipt(deepcopy(appended), intent_ref)
        except sqlite3.IntegrityError as error:
            _rollback(connection)
            raise TurnEventConflict("turn event identity conflict") from error
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def append_hook_receipt_bundle(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        receipt_kind: str,
        receipt_payload: object,
        run_lease: RunLeaseToken | None = None,
    ) -> HookReceiptBundleReceipt:
        """Persist a safe Hook receipt and its Event in one SQLite commit."""

        turn_id = str(event.get("turn_id", ""))
        _immutable_payload_identity(turn_id, receipt_kind)
        validate_governed_payload(receipt_payload)
        receipt_ref = _payload_ref(turn_id, receipt_kind)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_run_lease(connection, turn_id, run_lease)
            self._put_payload_in_transaction(
                connection, receipt_ref, turn_id, receipt_kind, receipt_payload,
            )
            appended = self._append_event_in_transaction(
                connection,
                _event_with_receipt_ref(event, receipt_ref),
                expected_sequence=expected_sequence,
            )
            connection.execute("COMMIT")
            return HookReceiptBundleReceipt(deepcopy(appended), receipt_ref)
        except sqlite3.IntegrityError as error:
            _rollback(connection)
            raise TurnEventConflict("turn event identity conflict") from error
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def append_approval_bundle(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        action_kind: str,
        action_payload: object,
        approval_kind: str,
        approval_payload: object,
        run_lease: RunLeaseToken | None = None,
    ) -> ApprovalBundleReceipt:
        """Resolve one approval without a crash window between its authorities."""
        turn_id = str(event.get("turn_id", ""))
        _immutable_payload_identity(turn_id, action_kind)
        _immutable_payload_identity(turn_id, approval_kind)
        validate_governed_payload(action_payload)
        validate_governed_payload(approval_payload)
        event_data = event.get("data")
        supplied_action_ref = event_data.get("payload_ref") if isinstance(event_data, Mapping) else None
        action_ref = (
            str(supplied_action_ref)
            if isinstance(supplied_action_ref, str)
            else _payload_ref(turn_id, action_kind)
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_run_lease(connection, turn_id, run_lease)
            approval_ref = self._get_or_create_immutable_payload_in_transaction(
                connection, turn_id, approval_kind, approval_payload,
            )
            self._put_payload_in_transaction(
                connection, action_ref, turn_id, action_kind, action_payload,
            )
            appended = self._append_event_in_transaction(
                connection,
                _event_with_evidence_ref(
                    _event_with_payload_ref(event, action_ref), approval_ref,
                ),
                expected_sequence=expected_sequence,
            )
            connection.execute("COMMIT")
            return ApprovalBundleReceipt(deepcopy(appended), action_ref, approval_ref)
        except sqlite3.IntegrityError as error:
            _rollback(connection)
            raise TurnEventConflict("turn event identity conflict") from error
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def append_waiting_mcp_continuation_bundle(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        immutable_kind: str,
        immutable_payload: object,
        pending: Mapping[str, object],
        run_lease: RunLeaseToken | None = None,
    ) -> ImmutablePayloadAppendReceipt:
        """Atomically persist opaque continuation, waiting event and pending ref.

        The pending projection deliberately contains references/identity only;
        requestState and arguments remain in immutable payloads.
        """
        turn_id = str(event.get("turn_id", ""))
        _immutable_payload_identity(turn_id, immutable_kind)
        validate_governed_payload(immutable_payload)
        validate_governed_payload(pending)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_run_lease(connection, turn_id, run_lease)
            payload_ref = self._get_or_create_immutable_payload_in_transaction(
                connection, turn_id, immutable_kind, immutable_payload,
            )
            stored_pending = dict(pending)
            stored_pending["_mcp_request_state_ref"] = payload_ref
            appended = self._append_event_in_transaction(
                connection, _event_with_payload_ref(event, payload_ref),
                expected_sequence=expected_sequence,
            )
            connection.execute(
                "INSERT INTO ai_turn_pending(turn_id, decision_json) VALUES(?,?) "
                "ON CONFLICT(turn_id) DO UPDATE SET decision_json=excluded.decision_json",
                (turn_id, _encode(stored_pending)),
            )
            connection.execute("COMMIT")
            return ImmutablePayloadAppendReceipt(deepcopy(appended), payload_ref)
        except sqlite3.IntegrityError as error:
            _rollback(connection)
            raise TurnEventConflict("turn event identity conflict") from error
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def append_expert_job_wait_bundle(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        immutable_kind: str,
        immutable_payload: object,
        job_ref: str,
        admission_job_revision: int,
        run_lease: RunLeaseToken | None = None,
    ) -> ImmutablePayloadAppendReceipt:
        """Freeze one expert Job wait and expose its waiting Event atomically.

        This deliberately records only the governed Job identity/revision.  It
        is not a second Job authority and cannot contain provider arguments,
        paths, or output data.
        """
        turn_id = str(event.get("turn_id", ""))
        _immutable_payload_identity(turn_id, immutable_kind)
        validate_governed_payload(immutable_payload)
        if not _expert_job_ref(job_ref) or not isinstance(admission_job_revision, int) or isinstance(admission_job_revision, bool) or admission_job_revision < 1:
            raise ValueError("expert job wait identity is invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_run_lease(connection, turn_id, run_lease)
            snapshot_ref = self._get_or_create_immutable_payload_in_transaction(
                connection, turn_id, immutable_kind, immutable_payload,
            )
            appended = self._append_event_in_transaction(
                connection, _event_with_payload_ref(event, snapshot_ref),
                expected_sequence=expected_sequence,
            )
            connection.execute(
                "INSERT INTO ai_expert_job_waits("
                "turn_id,job_ref,admission_job_revision,snapshot_ref,status) VALUES(?,?,?,?, 'waiting')",
                (turn_id, job_ref, admission_job_revision, snapshot_ref),
            )
            connection.execute("COMMIT")
            return ImmutablePayloadAppendReceipt(deepcopy(appended), snapshot_ref)
        except sqlite3.IntegrityError as error:
            _rollback(connection)
            raise TurnEventConflict("expert job wait identity conflict") from error
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def get_expert_job_wait(self, turn_id: str) -> Mapping[str, object] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT turn_id,job_ref,admission_job_revision,snapshot_ref,status,terminal_job_revision "
                "FROM ai_expert_job_waits WHERE turn_id=?", (turn_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "turn_id": str(row[0]), "job_ref": str(row[1]),
                "admission_job_revision": int(row[2]), "snapshot_ref": str(row[3]),
                "status": str(row[4]),
                "terminal_job_revision": int(row[5]) if row[5] is not None else None,
            }
        finally:
            connection.close()

    def append_expert_job_terminal_bundle(
        self, event: Mapping[str, object], *, expected_sequence: int,
        immutable_kind: str, immutable_payload: object, job_ref: str,
        admission_job_revision: int, terminal_job_revision: int,
        run_lease: RunLeaseToken | None = None,
    ) -> ImmutablePayloadAppendReceipt:
        """Freeze a verified terminal projection before an expert wake runs."""
        turn_id = str(event.get("turn_id", ""))
        _immutable_payload_identity(turn_id, immutable_kind)
        validate_governed_payload(immutable_payload)
        if not _expert_job_ref(job_ref) or not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 1
            for value in (admission_job_revision, terminal_job_revision)
        ) or terminal_job_revision < admission_job_revision:
            raise ValueError("expert job terminal identity is invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_run_lease(connection, turn_id, run_lease)
            snapshot_ref = self._get_or_create_immutable_payload_in_transaction(
                connection, turn_id, immutable_kind, immutable_payload,
            )
            changed = connection.execute(
                "UPDATE ai_expert_job_waits SET status='wake_enqueued',terminal_job_revision=? "
                "WHERE turn_id=? AND job_ref=? AND admission_job_revision=? AND status='waiting'",
                (terminal_job_revision, turn_id, job_ref, admission_job_revision),
            )
            if changed.rowcount != 1:
                raise TurnEventConflict("expert job terminal wait conflict")
            appended = self._append_event_in_transaction(
                connection, _event_with_payload_ref(event, snapshot_ref),
                expected_sequence=expected_sequence,
            )
            connection.execute("COMMIT")
            return ImmutablePayloadAppendReceipt(deepcopy(appended), snapshot_ref)
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def list_expert_job_waits(self, *, limit: int = 64) -> Sequence[Mapping[str, object]]:
        """Return a bounded read-only projection for the Media terminal observer."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 256:
            raise ValueError("expert Job wait listing limit is invalid")
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT turn_id,job_ref,admission_job_revision,snapshot_ref,status,terminal_job_revision "
                "FROM ai_expert_job_waits WHERE status IN ('waiting','wake_enqueued') "
                "ORDER BY turn_id LIMIT ?", (limit,),
            ).fetchall()
            return tuple({
                "turn_id": str(row[0]), "job_ref": str(row[1]),
                "admission_job_revision": int(row[2]), "snapshot_ref": str(row[3]),
                "status": str(row[4]),
                "terminal_job_revision": int(row[5]) if row[5] is not None else None,
            } for row in rows)
        finally:
            connection.close()

    def list_expert_job_waits_after(
        self, *, after_turn_id: str | None, limit: int = 64,
    ) -> Sequence[Mapping[str, object]]:
        """Read one stable keyset page of unresolved expert waits."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 256:
            raise ValueError("expert Job wait listing limit is invalid")
        if after_turn_id is not None and (not isinstance(after_turn_id, str) or not after_turn_id):
            raise ValueError("expert Job wait cursor is invalid")
        connection = self._connect()
        try:
            if after_turn_id is None:
                rows = connection.execute(
                    "SELECT turn_id,job_ref,admission_job_revision,snapshot_ref,status,terminal_job_revision "
                    "FROM ai_expert_job_waits WHERE status IN ('waiting','wake_enqueued') "
                    "ORDER BY turn_id LIMIT ?", (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT turn_id,job_ref,admission_job_revision,snapshot_ref,status,terminal_job_revision "
                    "FROM ai_expert_job_waits WHERE status IN ('waiting','wake_enqueued') AND turn_id>? "
                    "ORDER BY turn_id LIMIT ?", (after_turn_id, limit),
                ).fetchall()
            return tuple({
                "turn_id": str(row[0]), "job_ref": str(row[1]),
                "admission_job_revision": int(row[2]), "snapshot_ref": str(row[3]),
                "status": str(row[4]),
                "terminal_job_revision": int(row[5]) if row[5] is not None else None,
            } for row in rows)
        finally:
            connection.close()

    def finalize_expert_job_terminal_bundle(
        self, event: Mapping[str, object], *, expected_sequence: int,
        job_ref: str, admission_job_revision: int, terminal_job_revision: int,
        run_lease: RunLeaseToken,
    ) -> Mapping[str, object]:
        """Append one Turn terminal event and observe its wake atomically."""
        turn_id = str(event.get("turn_id", ""))
        if not _expert_job_ref(job_ref) or not isinstance(terminal_job_revision, int):
            raise ValueError("expert job terminal identity is invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_run_lease(connection, turn_id, run_lease)
            changed = connection.execute(
                "UPDATE ai_expert_job_waits SET status='terminal_observed' "
                "WHERE turn_id=? AND job_ref=? AND admission_job_revision=? "
                "AND terminal_job_revision=? AND status='wake_enqueued'",
                (turn_id, job_ref, admission_job_revision, terminal_job_revision),
            )
            if changed.rowcount != 1:
                raise TurnEventConflict("expert job terminal observation conflict")
            appended = self._append_event_in_transaction(
                connection, event, expected_sequence=expected_sequence,
            )
            connection.execute("COMMIT")
            return deepcopy(appended)
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def transition_expert_job_wait(
        self,
        turn_id: str,
        job_ref: str,
        admission_job_revision: int,
        *,
        expected_status: str,
        next_status: str,
        terminal_job_revision: int | None = None,
        run_lease: RunLeaseToken | None = None,
    ) -> bool:
        """Compare-and-set the single wait state without executing a Job."""
        if (
            not _expert_job_ref(job_ref)
            or not isinstance(admission_job_revision, int)
            or isinstance(admission_job_revision, bool)
            or admission_job_revision < 1
            or (expected_status, next_status) not in {
                ("waiting", "wake_enqueued"),
                ("wake_enqueued", "terminal_observed"),
            }
            or not isinstance(terminal_job_revision, int)
            or isinstance(terminal_job_revision, bool)
            or terminal_job_revision < admission_job_revision
        ):
            raise ValueError("expert job wait transition is invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_run_lease(connection, turn_id, run_lease)
            changed = connection.execute(
                "UPDATE ai_expert_job_waits SET status=?,terminal_job_revision=? "
                "WHERE turn_id=? AND job_ref=? AND admission_job_revision=? AND status=?",
                (next_status, terminal_job_revision, turn_id, job_ref, admission_job_revision, expected_status),
            )
            connection.execute("COMMIT")
            return changed.rowcount == 1
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def append_tool_outcome_bundle(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        outcome_kind: str,
        outcome_payload: object,
        result_kind: str | None = None,
        result_payload: object | None = None,
        operation_receipt_kind: str | None = None,
        operation_receipt_payload: object | None = None,
        run_lease: RunLeaseToken | None = None,
    ) -> ToolOutcomeBundleReceipt:
        """Commit result, outcome, Event and an optional receipt together.

        The receipt reference is allocated inside the same transaction and is
        written into both the outcome evidence and event.  A failed sequence or
        lease assertion therefore cannot leave a receipt or outcome orphaned.
        """
        turn_id = str(event.get("turn_id", ""))
        _immutable_payload_identity(turn_id, outcome_kind)
        validate_governed_payload(outcome_payload)
        if (result_kind is None) != (result_payload is None):
            raise ValueError("result kind and payload must be provided together")
        if result_kind is not None:
            _immutable_payload_identity(turn_id, result_kind)
            validate_governed_payload(result_payload)
        if (operation_receipt_kind is None) != (operation_receipt_payload is None):
            raise ValueError("operation receipt kind and payload must be provided together")
        if operation_receipt_kind is not None:
            _immutable_payload_identity(turn_id, operation_receipt_kind)
            validate_governed_payload(operation_receipt_payload)
        outcome_ref = _payload_ref(turn_id, outcome_kind)
        result_ref = _payload_ref(turn_id, result_kind) if result_kind is not None else None
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_run_lease(connection, turn_id, run_lease)
            receipt_ref: str | None = None
            stored_outcome = outcome_payload
            stored_event = _event_with_payload_ref(event, outcome_ref)
            if result_ref is not None:
                if not isinstance(outcome_payload, Mapping):
                    raise ValueError("tool outcome payload is invalid")
                supplied_result_ref = outcome_payload.get("payload_ref")
                if supplied_result_ref not in {None, result_ref}:
                    raise ValueError("tool outcome already has a result payload ref")
                stored_outcome = dict(outcome_payload)
                stored_outcome["payload_ref"] = result_ref
                self._put_payload_in_transaction(
                    connection, result_ref, turn_id, result_kind, result_payload,
                )
            if operation_receipt_kind is not None:
                receipt_ref = _payload_ref(turn_id, operation_receipt_kind)
                if not isinstance(stored_outcome, Mapping):
                    raise ValueError("tool outcome payload is invalid")
                supplied_ref = stored_outcome.get("receipt_ref")
                if supplied_ref not in {None, receipt_ref}:
                    raise ValueError("tool outcome already has a receipt ref")
                stored_outcome = dict(stored_outcome)
                stored_outcome["receipt_ref"] = receipt_ref
                stored_event = _event_with_receipt_ref(stored_event, receipt_ref)
                self._put_payload_in_transaction(
                    connection, receipt_ref, turn_id, operation_receipt_kind, operation_receipt_payload,
                )
            self._put_payload_in_transaction(
                connection, outcome_ref, turn_id, outcome_kind, stored_outcome,
            )
            appended = self._append_event_in_transaction(
                connection, stored_event, expected_sequence=expected_sequence,
            )
            connection.execute("COMMIT")
            return ToolOutcomeBundleReceipt(
                deepcopy(appended), outcome_ref, receipt_ref, result_ref,
            )
        except sqlite3.IntegrityError as error:
            _rollback(connection)
            raise TurnEventConflict("turn event identity conflict") from error
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def append_model_terminal_bundle(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        model_receipt_payload: object | None = None,
        dispatch_authority_receipt_payload: object | None = None,
        prompt_cache_receipt_payload: object | None = None,
        run_lease: RunLeaseToken | None = None,
    ) -> ModelTerminalBundleReceipt:
        """Atomically commit model terminal metadata and its referencing Event."""
        turn_id = str(event.get("turn_id", ""))
        for payload in (
            model_receipt_payload,
            dispatch_authority_receipt_payload,
            prompt_cache_receipt_payload,
        ):
            if payload is not None:
                validate_governed_payload(payload)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_run_lease(connection, turn_id, run_lease)
            model_ref = (
                _payload_ref(turn_id, "model-call-receipt")
                if model_receipt_payload is not None else None
            )
            dispatch_ref = (
                _payload_ref(turn_id, "model-dispatch-authority-receipt")
                if dispatch_authority_receipt_payload is not None else None
            )
            cache_ref = (
                _payload_ref(turn_id, "prompt-cache-receipt")
                if prompt_cache_receipt_payload is not None else None
            )
            stored_event = _model_terminal_event_with_receipts(
                event, model_ref, (dispatch_ref, cache_ref),
            )
            for ref, kind, payload in (
                (model_ref, "model-call-receipt", model_receipt_payload),
                (dispatch_ref, "model-dispatch-authority-receipt", dispatch_authority_receipt_payload),
                (cache_ref, "prompt-cache-receipt", prompt_cache_receipt_payload),
            ):
                if ref is not None:
                    self._put_payload_in_transaction(connection, ref, turn_id, kind, payload)
            appended = self._append_event_in_transaction(
                connection, stored_event, expected_sequence=expected_sequence,
            )
            connection.execute("COMMIT")
            return ModelTerminalBundleReceipt(
                deepcopy(appended), model_ref, dispatch_ref, cache_ref,
            )
        except sqlite3.IntegrityError as error:
            _rollback(connection)
            raise TurnEventConflict("turn event identity conflict") from error
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def commit_model_attempt_dispatch_bundle(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        dispatch_payload: object,
        run_lease: RunLeaseToken | None = None,
    ) -> ModelAttemptDispatchBundleReceipt:
        """Commit the pre-wire marker, Event, and immutable dispatch identity."""
        dispatch = validate_model_wire_attempt_dispatch(dispatch_payload)
        turn_id = str(event.get("turn_id", ""))
        self._assert_model_attempt_event_identity(
            event, dispatch, expected_type="model.attempt.dispatched",
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_model_attempt_run_lease(connection, turn_id, run_lease)
            payload_ref = _payload_ref(turn_id, "model-wire-attempt-dispatch")
            stored_event = _event_with_payload_ref(event, payload_ref)
            self._put_payload_in_transaction(
                connection, payload_ref, turn_id, "model-wire-attempt-dispatch", dispatch,
            )
            connection.execute(
                "INSERT INTO ai_model_attempt_reservations("
                "turn_id,model_request_id,attempt_number,attempt_id,status,dispatch_payload_ref,"
                "terminal_receipt_ref,lease_owner_id,lease_generation,dispatched_at,terminal_status"
                ") VALUES(?,?,?,?, 'committed', ?, NULL, ?, ?, ?, NULL)",
                (
                    turn_id, dispatch["model_request_id"], dispatch["attempt_number"],
                    dispatch["attempt_id"], payload_ref,
                    run_lease.owner_id if run_lease is not None else None,
                    run_lease.generation if run_lease is not None else None,
                    dispatch["dispatched_at"],
                ),
            )
            effect_intent = _model_wire_attempt_effect_intent(
                connection, dispatch=dispatch, dispatch_ref=payload_ref,
            )
            effect, created = self._effect_log.plan_in_connection(
                connection, effect_intent, now=_effect_time(str(dispatch["dispatched_at"])),
            )
            if not created:
                raise TurnEventConflict("model attempt effect identity conflict")
            appended = self._append_event_in_transaction(
                connection, stored_event, expected_sequence=expected_sequence,
            )
            connection.execute("COMMIT")
            return ModelAttemptDispatchBundleReceipt(
                deepcopy(appended), payload_ref, str(dispatch["attempt_id"]),
                str(dispatch["model_request_id"]), int(dispatch["attempt_number"]),
            )
        except sqlite3.IntegrityError as error:
            _rollback(connection)
            raise TurnEventConflict("model attempt reservation identity conflict") from error
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def execute_model_attempt_handler(
        self,
        attempt_id: str,
        handler,
        *,
        run_lease: RunLeaseToken | None = None,
    ) -> object:
        """Claim one planned Model Effect and run its provider wire Handler."""

        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("model attempt id is invalid")
        import threading
        import time
        execution_context = self.__dict__.setdefault("_model_execution_context", threading.local())
        result_box: dict[str, object] = {}

        def execute(_effect):
            previous = getattr(execution_context, "current", None)
            execution_context.current = (_effect, observed_at.timestamp(), started_at)
            try:
                handled = handler()
            finally:
                execution_context.current = previous
            if (
                not isinstance(handled, tuple)
                or len(handled) != 2
                or not isinstance(handled[1], str)
                or not handled[1]
            ):
                raise ValueError("model attempt Handler result is invalid")
            result_box["value"] = handled[0]
            return handled[1]

        observed_at = datetime.now(timezone.utc)
        now = _effect_time(observed_at.isoformat())
        started_at = time.monotonic()
        connection = self._connect()
        try:
            lease_expires_at = _model_effect_lease_expiry(
                connection, run_lease, fallback=now + self._effect_runner.lease_seconds,
            )
        finally:
            connection.close()
        outcome = self._effect_runner.execute_planned(
            attempt_id,
            execute,
            now=now,
            receipt_kind="model-wire-attempt-receipt",
            lease_expires_at=lease_expires_at,
        )
        if outcome.state is not EffectState.SETTLED_OK or "value" not in result_box:
            raise RunLeaseRevoked()
        return result_box["value"]

    def resolve_model_provider_resume_source(
        self, turn_id: str, *, attempt_id: str, dispatch_ref: str, terminal_ref: str,
    ) -> dict[str, object]:
        """只读闭合原尝试与完整游标链，不恢复租约或授予继续权限。"""
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            source, _, _ = self._model_provider_resume_source(
                connection, turn_id, attempt_id, dispatch_ref, terminal_ref,
            )
            return deepcopy(source)
        except (ValueError, KeyError, TypeError) as error:
            raise TurnEventConflict("model provider resume source is invalid") from error
        finally:
            connection.close()

    def commit_model_provider_resume_binding(
        self, *, dispatch_payload: Mapping[str, object], dispatch_payload_ref: str,
        source: Mapping[str, object], run_lease: RunLeaseToken,
    ) -> str:
        """新原 Handler 内复验来源并追加无正文血缘；旧 UNKNOWN 不变。"""
        dispatch = validate_model_wire_attempt_dispatch(dispatch_payload)
        context = getattr(getattr(self, "_model_execution_context", None), "current", None)
        if run_lease is None or context is None:
            raise RunLeaseRevoked()
        if not isinstance(source, Mapping) or set(source) != {
            "attempt_id", "dispatch_ref", "terminal_ref", "checkpoint_ref", "cursor",
        }:
            raise TurnEventConflict("model provider resume source is invalid")
        import time
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = max(datetime.now(timezone.utc).timestamp(), context[1] + time.monotonic() - context[2])
            run_binding, effect_binding = self._assert_model_provider_owner(
                connection, dispatch, dispatch_payload_ref, run_lease, context, now,
            )
            resolved, previous_dispatch, previous_route = self._model_provider_resume_source(
                connection, str(dispatch["turn_id"]), source.get("attempt_id"),
                source.get("dispatch_ref"), source.get("terminal_ref"),
            )
            if (dict(source) != resolved or dispatch["attempt_id"] == previous_dispatch["attempt_id"]
                    or dispatch["model_request_id"] == previous_dispatch["model_request_id"]
                    or any(dispatch.get(key) != previous_dispatch.get(key) for key in
                        ("turn_id", "routing_snapshot_revision", "provider_id", "model_id", "execution_location"))
                    or self._model_provider_route(connection, dispatch) != previous_route):
                raise TurnEventConflict("model provider resume binding conflict")
            payload = {"schema_version": "1.0.0", "source": resolved, "dispatch": dispatch,
                "dispatch_ref": dispatch_payload_ref, "run_lease": run_binding, "effect_lease": effect_binding}
            validate_governed_payload(payload)
            ref = self._get_or_create_immutable_payload_in_transaction(connection, str(dispatch["turn_id"]),
                "model-provider-resume-" + str(dispatch["attempt_id"]), payload)
            connection.execute("COMMIT")
            return ref
        except RunLeaseRevoked:
            _rollback(connection)
            raise
        except (ValueError, KeyError, TypeError) as error:
            _rollback(connection)
            raise TurnEventConflict("model provider resume source is invalid") from error
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    @staticmethod
    def _model_provider_route(connection, dispatch):
        turn_id = dispatch["turn_id"]
        rows = connection.execute("SELECT event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence", (turn_id,))
        routes = [event for row in rows if (event := _mapping_json(row[0]))["type"] == "model.routed"
            and event["correlation"].get("model_request_id") == dispatch["model_request_id"]]
        if len(routes) != 1:
            raise TurnEventConflict("model provider resume route conflict")
        ref = routes[0]["data"]["payload_ref"]
        frozen = connection.execute("SELECT turn_id,payload_json FROM ai_turn_immutable_payloads WHERE payload_ref=?", (ref,)).fetchone()
        if frozen is None or frozen[0] != turn_id:
            raise TurnEventConflict("model provider resume route conflict")
        return ref, routes[0]["data"].get("model_call_purpose", "primary"), _mapping_json(frozen[1])

    @classmethod
    def _model_provider_resume_source(cls, connection, turn_id, attempt_id, dispatch_ref, terminal_ref):
        if any(type(value) is not str or not value for value in (turn_id, attempt_id, dispatch_ref, terminal_ref)):
            raise TurnEventConflict("model provider resume source is invalid")
        request_row = connection.execute("SELECT request_json FROM ai_turns WHERE turn_id=?", (turn_id,)).fetchone()
        if request_row is None or validate_turn_request(_mapping_json(request_row[0]))["turn_id"] != turn_id:
            raise TurnEventConflict("model provider resume Turn conflict")
        events = [_mapping_json(row[0]) for row in connection.execute(
            "SELECT event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence", (turn_id,))]
        if any(event["type"] in {"turn.cancelled", "turn.completed", "turn.failed"} for event in events):
            raise TurnEventConflict("model provider resume Turn is terminal")
        row = connection.execute(
            "SELECT turn_id,model_request_id,attempt_number,dispatch_payload_ref,status,terminal_receipt_ref,"
            "lease_owner_id,lease_generation,terminal_status FROM ai_model_attempt_reservations WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if (row is None or row[0] != turn_id or row[3] != dispatch_ref or row[4] != "terminal"
                or row[5] != terminal_ref or row[8] != "failed_transport"
                or type(row[6]) is not str or not row[6] or type(row[7]) is not int or row[7] < 1):
            raise TurnEventConflict("model provider resume reservation conflict")
        values = []
        for ref, kind in ((dispatch_ref, "model-wire-attempt-dispatch"), (terminal_ref, "model-wire-attempt-receipt")):
            value = connection.execute("SELECT turn_id,kind,payload_json FROM ai_turn_payloads WHERE payload_ref=?", (ref,)).fetchone()
            if value is None or value[0] != turn_id or value[1] != kind:
                raise TurnEventConflict("model provider resume reference conflict")
            values.append(_mapping_json(value[2]))
        dispatch = validate_model_wire_attempt_dispatch(values[0])
        receipt = validate_model_wire_attempt_receipt(values[1])
        if (dispatch["attempt_id"] != attempt_id or dispatch["turn_id"] != turn_id
                or dispatch["model_request_id"] != row[1] or dispatch["attempt_number"] != row[2]
                or not _model_attempt_identity_matches(dispatch, receipt) or receipt["status"] != "failed_transport"
                or receipt["error_code"] == "ai.consumer_cancelled"):
            raise TurnEventConflict("model provider resume receipt conflict")
        for event_type, ref, field in (("model.attempt.dispatched", dispatch_ref, "payload_ref"),
                ("model.attempt.terminal", terminal_ref, "receipt_ref")):
            matching = [event for event in events if event["type"] == event_type and event["data"].get(field) == ref]
            if (len(matching) != 1 or matching[0]["correlation"].get("model_request_id") != row[1]
                    or matching[0]["turn_id"] != turn_id
                    or event_type == "model.attempt.terminal" and dispatch_ref not in matching[0]["data"]["evidence_refs"]):
                raise TurnEventConflict("model provider resume event conflict")
        effect = connection.execute("SELECT state,attempt,turn_id,kind,error_ref FROM effect WHERE operation_id=?", (attempt_id,)).fetchone()
        if (effect is None or effect[0] != EffectState.UNKNOWN.value or effect[2] != turn_id
                or effect[3] != "model_call" or effect[4] != receipt["error_code"]):
            raise TurnEventConflict("model provider resume Effect conflict")
        prefix = "model-provider-checkpoint-" + attempt_id + "-"
        chain = connection.execute("SELECT payload_ref,kind,payload_json FROM ai_turn_immutable_payloads "
            "WHERE turn_id=? AND kind>=? AND kind<? ORDER BY kind", (turn_id, prefix, prefix + "\uffff")).fetchall()
        if not chain:
            raise TurnEventConflict("model provider resume checkpoint missing")
        prior_ref, prior_cursor, effect_binding = None, None, None
        run_binding = {"owner_id": row[6], "generation": row[7]}
        for ref, kind, encoded in chain:
            payload = _mapping_json(encoded)
            cursor = _model_provider_cursor(payload.get("cursor"))
            lease = payload.get("effect_lease")
            if (set(payload) != {"schema_version", "dispatch", "dispatch_ref", "cursor", "previous_ref", "run_lease", "effect_lease"}
                    or payload["schema_version"] != "1.0.0" or payload["dispatch"] != dispatch
                    or payload["dispatch_ref"] != dispatch_ref or payload["previous_ref"] != prior_ref
                    or payload["run_lease"] != run_binding
                    or type(payload["run_lease"].get("generation")) is not int
                    or not isinstance(lease, dict) or set(lease) != {"owner_id", "attempt"}
                    or type(lease["owner_id"]) is not str or not lease["owner_id"]
                    or type(lease["attempt"]) is not int or lease["attempt"] != effect[1]
                    or lease["attempt"] < 1 or effect_binding is not None and lease != effect_binding
                    or kind != prefix + f"{cursor['sequence_number']:016d}"
                    or not re.fullmatch(re.escape(_immutable_payload_ref(turn_id, kind)) + r"/[0-9a-f]{32}", ref)
                    or prior_cursor is not None and (cursor["response_id"] != prior_cursor["response_id"]
                        or cursor["sequence_number"] <= prior_cursor["sequence_number"])):
                raise TurnEventConflict("model provider resume checkpoint chain conflict")
            prior_ref, prior_cursor, effect_binding = ref, cursor, lease
        source = {"attempt_id": attempt_id, "dispatch_ref": dispatch_ref, "terminal_ref": terminal_ref,
            "checkpoint_ref": prior_ref, "cursor": prior_cursor}
        return source, dispatch, cls._model_provider_route(connection, dispatch)

    def _assert_model_provider_owner(self, connection, dispatch, dispatch_ref, run_lease, context, now):
        """复用原 checkpoint 的实际 run、reservation 和 Effect 租约约束。"""
        turn_id, attempt_id = str(dispatch["turn_id"]), str(dispatch["attempt_id"])
        self._assert_model_attempt_run_lease(connection, turn_id, run_lease, now=now)
        reservation = connection.execute("SELECT turn_id,model_request_id,attempt_number,dispatch_payload_ref,"
            "status,terminal_receipt_ref,lease_owner_id,lease_generation FROM ai_model_attempt_reservations WHERE attempt_id=?",
            (attempt_id,)).fetchone()
        if (reservation is None or reservation[0] != turn_id or reservation[1] != dispatch["model_request_id"]
                or reservation[2] != dispatch["attempt_number"] or reservation[3] != dispatch_ref
                or reservation[4] != "committed" or reservation[5] is not None
                or reservation[6] != run_lease.owner_id or reservation[7] != run_lease.generation):
            raise TurnEventConflict("model provider checkpoint reservation conflict")
        stored = connection.execute("SELECT payload_json FROM ai_turn_payloads WHERE payload_ref=?", (dispatch_ref,)).fetchone()
        if stored is None or validate_model_wire_attempt_dispatch(_mapping_json(stored[0])) != dispatch:
            raise TurnEventConflict("model provider checkpoint dispatch conflict")
        effect = connection.execute("SELECT state,lease_owner,attempt,lease_expires_at FROM effect WHERE operation_id=?", (attempt_id,)).fetchone()
        if (effect is None or effect[0] != EffectState.INFLIGHT.value or effect[1] != self._effect_runner.owner_id
                or effect[3] is None or float(effect[3]) <= now or context[0].operation_id != attempt_id
                or context[0].lease_owner != effect[1] or context[0].attempt != effect[2]):
            raise RunLeaseRevoked()
        return ({"owner_id": run_lease.owner_id, "generation": run_lease.generation},
            {"owner_id": effect[1], "attempt": effect[2]})

    def commit_model_provider_checkpoint(
        self,
        *,
        dispatch_payload: Mapping[str, object],
        dispatch_payload_ref: str,
        cursor: Mapping[str, object],
        expected_previous_ref: str | None,
        run_lease: RunLeaseToken,
    ) -> str:
        """CAS body-free provider metadata under the actual run and Effect leases.

        This does not settle an Effect, append an Event, or authorize recovery.
        The caller must be inside the original claimed model Handler.
        """
        dispatch = validate_model_wire_attempt_dispatch(dispatch_payload)
        normalized_cursor = _model_provider_cursor(cursor)
        if run_lease is None:
            raise RunLeaseRevoked()
        context = getattr(getattr(self, "_model_execution_context", None), "current", None)
        if context is None:
            raise RunLeaseRevoked()
        import time
        turn_id = str(dispatch["turn_id"])
        attempt_id = str(dispatch["attempt_id"])
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            observed_now = max(
                datetime.now(timezone.utc).timestamp(),
                context[1] + (time.monotonic() - context[2]),
            )
            run_binding, effect_binding = self._assert_model_provider_owner(
                connection, dispatch, dispatch_payload_ref, run_lease, context, observed_now,
            )
            prefix = f"model-provider-checkpoint-{attempt_id}-"
            previous = connection.execute(
                "SELECT payload_ref,payload_json FROM ai_turn_immutable_payloads "
                "WHERE turn_id=? AND kind>=? AND kind<? ORDER BY kind DESC LIMIT 1",
                (turn_id, prefix, prefix + "\uffff"),
            ).fetchone()
            if (str(previous[0]) if previous is not None else None) != expected_previous_ref:
                raise TurnEventConflict("model provider checkpoint CAS conflict")
            if previous is not None:
                prior = _mapping_json(previous[1])
                try:
                    prior_cursor = _model_provider_cursor(prior.get("cursor"))
                except ValueError as error:
                    raise TurnEventConflict("model provider checkpoint predecessor is invalid") from error
                if (
                    set(prior) != {"schema_version", "dispatch", "dispatch_ref", "cursor", "previous_ref", "run_lease", "effect_lease"}
                    or prior.get("schema_version") != "1.0.0"
                    or prior.get("dispatch") != dispatch
                    or prior.get("dispatch_ref") != dispatch_payload_ref
                    or prior.get("run_lease") != run_binding
                    or prior.get("effect_lease") != effect_binding
                    or prior_cursor["response_id"] != normalized_cursor["response_id"]
                    or prior_cursor["sequence_number"] >= normalized_cursor["sequence_number"]
                ):
                    raise TurnEventConflict("model provider checkpoint cursor conflict")
            payload = {
                "schema_version": "1.0.0", "dispatch": dispatch,
                "dispatch_ref": dispatch_payload_ref, "cursor": normalized_cursor,
                "previous_ref": expected_previous_ref, "run_lease": run_binding,
                "effect_lease": effect_binding,
            }
            validate_governed_payload(payload)
            kind = prefix + f"{normalized_cursor['sequence_number']:016d}"
            ref = self._get_or_create_immutable_payload_in_transaction(connection, turn_id, kind, payload)
            connection.execute("COMMIT")
            return ref
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def append_model_attempt_terminal_bundle(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        attempt_receipt_payload: object,
        run_lease: RunLeaseToken | None = None,
    ) -> ModelAttemptTerminalBundleReceipt:
        """Bind one immutable Receipt and terminal Event to the current Effect."""
        receipt = validate_model_wire_attempt_receipt(attempt_receipt_payload)
        turn_id = str(event.get("turn_id", ""))
        self._assert_model_attempt_event_identity(
            event, receipt, expected_type="model.attempt.terminal",
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            import time
            context = getattr(getattr(self, "_model_execution_context", None), "current", None)
            terminal_now = datetime.now(timezone.utc).timestamp()
            if context is not None:
                terminal_now = max(terminal_now, context[1] + (time.monotonic() - context[2]))
            self._assert_model_attempt_run_lease(connection, turn_id, run_lease)
            reservation = connection.execute(
                "SELECT turn_id,model_request_id,attempt_number,attempt_id,dispatch_payload_ref,"
                "status,terminal_receipt_ref,lease_owner_id,lease_generation "
                "FROM ai_model_attempt_reservations "
                "WHERE attempt_id=?",
                (receipt["attempt_id"],),
            ).fetchone()
            if reservation is None:
                raise TurnEventConflict("model attempt reservation was not found")
            if (
                str(reservation[0]) != turn_id
                or str(reservation[1]) != receipt["model_request_id"]
                or int(reservation[2]) != receipt["attempt_number"]
                or str(reservation[3]) != receipt["attempt_id"]
            ):
                raise TurnEventConflict("model attempt reservation identity conflict")
            if (
                str(reservation[5]) != "committed"
                or reservation[6] is not None
                or reservation[7] != (run_lease.owner_id if run_lease is not None else None)
                or reservation[8] != (run_lease.generation if run_lease is not None else None)
            ):
                raise TurnEventConflict("model attempt reservation is not terminal-eligible")
            stored_dispatch = connection.execute(
                "SELECT payload_json FROM ai_turn_payloads WHERE payload_ref=?",
                (reservation[4],),
            ).fetchone()
            if stored_dispatch is None or not _model_attempt_identity_matches(
                validate_model_wire_attempt_dispatch(_mapping_json(stored_dispatch[0])), receipt,
            ):
                raise TurnEventConflict("model attempt reservation identity conflict")
            effect_before_settle = connection.execute(
                "SELECT state,lease_owner,attempt,lease_expires_at FROM effect WHERE operation_id=?",
                (receipt["attempt_id"],),
            ).fetchone()
            if effect_before_settle is None:
                raise TurnEventConflict("model attempt Effect was not found")
            self._assert_model_attempt_run_lease(connection, turn_id, run_lease, now=terminal_now)
            if (
                str(effect_before_settle[0]) != EffectState.INFLIGHT.value
                or str(effect_before_settle[1]) != self._effect_runner.owner_id
                or effect_before_settle[3] is None
                or float(effect_before_settle[3]) <= terminal_now
                or (context is not None and (
                    context[0].operation_id != receipt["attempt_id"]
                    or context[0].lease_owner != str(effect_before_settle[1])
                    or context[0].attempt != int(effect_before_settle[2])
                ))
            ):
                raise RunLeaseRevoked()
            receipt_ref = _payload_ref(turn_id, "model-wire-attempt-receipt")
            stored_event = _model_attempt_terminal_event_with_receipt(
                event, receipt_ref, str(reservation[4]),
            )
            self._put_payload_in_transaction(
                connection, receipt_ref, turn_id, "model-wire-attempt-receipt", receipt,
            )
            effect_target = (
                EffectState.SETTLED_OK
                if receipt["status"] == "succeeded" else EffectState.UNKNOWN
            )
            if effect_target is EffectState.UNKNOWN:
                current_effect = self._effect_log.get_in_connection(
                    connection, str(receipt["attempt_id"]),
                )
                self._effect_runner.mark_unknown(
                    current_effect, connection=connection,
                    now=terminal_now,
                    error_ref=str(receipt["error_code"]),
                )
            appended = self._append_event_in_transaction(
                connection, stored_event, expected_sequence=expected_sequence,
            )
            updated = connection.execute(
                "UPDATE ai_model_attempt_reservations "
                "SET status='terminal',terminal_receipt_ref=?,terminal_status=? "
                "WHERE attempt_id=? AND status='committed' AND terminal_receipt_ref IS NULL "
                "AND lease_owner_id IS ? AND lease_generation IS ?",
                (
                    receipt_ref, receipt["status"], receipt["attempt_id"],
                    run_lease.owner_id if run_lease is not None else None,
                    run_lease.generation if run_lease is not None else None,
                ),
            )
            if updated.rowcount != 1:
                raise TurnEventConflict("model attempt reservation terminal compare-and-set failed")
            connection.execute("COMMIT")
            return ModelAttemptTerminalBundleReceipt(
                deepcopy(appended), receipt_ref, str(receipt["attempt_id"]),
                str(receipt["model_request_id"]), int(receipt["attempt_number"]),
            )
        except sqlite3.IntegrityError as error:
            _rollback(connection)
            raise TurnEventConflict("model attempt terminal identity conflict") from error
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def events_after(self, turn_id: str, after_sequence: int = 0) -> Sequence[Mapping[str, object]]:
        connection = self._connect()
        try:
            rows = connection.execute("SELECT event_json FROM ai_turn_events WHERE turn_id=? AND sequence>? ORDER BY sequence", (turn_id, after_sequence)).fetchall()
            return tuple(_mapping_json(row[0]) for row in rows)
        finally:
            connection.close()

    def put(self, turn_id: str, kind: str, payload: object) -> str:
        validate_governed_payload(payload)
        ref = f"crp://session/{turn_id}/{kind}/{uuid4().hex}"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO ai_turn_payloads(payload_ref, turn_id, kind, payload_json) VALUES(?,?,?,?)", (ref, turn_id, kind, _encode(payload)))
            connection.execute("COMMIT")
            return ref
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def get(self, payload_ref: str) -> object:
        row = self._cached_immutable_read(("payload", payload_ref),
            "SELECT payload_json FROM ai_turn_payloads WHERE payload_ref=? UNION ALL "
            "SELECT payload_json FROM ai_turn_immutable_payloads WHERE payload_ref=? LIMIT 1",
            (payload_ref, payload_ref))
        if row is None:
            raise KeyError(payload_ref)
        return json.loads(str(row[0]))

    def immutable_payload_reference(self, turn_id: str, kind: str) -> str:
        """Return the stable reference an immutable payload will use.

        Admission must bind this reference into a Turn request before the Turn
        exists.  The immutable table has a foreign key to ``ai_turns``, so a
        row cannot be reserved at that point.  Existing rows retain their
        historical opaque references; a new row is written only after the
        Turn authority accepts the request.
        """
        _immutable_payload_identity(turn_id, kind)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT payload_ref FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?",
                (turn_id, kind),
            ).fetchone()
            return str(row[0]) if row is not None else _immutable_payload_ref(turn_id, kind)
        finally:
            connection.close()

    def get_or_create_immutable_payload(self, turn_id: str, kind: str, payload: object) -> str:
        _immutable_payload_identity(turn_id, kind)
        validate_governed_payload(payload)
        encoded = _encode(payload)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_ref, payload_json FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?",
                (turn_id, kind),
            ).fetchone()
            if row is not None:
                if str(row[1]) != encoded:
                    raise ValueError("immutable payload identity conflict")
                connection.execute("COMMIT")
                return str(row[0])
            ref = _immutable_payload_ref(turn_id, kind)
            connection.execute(
                "INSERT INTO ai_turn_immutable_payloads(payload_ref, turn_id, kind, payload_json) VALUES(?,?,?,?)",
                (ref, turn_id, kind, encoded),
            )
            connection.execute("COMMIT")
            return ref
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def verify_tool_call_effect(self, effect: object) -> tuple[EffectState, str | None]:
        """Project durable Tool outcome evidence for Core Reaper only."""

        turn_id = getattr(effect, "turn_id", None)
        operation_id = getattr(effect, "operation_id", None)
        effect_class = getattr(effect, "effect_class", None)
        if not isinstance(turn_id, str) or not isinstance(operation_id, str):
            return EffectState.UNKNOWN, "ai.tool_effect_identity_invalid"
        for event in reversed(tuple(self.events_after(turn_id))):
            if event.get("type") != "tool.outcome.recorded":
                continue
            correlation = event.get("correlation")
            if not isinstance(correlation, Mapping) or correlation.get("tool_call_id") != operation_id:
                continue
            data = event.get("data")
            payload_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
            if not isinstance(payload_ref, str):
                return EffectState.UNKNOWN, "ai.tool_outcome_ref_missing"
            outcome = self.get(payload_ref)
            if not isinstance(outcome, Mapping):
                return EffectState.UNKNOWN, "ai.tool_outcome_payload_invalid"
            status = outcome.get("status")
            if status == "completed":
                return EffectState.SETTLED_OK, payload_ref
            if status in {"failed", "cancelled"}:
                return EffectState.SETTLED_ERR, payload_ref
            return EffectState.UNKNOWN, payload_ref
        if effect_class in {EffectClass.PURE, EffectClass.IDEMPOTENT}:
            return EffectState.PLANNED, None
        return EffectState.UNKNOWN, "ai.tool_outcome_unverified"

    def verify_mcp_call_effect(self, effect: object) -> tuple[EffectState, str | None]:
        """Verify one MCP child Effect from its immutable terminal Receipt.

        The MCP transport may query a reviewed remote status endpoint while a
        live Turn owns the ordinary execution fences.  Core Reaper performs no
        network call here: a locally committed Receipt is conclusive, while a
        missing Receipt remains UNKNOWN and can never authorize a replay.
        """

        turn_id = getattr(effect, "turn_id", None)
        operation_id = getattr(effect, "operation_id", None)
        intent_ref = getattr(effect, "intent_ref", None)
        if (
            not isinstance(turn_id, str)
            or not isinstance(operation_id, str)
            or not operation_id.startswith("mcp-effect-")
            or not isinstance(intent_ref, str)
        ):
            return EffectState.UNKNOWN, "mcp.effect_identity_invalid"
        try:
            payload = self.get(intent_ref)
        except KeyError:
            return EffectState.UNKNOWN, "mcp.effect_intent_missing"
        if not isinstance(payload, Mapping):
            return EffectState.UNKNOWN, "mcp.effect_intent_invalid"
        invocation_id = payload.get("invocation_id")
        if (
            not isinstance(invocation_id, str)
            or not invocation_id.strip()
            or operation_id != _mcp_effect_operation_id(invocation_id)
        ):
            return EffectState.UNKNOWN, "mcp.effect_identity_invalid"
        existing = self.get_immutable_payload(
            turn_id, f"mcp-call-receipt-{invocation_id}",
        )
        if existing is None:
            return EffectState.UNKNOWN, "mcp.remote_status_required"
        receipt_ref, receipt = existing
        if (
            not isinstance(receipt, Mapping)
            or receipt.get("turn_id") != turn_id
            or receipt.get("invocation_id") != invocation_id
            or receipt.get("status") != "completed"
            or receipt.get("effect_certainty") != "confirmed_applied"
        ):
            return EffectState.UNKNOWN, "mcp.receipt_identity_invalid"
        for key in ("server_id", "tool_id", "tool_name", "protocol_version"):
            if receipt.get(key) != payload.get(key):
                return EffectState.UNKNOWN, "mcp.receipt_identity_invalid"
        return EffectState.SETTLED_OK, receipt_ref

    def project_event_cursor(self, project_id: str) -> int:
        """Return the latest durable metadata-feed cursor for one project."""
        project = _required_project_id(project_id)
        connection = self._connect()
        try:
            return _project_event_head_cursor(connection, project)
        finally:
            connection.close()

    def project_event_bounds(self, project_id: str) -> Mapping[str, int]:
        """Return the durable retention watermark and currently readable cursor range."""
        project = _required_project_id(project_id)
        connection = self._connect()
        try:
            retained_after = _project_event_retained_after_cursor(connection, project)
            head = _project_event_head_cursor(connection, project)
            row = connection.execute(
                "SELECT MIN(cursor) FROM ai_project_event_feed WHERE project_id=?", (project,),
            ).fetchone()
            earliest = int(row[0]) if row and row[0] is not None else head + 1
            if earliest != retained_after + 1:
                raise RuntimeError("project event retention boundary is inconsistent")
            return {
                "retained_after_cursor": retained_after,
                "earliest_available_cursor": earliest,
                "head_cursor": head,
            }
        finally:
            connection.close()

    def prune_project_events_through(self, project_id: str, through_cursor: int) -> Mapping[str, int]:
        """Prune only replay metadata and record a durable rebase watermark."""
        project = _required_project_id(project_id)
        if not isinstance(through_cursor, int) or isinstance(through_cursor, bool) or through_cursor < 0:
            raise ValueError("project event retention cursor is invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            retained_after = _project_event_retained_after_cursor(connection, project)
            head = _project_event_head_cursor(connection, project)
            if through_cursor > head:
                raise ValueError("project event retention cursor is ahead of authority")
            next_retained_after = max(retained_after, through_cursor)
            connection.execute(
                "DELETE FROM ai_project_event_feed WHERE project_id=? AND cursor<=?",
                (project, next_retained_after),
            )
            connection.execute(
                "INSERT INTO ai_project_event_feed_retention(project_id,retained_after_cursor) VALUES(?,?) "
                "ON CONFLICT(project_id) DO UPDATE SET retained_after_cursor=excluded.retained_after_cursor",
                (project, next_retained_after),
            )
            connection.execute("COMMIT")
            return {
                "retained_after_cursor": next_retained_after,
                "earliest_available_cursor": next_retained_after + 1,
                "head_cursor": head,
            }
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def project_events_after(
        self, project_id: str, after_cursor: int = 0, *, limit: int = 128,
        until_cursor: int | None = None,
    ) -> Sequence[Mapping[str, object]]:
        """Read bounded event metadata without exposing Turn event payloads."""
        project = _required_project_id(project_id)
        if not isinstance(after_cursor, int) or isinstance(after_cursor, bool) or after_cursor < 0:
            raise ValueError("project event cursor is invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 256:
            raise ValueError("project event limit is invalid")
        if until_cursor is not None and (
            not isinstance(until_cursor, int) or isinstance(until_cursor, bool)
            or until_cursor < after_cursor
        ):
            raise ValueError("project event replay cursor is invalid")
        connection = self._connect()
        try:
            retained_after = _project_event_retained_after_cursor(connection, project)
            latest_cursor = _project_event_head_cursor(connection, project)
            if after_cursor < retained_after:
                raise ValueError("project event cursor predates retained authority")
            if after_cursor > latest_cursor:
                raise ValueError("project event cursor is ahead of authority")
            if until_cursor is not None:
                rows = connection.execute(
                    "SELECT cursor,change_type,object_ref,object_revision,occurred_at "
                    "FROM ai_project_event_feed WHERE project_id=? AND cursor>? AND cursor<=? "
                    "ORDER BY cursor LIMIT ?",
                    (project, after_cursor, until_cursor, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT cursor,change_type,object_ref,object_revision,occurred_at "
                    "FROM ai_project_event_feed WHERE project_id=? AND cursor>? "
                    "ORDER BY cursor LIMIT ?",
                    (project, after_cursor, limit),
                ).fetchall()
            return tuple(_project_event_row(row) for row in rows)
        finally:
            connection.close()

    def append_external_agent_change(
        self, event: Mapping[str, object], *, expected_sequence: int,
        project_id: str, change_type: str, object_ref: str,
        object_revision: str, occurred_at: str,
        run_lease: RunLeaseToken | None = None,
    ) -> Mapping[str, object]:
        """Atomically append a Turn event and its privacy-safe change projection."""
        project = _required_project_id(project_id)
        projected = _external_agent_change_projection(
            project_id=project, change_type=change_type, object_ref=object_ref,
            object_revision=object_revision, occurred_at=occurred_at,
        )
        turn_id = str(event.get("turn_id", ""))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_run_lease(connection, turn_id, run_lease)
            request_row = connection.execute(
                "SELECT request_json FROM ai_turns WHERE turn_id=?", (turn_id,),
            ).fetchone()
            if request_row is None or _request_project_id(_mapping_json(request_row[0])) != project:
                raise TurnStateConflict("external agent change project does not own Turn")
            appended = self._append_event_in_transaction(
                connection, event, expected_sequence=expected_sequence,
            )
            cursor = _project_event_head_cursor(connection, project) + 1
            connection.execute(
                "INSERT INTO ai_project_event_feed(project_id,cursor,change_type,object_ref,object_revision,occurred_at) "
                "VALUES(?,?,?,?,?,?)",
                (project, cursor, projected["change_type"], projected["object_ref"],
                 projected["object_revision"], projected["occurred_at"]),
            )
            connection.execute("COMMIT")
            return {
                "event": deepcopy(appended),
                "change": {"cursor": cursor, **projected},
            }
        except sqlite3.IntegrityError as error:
            _rollback(connection)
            raise TurnEventConflict("turn event or project change identity conflict") from error
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def ingest_external_agent_publication_change(
        self, event: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Idempotently receive a publication-owned change into the project feed."""
        projected = _external_agent_publication_projection(event)
        identity = projected["publication_identity"]
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT change_type,object_ref,object_revision,occurred_at,change_cursor "
                "FROM ai_external_agent_publication_inbox WHERE project_id=? AND publication_identity=?",
                (projected["project_id"], identity),
            ).fetchone()
            if existing is not None:
                observed = {
                    "publication_identity": identity, "project_id": projected["project_id"],
                    "change_type": str(existing[0]), "object_ref": str(existing[1]),
                    "object_revision": str(existing[2]), "occurred_at": str(existing[3]),
                }
                if observed != projected:
                    raise TurnStateConflict("external agent publication identity conflict")
                connection.execute("COMMIT")
                return {"change": _public_publication_change(int(existing[4]), observed), "replayed": True}
            cursor = _project_event_head_cursor(connection, projected["project_id"]) + 1
            connection.execute(
                "INSERT INTO ai_project_event_feed(project_id,cursor,change_type,object_ref,object_revision,occurred_at,publication_identity) "
                "VALUES(?,?,?,?,?,?,?)",
                (projected["project_id"], cursor, projected["change_type"],
                 projected["object_ref"], projected["object_revision"],
                 projected["occurred_at"], identity),
            )
            connection.execute(
                "INSERT INTO ai_external_agent_publication_inbox("
                "project_id,publication_identity,change_type,object_ref,object_revision,occurred_at,change_cursor) "
                "VALUES(?,?,?,?,?,?,?)",
                (projected["project_id"], identity, projected["change_type"],
                 projected["object_ref"], projected["object_revision"],
                 projected["occurred_at"], cursor),
            )
            connection.execute("COMMIT")
            return {"change": _public_publication_change(cursor, projected), "replayed": False}
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def create_external_agent_session(
        self, session_id: str, payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Persist one read-only Bridge map in the Session database."""
        if not isinstance(session_id, str) or not session_id.startswith("agent-session-"):
            raise ValueError("external agent session identity is invalid")
        encoded = _encode(dict(payload))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO ai_external_agent_sessions("
                "session_id,project_id,turn_id,expires_at,resolved_bytes,delivered_cursor,acknowledged_cursor,session_json) "
                "VALUES(?,?,?,?,0,?,?,?)",
                (
                    session_id,
                    _required_project_id(payload.get("project_id")),
                    str(payload.get("turn_id", "")),
                    str(payload.get("expires_at", "")),
                    int(payload.get("delivered_cursor", -1)),
                    int(payload.get("acknowledged_cursor", -1)),
                    encoded,
                ),
            )
            connection.execute("COMMIT")
            return deepcopy(dict(payload))
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def get_external_agent_start_receipt(
        self, operation_id: str, *, request: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        encoded_request = _encode(dict(request))
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT operation,request_json,receipt_json "
                "FROM ai_external_agent_read_receipts WHERE operation_id=?", (operation_id,),
            ).fetchone()
            if row is None:
                return None
            if str(row[0]) != "start_session" or str(row[1]) != encoded_request:
                raise TurnStateConflict("external agent operation idempotency identity conflict")
            return _mapping_json(row[2])
        finally:
            connection.close()

    def create_external_agent_session_with_receipt(
        self, session_id: str, payload: Mapping[str, object], *, operation_id: str,
        request: Mapping[str, object], receipt: Mapping[str, object],
    ) -> Mapping[str, object]:
        if not isinstance(session_id, str) or not session_id.startswith("agent-session-"):
            raise ValueError("external agent session identity is invalid")
        encoded_payload = _encode(dict(payload))
        encoded_request = _encode(dict(request))
        encoded_receipt = _encode(dict(receipt))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute(
                "SELECT operation,request_json,receipt_json "
                "FROM ai_external_agent_read_receipts WHERE operation_id=?", (operation_id,),
            ).fetchone()
            if replay is not None:
                if str(replay[0]) != "start_session" or str(replay[1]) != encoded_request:
                    raise TurnStateConflict("external agent operation idempotency identity conflict")
                connection.execute("COMMIT")
                return _mapping_json(replay[2])
            connection.execute(
                "INSERT INTO ai_external_agent_sessions("
                "session_id,project_id,turn_id,expires_at,resolved_bytes,delivered_cursor,acknowledged_cursor,session_json) "
                "VALUES(?,?,?,?,0,?,?,?)",
                (
                    session_id, _required_project_id(payload.get("project_id")),
                    str(payload.get("turn_id", "")), str(payload.get("expires_at", "")),
                    int(payload.get("delivered_cursor", -1)),
                    int(payload.get("acknowledged_cursor", -1)), encoded_payload,
                ),
            )
            connection.execute(
                "INSERT INTO ai_external_agent_read_receipts(operation_id,session_id,operation,request_json,receipt_json) "
                "VALUES(?,?,?,?,?)",
                (operation_id, session_id, "start_session", encoded_request, encoded_receipt),
            )
            connection.execute("COMMIT")
            return _mapping_json(encoded_receipt)
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def get_external_agent_session(self, session_id: str) -> Mapping[str, object] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT session_json,resolved_bytes,delivered_cursor,acknowledged_cursor "
                "FROM ai_external_agent_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            payload = dict(_mapping_json(row[0]))
            payload["resolved_bytes"] = int(row[1])
            payload["delivered_cursor"] = int(row[2])
            payload["acknowledged_cursor"] = int(row[3])
            return payload
        finally:
            connection.close()

    def reserve_external_agent_context_bytes(
        self, session_id: str, *, expected_resolved_bytes: int, additional_bytes: int,
        maximum_bytes: int,
    ) -> int:
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (expected_resolved_bytes, additional_bytes, maximum_bytes)
        ):
            raise ValueError("external agent context byte reservation is invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT resolved_bytes FROM ai_external_agent_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            current = int(row[0])
            if current != expected_resolved_bytes:
                raise TurnStateConflict("external agent context budget revision conflict")
            updated = current + additional_bytes
            if updated > maximum_bytes:
                raise ValueError("external agent context byte budget exceeded")
            connection.execute(
                "UPDATE ai_external_agent_sessions SET resolved_bytes=? WHERE session_id=?",
                (updated, session_id),
            )
            connection.execute("COMMIT")
            return updated
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def get_external_agent_read_receipt(
        self, operation_id: str, *, session_id: str, operation: str,
        request: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        encoded_request = _encode(dict(request))
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT session_id,operation,request_json,receipt_json "
                "FROM ai_external_agent_read_receipts WHERE operation_id=?", (operation_id,),
            ).fetchone()
            if row is None:
                return None
            if str(row[0]) != session_id or str(row[1]) != operation or str(row[2]) != encoded_request:
                raise TurnStateConflict("external agent operation idempotency identity conflict")
            return _mapping_json(row[3])
        finally:
            connection.close()

    def reserve_external_agent_context_bytes_with_receipt(
        self, session_id: str, *, operation_id: str, request: Mapping[str, object],
        expected_resolved_bytes: int, additional_bytes: int, maximum_bytes: int,
        receipt: Mapping[str, object],
    ) -> Mapping[str, object]:
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0
               for value in (expected_resolved_bytes, additional_bytes, maximum_bytes)):
            raise ValueError("external agent context byte reservation is invalid")
        return self._commit_external_agent_read_operation(
            session_id, operation_id=operation_id, operation="resolve_context", request=request,
            receipt=receipt, expected_resolved_bytes=expected_resolved_bytes,
            additional_bytes=additional_bytes, maximum_bytes=maximum_bytes,
        )

    def record_external_agent_delivery_with_receipt(
        self, session_id: str, *, operation_id: str, request: Mapping[str, object],
        expected_delivered_cursor: int, delivered_cursor: int,
        receipt: Mapping[str, object],
    ) -> Mapping[str, object]:
        return self._commit_external_agent_read_operation(
            session_id, operation_id=operation_id, operation="get_changes", request=request,
            receipt=receipt, expected_delivered_cursor=expected_delivered_cursor,
            delivered_cursor=delivered_cursor,
        )

    def acknowledge_external_agent_delivery_with_receipt(
        self, session_id: str, *, operation_id: str, request: Mapping[str, object],
        acknowledged_cursor: int, receipt: Mapping[str, object],
    ) -> Mapping[str, object]:
        return self._commit_external_agent_read_operation(
            session_id, operation_id=operation_id, operation="acknowledge_changes", request=request,
            receipt=receipt, acknowledged_cursor=acknowledged_cursor,
        )

    def external_agent_resolved_context_refs(self, session_id: str) -> tuple[str, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT request_json FROM ai_external_agent_read_receipts "
                "WHERE session_id=? AND operation='resolve_context' ORDER BY operation_id",
                (session_id,),
            ).fetchall()
            refs: set[str] = set()
            for row in rows:
                request = _mapping_json(row[0])
                values = request.get("context_refs")
                if isinstance(values, list):
                    refs.update(item for item in values if isinstance(item, str))
            return tuple(sorted(refs))
        finally:
            connection.close()

    def reserve_external_agent_proposal_operation(
        self, session_id: str, *, operation_id: str, project_id: str,
        request: Mapping[str, object], created_at: str,
    ) -> Mapping[str, object]:
        encoded_request = _encode(dict(request))
        effect_intent = _external_agent_proposal_effect_intent(
            operation_id=operation_id,
            session_id=session_id,
            project_id=project_id,
            request=request,
        )
        effect_now = _effect_time(created_at)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT project_id FROM ai_external_agent_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None or str(row[0]) != _required_project_id(project_id):
                raise TurnStateConflict("external agent proposal session scope drifted")
            existing = connection.execute(
                "SELECT session_id,project_id,request_json,status,proposal_ref,proposal_revision,result_json,change_cursor "
                "FROM ai_external_agent_proposal_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != session_id or str(existing[1]) != project_id or str(existing[2]) != encoded_request:
                    raise TurnStateConflict("external agent proposal operation identity conflict")
                effect, created = self._effect_log.plan_in_connection(
                    connection, effect_intent, now=effect_now,
                )
                if created or effect.state is EffectState.PLANNED:
                    self._effect_runner.begin_planned(
                        operation_id, connection=connection, now=effect_now,
                        lease_expires_at=effect_now + 60,
                    )
                connection.execute("COMMIT")
                return _external_agent_proposal_operation(existing, operation_id=operation_id, replayed=True)
            connection.execute(
                "INSERT INTO ai_external_agent_proposal_operations("
                "operation_id,session_id,project_id,request_json,status,proposal_ref,proposal_revision,result_json,change_cursor,created_at,updated_at) "
                "VALUES(?,?,?,?, 'prepared',NULL,NULL,NULL,NULL,?,?)",
                (operation_id, session_id, project_id, encoded_request, created_at, created_at),
            )
            effect, created = self._effect_log.plan_in_connection(
                connection, effect_intent, now=effect_now,
            )
            if not created:
                raise TurnStateConflict("external agent proposal effect identity conflict")
            self._effect_runner.begin_planned(
                operation_id, connection=connection, now=effect_now,
                lease_expires_at=effect_now + 60,
            )
            connection.execute("COMMIT")
            return {
                "operation_id": operation_id, "session_id": session_id,
                "project_id": project_id, "status": "prepared", "replayed": False,
            }
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def finalize_external_agent_proposal_operation(
        self, session_id: str, *, operation_id: str, project_id: str,
        request: Mapping[str, object], proposal_ref: str, proposal_revision: str,
        result: Mapping[str, object], occurred_at: str,
    ) -> Mapping[str, object]:
        encoded_request = _encode(dict(request))
        encoded_result = _encode(dict(result))
        projected = _external_agent_change_projection(
            project_id=project_id, change_type="memory.proposed",
            object_ref=proposal_ref, object_revision=proposal_revision,
            occurred_at=occurred_at,
        )
        effect_now = _effect_time(occurred_at)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT session_id,project_id,request_json,status,proposal_ref,proposal_revision,result_json,change_cursor "
                "FROM ai_external_agent_proposal_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None or str(row[0]) != session_id or str(row[1]) != project_id or str(row[2]) != encoded_request:
                raise TurnStateConflict("external agent proposal operation identity conflict")
            if str(row[3]) == "finalized":
                if str(row[4]) != proposal_ref or str(row[5]) != proposal_revision or str(row[6]) != encoded_result:
                    raise TurnStateConflict("external agent proposal outcome drifted")
                effect_row = connection.execute(
                    "SELECT state FROM effect WHERE operation_id=?", (operation_id,),
                ).fetchone()
                if effect_row is not None and str(effect_row[0]) == EffectState.INFLIGHT.value:
                    current_effect = self._effect_log.get_in_connection(connection, operation_id)
                    self._effect_runner.settle_ok(
                        current_effect, connection=connection,
                        receipt_ref=proposal_ref,
                        receipt_kind="external-agent-memory-proposal",
                        now=effect_now,
                    )
                connection.execute("COMMIT")
                return _external_agent_proposal_operation(row, operation_id=operation_id, replayed=True)
            cursor = _project_event_head_cursor(connection, project_id) + 1
            connection.execute(
                "INSERT INTO ai_project_event_feed(project_id,cursor,change_type,object_ref,object_revision,occurred_at) "
                "VALUES(?,?,?,?,?,?)",
                (project_id, cursor, projected["change_type"], projected["object_ref"],
                 projected["object_revision"], projected["occurred_at"]),
            )
            connection.execute(
                "UPDATE ai_external_agent_proposal_operations SET status='finalized',proposal_ref=?,proposal_revision=?,"
                "result_json=?,change_cursor=?,updated_at=? WHERE operation_id=? AND status='prepared'",
                (proposal_ref, proposal_revision, encoded_result, cursor, occurred_at, operation_id),
            )
            try:
                current_effect = self._effect_log.get_in_connection(connection, operation_id)
                self._effect_runner.settle_ok(
                    current_effect, connection=connection,
                    receipt_ref=proposal_ref,
                    receipt_kind="external-agent-memory-proposal",
                    now=effect_now,
                )
            except InvalidEffectTransition as error:
                raise TurnStateConflict("external agent proposal effect state drifted") from error
            connection.execute("COMMIT")
            final_row = (session_id, project_id, encoded_request, "finalized", proposal_ref,
                         proposal_revision, encoded_result, cursor)
            return _external_agent_proposal_operation(final_row, operation_id=operation_id, replayed=False)
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def prepared_external_agent_proposal_operations(
        self, *, limit: int = 32,
    ) -> tuple[Mapping[str, object], ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 128:
            raise ValueError("external agent proposal recovery limit is invalid")
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT operation_id,session_id,project_id,request_json,created_at "
                "FROM ai_external_agent_proposal_operations AS operation "
                "WHERE status='prepared' AND ("
                "NOT EXISTS(SELECT 1 FROM effect WHERE effect.operation_id=operation.operation_id) OR "
                "EXISTS(SELECT 1 FROM effect WHERE effect.operation_id=operation.operation_id "
                "AND effect.state IN ('PLANNED','INFLIGHT'))) "
                "ORDER BY updated_at,operation_id LIMIT ?", (limit,),
            ).fetchall()
            return tuple({
                "operation_id": str(row[0]), "session_id": str(row[1]),
                "project_id": str(row[2]), "request": _mapping_json(row[3]),
                "created_at": str(row[4]),
            } for row in rows)
        finally:
            connection.close()

    def defer_external_agent_proposal_operation(
        self, operation_id: str, *, updated_at: str,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE ai_external_agent_proposal_operations SET updated_at=? "
                "WHERE operation_id=? AND status='prepared'",
                (updated_at, operation_id),
            ).rowcount
            if changed != 1:
                raise KeyError(operation_id)
            connection.execute("COMMIT")
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def _commit_external_agent_read_operation(
        self, session_id: str, *, operation_id: str, operation: str,
        request: Mapping[str, object], receipt: Mapping[str, object],
        expected_resolved_bytes: int | None = None, additional_bytes: int | None = None,
        maximum_bytes: int | None = None, expected_delivered_cursor: int | None = None,
        delivered_cursor: int | None = None, acknowledged_cursor: int | None = None,
    ) -> Mapping[str, object]:
        encoded_request = _encode(dict(request))
        encoded_receipt = _encode(dict(receipt))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute(
                "SELECT session_id,operation,request_json,receipt_json "
                "FROM ai_external_agent_read_receipts WHERE operation_id=?", (operation_id,),
            ).fetchone()
            if replay is not None:
                if str(replay[0]) != session_id or str(replay[1]) != operation or str(replay[2]) != encoded_request:
                    raise TurnStateConflict("external agent operation idempotency identity conflict")
                connection.execute("COMMIT")
                return _mapping_json(replay[3])
            row = connection.execute(
                "SELECT resolved_bytes,delivered_cursor,acknowledged_cursor "
                "FROM ai_external_agent_sessions WHERE session_id=?", (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            current_resolved, current_delivered, current_acknowledged = map(int, row)
            if expected_resolved_bytes is not None:
                if current_resolved != expected_resolved_bytes:
                    raise TurnStateConflict("external agent context budget revision conflict")
                assert additional_bytes is not None and maximum_bytes is not None
                updated = current_resolved + additional_bytes
                if updated > maximum_bytes:
                    raise ValueError("external agent context byte budget exceeded")
                connection.execute(
                    "UPDATE ai_external_agent_sessions SET resolved_bytes=? WHERE session_id=?",
                    (updated, session_id),
                )
            if expected_delivered_cursor is not None:
                if current_delivered != expected_delivered_cursor:
                    raise TurnStateConflict("external agent delivery cursor revision conflict")
                assert delivered_cursor is not None
                if delivered_cursor < current_delivered:
                    raise TurnStateConflict("external agent delivery cursor cannot move backwards")
                connection.execute(
                    "UPDATE ai_external_agent_sessions SET delivered_cursor=? WHERE session_id=?",
                    (delivered_cursor, session_id),
                )
            if acknowledged_cursor is not None:
                if acknowledged_cursor < current_acknowledged:
                    raise TurnStateConflict("external agent acknowledgement cursor cannot move backwards")
                if acknowledged_cursor > current_delivered:
                    raise TurnStateConflict("external agent acknowledgement cursor exceeds delivery")
                connection.execute(
                    "UPDATE ai_external_agent_sessions SET acknowledged_cursor=? WHERE session_id=?",
                    (acknowledged_cursor, session_id),
                )
                receipt = {**receipt, "acknowledged_cursor": acknowledged_cursor,
                           "delivered_cursor": current_delivered}
                encoded_receipt = _encode(receipt)
            connection.execute(
                "INSERT INTO ai_external_agent_read_receipts(operation_id,session_id,operation,request_json,receipt_json) "
                "VALUES(?,?,?,?,?)",
                (operation_id, session_id, operation, encoded_request, encoded_receipt),
            )
            connection.execute("COMMIT")
            return _mapping_json(encoded_receipt)
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def reserve_immutable_payload(
        self, turn_id: str, kind: str, payload: object,
    ) -> tuple[str, bool]:
        _immutable_payload_identity(turn_id, kind)
        validate_governed_payload(payload)
        encoded = _encode(payload)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_ref, payload_json FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?",
                (turn_id, kind),
            ).fetchone()
            if row is not None:
                if str(row[1]) != encoded:
                    raise ValueError("immutable payload identity conflict")
                connection.execute("COMMIT")
                return str(row[0]), False
            ref = f"crp://session/{turn_id}/{kind}/{uuid4().hex}"
            connection.execute(
                "INSERT INTO ai_turn_immutable_payloads(payload_ref, turn_id, kind, payload_json) VALUES(?,?,?,?)",
                (ref, turn_id, kind, encoded),
            )
            connection.execute("COMMIT")
            return ref, True
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def reserve_mcp_side_effect(
        self, intent: Mapping[str, object],
    ) -> tuple[str, bool]:
        """Atomically freeze one MCP intent and make Effect the execution authority."""
        turn_id, invocation_id = _mcp_effect_identity(intent)
        kind = f"mcp-side-effect-intent-{invocation_id}"
        _immutable_payload_identity(turn_id, kind)
        validate_governed_payload(intent)
        encoded = _encode(dict(intent))
        now = int(datetime.now(timezone.utc).timestamp())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_ref,payload_json FROM ai_turn_immutable_payloads "
                "WHERE turn_id=? AND kind=?", (turn_id, kind),
            ).fetchone()
            if row is not None:
                if str(row[1]) != encoded:
                    raise TurnStateConflict("MCP side-effect intent identity conflict")
                connection.execute("COMMIT")
                return str(row[0]), False
            intent_ref = _payload_ref(turn_id, kind)
            connection.execute(
                "INSERT INTO ai_turn_immutable_payloads(payload_ref,turn_id,kind,payload_json) "
                "VALUES(?,?,?,?)", (intent_ref, turn_id, kind, encoded),
            )
            effect_intent = _mcp_side_effect_intent(
                connection, intent=intent, intent_ref=intent_ref,
            )
            effect, created = self._effect_log.plan_in_connection(
                connection, effect_intent, now=now,
            )
            if not created:
                raise TurnStateConflict("MCP side-effect Effect identity conflict")
            lease_ttl_seconds = intent.get("lease_ttl_seconds")
            if (
                not isinstance(lease_ttl_seconds, int)
                or isinstance(lease_ttl_seconds, bool)
                or not 1 <= lease_ttl_seconds <= 3605
            ):
                raise ValueError("MCP side-effect lease TTL is invalid")
            self._effect_runner.begin_planned(
                effect.operation_id, connection=connection, now=now,
                lease_expires_at=now + lease_ttl_seconds,
            )
            connection.execute("COMMIT")
            return intent_ref, True
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def mark_mcp_side_effect_unknown(
        self, intent: Mapping[str, object], *, error_code: str,
    ) -> None:
        turn_id, invocation_id = _mcp_effect_identity(intent)
        if not isinstance(error_code, str) or not error_code.strip():
            raise ValueError("MCP unknown effect error code is invalid")
        operation_id = _mcp_effect_operation_id(invocation_id)
        now = int(datetime.now(timezone.utc).timestamp())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM effect WHERE operation_id=? AND turn_id=?",
                (operation_id, turn_id),
            ).fetchone()
            if row is None:
                raise TurnStateConflict("MCP side-effect Effect was not found")
            if str(row[0]) == EffectState.UNKNOWN.value:
                connection.execute("COMMIT")
                return
            current_effect = self._effect_log.get_in_connection(connection, operation_id)
            self._effect_runner.mark_unknown(
                current_effect, connection=connection, now=now, error_ref=error_code,
            )
            connection.execute("COMMIT")
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def settle_mcp_side_effect(
        self, receipt: Mapping[str, object],
    ) -> str:
        turn_id, invocation_id = _mcp_effect_identity(receipt)
        kind = f"mcp-call-receipt-{invocation_id}"
        _immutable_payload_identity(turn_id, kind)
        validate_governed_payload(receipt)
        operation_id = _mcp_effect_operation_id(invocation_id)
        now = int(datetime.now(timezone.utc).timestamp())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            intent_row = connection.execute(
                "SELECT payload_ref,payload_json FROM ai_turn_immutable_payloads "
                "WHERE turn_id=? AND kind=?",
                (turn_id, f"mcp-side-effect-intent-{invocation_id}"),
            ).fetchone()
            if intent_row is None:
                raise TurnStateConflict("MCP side-effect intent was not found")
            effect_row = connection.execute(
                "SELECT state FROM effect WHERE operation_id=?", (operation_id,),
            ).fetchone()
            if effect_row is None:
                intent = _mapping_json(intent_row[1])
                effect_intent = _mcp_side_effect_intent(
                    connection, intent=intent, intent_ref=str(intent_row[0]),
                )
                self._effect_log.plan_in_connection(connection, effect_intent, now=now)
                self._effect_runner.begin_planned(
                    operation_id, connection=connection, now=now,
                    lease_expires_at=now + 60,
                )
                source_state = EffectState.INFLIGHT
            else:
                source_state = EffectState(str(effect_row[0]))
            if source_state not in {EffectState.INFLIGHT, EffectState.UNKNOWN}:
                raise TurnStateConflict("MCP side-effect Effect state drifted")
            receipt_ref = self._get_or_create_immutable_payload_in_transaction(
                connection, turn_id, kind, dict(receipt),
            )
            current_effect = self._effect_log.get_in_connection(connection, operation_id)
            self._effect_runner.settle_verified_ok(
                current_effect, connection=connection,
                receipt_ref=receipt_ref,
                receipt_kind="mcp-call-receipt",
                now=now,
            )
            connection.execute("COMMIT")
            return receipt_ref
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def reserve_mcp_verified_none_replay(
        self, claim: Mapping[str, object],
    ) -> tuple[str, bool]:
        """Reserve one reviewed replay and reopen the same frozen MCP Effect."""
        turn_id, invocation_id = _mcp_effect_identity(claim)
        kind = f"mcp-verified-none-replay-{invocation_id}"
        _immutable_payload_identity(turn_id, kind)
        validate_governed_payload(claim)
        encoded = _encode(dict(claim))
        now = int(datetime.now(timezone.utc).timestamp())
        operation_id = _mcp_effect_operation_id(invocation_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_ref,payload_json FROM ai_turn_immutable_payloads "
                "WHERE turn_id=? AND kind=?", (turn_id, kind),
            ).fetchone()
            if existing is not None:
                if str(existing[1]) != encoded:
                    raise TurnStateConflict("MCP verified-none replay identity conflict")
                connection.execute("COMMIT")
                return str(existing[0]), False
            replay_ref = _payload_ref(turn_id, kind)
            connection.execute(
                "INSERT INTO ai_turn_immutable_payloads(payload_ref,turn_id,kind,payload_json) "
                "VALUES(?,?,?,?)", (replay_ref, turn_id, kind, encoded),
            )
            intent_row = connection.execute(
                "SELECT payload_ref,payload_json FROM ai_turn_immutable_payloads "
                "WHERE turn_id=? AND kind=?",
                (turn_id, f"mcp-side-effect-intent-{invocation_id}"),
            ).fetchone()
            if intent_row is None:
                connection.execute("COMMIT")
                return replay_ref, True
            effect_row = connection.execute(
                "SELECT state FROM effect WHERE operation_id=?", (operation_id,),
            ).fetchone()
            if effect_row is None:
                intent = _mapping_json(intent_row[1])
                effect_intent = _mcp_side_effect_intent(
                    connection, intent=intent, intent_ref=str(intent_row[0]),
                )
                self._effect_log.plan_in_connection(connection, effect_intent, now=now)
                self._effect_runner.begin_planned(
                    operation_id, connection=connection, now=now,
                    lease_expires_at=now + 60,
                )
                source_state = EffectState.INFLIGHT
            else:
                source_state = EffectState(str(effect_row[0]))
            if source_state is EffectState.INFLIGHT:
                current_effect = self._effect_log.get_in_connection(connection, operation_id)
                self._effect_runner.mark_unknown(
                    current_effect, connection=connection, now=now,
                    error_ref="mcp.verified_none", probe_ref=replay_ref,
                )
                source_state = EffectState.UNKNOWN
            if source_state is not EffectState.UNKNOWN:
                raise TurnStateConflict("MCP verified-none Effect state drifted")
            current_effect = self._effect_log.get_in_connection(connection, operation_id)
            self._effect_runner.reauthorize_unknown(
                current_effect, connection=connection, now=now, probe_ref=replay_ref,
            )
            self._effect_runner.begin_planned(
                operation_id, connection=connection, now=now,
                lease_expires_at=now + 60, probe_ref=replay_ref,
            )
            connection.execute("COMMIT")
            return replay_ref, True
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def get_immutable_payload(self, turn_id: str, kind: str) -> tuple[str, object] | None:
        _immutable_payload_identity(turn_id, kind)
        row = self._cached_immutable_read(("immutable", turn_id, kind),
            "SELECT payload_ref, payload_json FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?",
            (turn_id, kind))
        return (str(row[0]), json.loads(str(row[1]))) if row is not None else None

    def put_pending(self, turn_id: str, decision: Mapping[str, object]) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO ai_turn_pending(turn_id, decision_json) VALUES(?,?) ON CONFLICT(turn_id) DO UPDATE SET decision_json=excluded.decision_json", (turn_id, _encode(decision)))
            connection.execute("COMMIT")
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def get_pending(self, turn_id: str) -> Mapping[str, object] | None:
        connection = self._connect()
        try:
            row = connection.execute("SELECT decision_json FROM ai_turn_pending WHERE turn_id=?", (turn_id,)).fetchone()
            return _mapping_json(row[0]) if row else None
        finally:
            connection.close()

    def clear_pending(self, turn_id: str) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM ai_turn_pending WHERE turn_id=?", (turn_id,))
            connection.execute("COMMIT")
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def get_action(self, idempotency_key: str) -> tuple[Mapping[str, object], TurnReceipt] | None:
        connection = self._connect()
        try:
            row = connection.execute("SELECT action_json, receipt_json FROM ai_turn_actions WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            return (_mapping_json(row[0]), _receipt(_mapping_json(row[1]))) if row else None
        finally:
            connection.close()

    def save_action(self, action: Mapping[str, object], receipt: TurnReceipt) -> None:
        key = str(action["idempotency_key"])
        action_json = _encode(action)
        receipt_json = _encode(_receipt_payload(receipt))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT action_json, receipt_json FROM ai_turn_actions WHERE idempotency_key=?", (key,)).fetchone()
            if row is not None and (str(row[0]) != action_json or str(row[1]) != receipt_json):
                raise TurnStateConflict("turn action idempotency identity conflict")
            if row is None:
                connection.execute("INSERT INTO ai_turn_actions(idempotency_key, turn_id, action_json, receipt_json) VALUES(?,?,?,?)", (key, receipt.turn_id, action_json, receipt_json))
            connection.execute("COMMIT")
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    @reusable_connection
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5, check_same_thread=False)
        observe_connection(connection)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _assert_run_lease(
        connection: sqlite3.Connection,
        turn_id: str,
        run_lease: RunLeaseToken | None,
        *,
        now: float | None = None,
    ) -> None:
        if run_lease is None:
            return
        validate_run_lease_token(run_lease)
        if run_lease.turn_id != turn_id or not connection.execute(
            "SELECT 1 FROM ai_turn_run_leases WHERE turn_id=? AND owner_id=? AND generation=? AND status='active'",
            (run_lease.turn_id, run_lease.owner_id, run_lease.generation),
        ).fetchone():
            raise RunLeaseRevoked()
        if now is not None:
            row = connection.execute(
                "SELECT stale_after FROM ai_turn_run_leases WHERE turn_id=? AND owner_id=? AND generation=? AND status='active'",
                (run_lease.turn_id, run_lease.owner_id, run_lease.generation),
            ).fetchone()
            if row is None or _lease_datetime(row[0]).timestamp() <= now:
                raise RunLeaseRevoked()

    @staticmethod
    def _assert_model_attempt_run_lease(
        connection: sqlite3.Connection,
        turn_id: str,
        run_lease: RunLeaseToken | None,
        *,
        now: float | None = None,
    ) -> None:
        """Require a token whenever a strict active lease owns this Turn."""
        active = connection.execute(
            "SELECT 1 FROM ai_turn_run_leases WHERE turn_id=? AND status='active'",
            (turn_id,),
        ).fetchone()
        if run_lease is None:
            if active is not None:
                raise RunLeaseRevoked()
            return
        SQLiteAITurnStore._assert_run_lease(connection, turn_id, run_lease, now=now)

    @staticmethod
    def _assert_model_attempt_event_identity(
        event: Mapping[str, object],
        payload: Mapping[str, object],
        *,
        expected_type: str,
    ) -> None:
        if event.get("type") != expected_type:
            raise ValueError("model attempt Event type is invalid")
        if str(event.get("turn_id", "")) != payload["turn_id"]:
            raise ValueError("model attempt Event Turn identity drifted")
        correlation = event.get("correlation")
        if not isinstance(correlation, Mapping) or correlation.get("model_request_id") != payload["model_request_id"]:
            raise ValueError("model attempt Event request identity drifted")

    @staticmethod
    def _append_event_in_transaction(
        connection: sqlite3.Connection,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
    ) -> Mapping[str, object]:
        turn_id = str(event.get("turn_id", ""))
        row = connection.execute(
            "SELECT sequence, event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence DESC LIMIT 1",
            (turn_id,),
        ).fetchone()
        actual = int(row[0]) if row else 0
        if actual != expected_sequence:
            raise TurnEventConflict("turn event expected sequence conflict")
        validated = validate_event_transition(_mapping_json(row[1]) if row else None, event)
        connection.execute(
            "INSERT INTO ai_turn_events(turn_id, sequence, event_id, event_json) VALUES(?,?,?,?)",
            (turn_id, int(validated["sequence"]), str(validated["event_id"]), _encode(validated)),
        )
        return validated

    @staticmethod
    def _put_payload_in_transaction(
        connection: sqlite3.Connection,
        payload_ref: str,
        turn_id: str,
        kind: str,
        payload: object,
    ) -> None:
        connection.execute(
            "INSERT INTO ai_turn_payloads(payload_ref, turn_id, kind, payload_json) VALUES(?,?,?,?)",
            (payload_ref, turn_id, kind, _encode(payload)),
        )

    @staticmethod
    def _get_or_create_immutable_payload_in_transaction(
        connection: sqlite3.Connection,
        turn_id: str,
        kind: str,
        payload: object,
    ) -> str:
        encoded = _encode(payload)
        row = connection.execute(
            "SELECT payload_ref, payload_json FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?",
            (turn_id, kind),
        ).fetchone()
        if row is not None:
            if str(row[1]) != encoded:
                raise ValueError("immutable payload identity conflict")
            return str(row[0])
        ref = _payload_ref(turn_id, kind)
        connection.execute(
            "INSERT INTO ai_turn_immutable_payloads(payload_ref, turn_id, kind, payload_json) VALUES(?,?,?,?)",
            (ref, turn_id, kind, encoded),
        )
        return ref


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS ai_turn_schema(version INTEGER NOT NULL);
        INSERT INTO ai_turn_schema(version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM ai_turn_schema);
        CREATE TABLE IF NOT EXISTS ai_turns(turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, operation_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, request_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_turn_events(turn_id TEXT NOT NULL REFERENCES ai_turns(turn_id), sequence INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE, event_json TEXT NOT NULL, PRIMARY KEY(turn_id, sequence));
        CREATE TABLE IF NOT EXISTS ai_project_event_feed(project_id TEXT NOT NULL, cursor INTEGER NOT NULL, change_type TEXT NOT NULL, object_ref TEXT NOT NULL, object_revision TEXT NOT NULL, occurred_at TEXT NOT NULL, publication_identity TEXT NULL, PRIMARY KEY(project_id,cursor));
        CREATE INDEX IF NOT EXISTS ai_project_event_feed_project_cursor ON ai_project_event_feed(project_id,cursor);
        CREATE TABLE IF NOT EXISTS ai_project_event_feed_retention(project_id TEXT PRIMARY KEY, retained_after_cursor INTEGER NOT NULL CHECK(retained_after_cursor>=0));
        CREATE TABLE IF NOT EXISTS ai_external_agent_publication_inbox(project_id TEXT NOT NULL, publication_identity TEXT NOT NULL, change_type TEXT NOT NULL, object_ref TEXT NOT NULL, object_revision TEXT NOT NULL, occurred_at TEXT NOT NULL, change_cursor INTEGER NOT NULL, PRIMARY KEY(project_id,publication_identity));
        CREATE TABLE IF NOT EXISTS ai_external_agent_sessions(session_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, turn_id TEXT NOT NULL REFERENCES ai_turns(turn_id), expires_at TEXT NOT NULL, resolved_bytes INTEGER NOT NULL CHECK(resolved_bytes>=0), delivered_cursor INTEGER NOT NULL CHECK(delivered_cursor>=0), acknowledged_cursor INTEGER NOT NULL CHECK(acknowledged_cursor>=0 AND acknowledged_cursor<=delivered_cursor), session_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_external_agent_read_receipts(operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES ai_external_agent_sessions(session_id), operation TEXT NOT NULL, request_json TEXT NOT NULL, receipt_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_external_agent_proposal_operations(operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES ai_external_agent_sessions(session_id), project_id TEXT NOT NULL, request_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('prepared','finalized')), proposal_ref TEXT NULL, proposal_revision TEXT NULL, result_json TEXT NULL, change_cursor INTEGER NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_turn_payloads(payload_ref TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES ai_turns(turn_id), kind TEXT NOT NULL, payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_turn_immutable_payloads(payload_ref TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES ai_turns(turn_id), kind TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(turn_id, kind));
        CREATE TABLE IF NOT EXISTS ai_turn_pending(turn_id TEXT PRIMARY KEY REFERENCES ai_turns(turn_id), decision_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_expert_job_waits(turn_id TEXT PRIMARY KEY REFERENCES ai_turns(turn_id), job_ref TEXT NOT NULL, admission_job_revision INTEGER NOT NULL CHECK(admission_job_revision>=1), snapshot_ref TEXT NOT NULL REFERENCES ai_turn_immutable_payloads(payload_ref), status TEXT NOT NULL CHECK(status IN ('waiting','wake_enqueued','terminal_observed')), terminal_job_revision INTEGER NULL CHECK(terminal_job_revision>=admission_job_revision));
        CREATE TABLE IF NOT EXISTS ai_turn_actions(idempotency_key TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES ai_turns(turn_id), action_json TEXT NOT NULL, receipt_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_turn_run_generations(turn_id TEXT PRIMARY KEY REFERENCES ai_turns(turn_id), generation INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_turn_run_leases(turn_id TEXT PRIMARY KEY REFERENCES ai_turns(turn_id), owner_id TEXT NOT NULL, generation INTEGER NOT NULL, status TEXT NULL CHECK(status IN ('active','recovery_required','quarantined')), acquired_at TEXT NULL, heartbeat_at TEXT NULL, stale_after TEXT NULL, recovery_attempts INTEGER NULL, recovery_attempted_at TEXT NULL);
        CREATE TABLE IF NOT EXISTS ai_turn_recovery_audit(turn_id TEXT NOT NULL, generation INTEGER NOT NULL, old_owner_id TEXT NOT NULL, old_acquired_at TEXT NOT NULL, old_heartbeat_at TEXT NOT NULL, old_stale_after TEXT NOT NULL, disposition TEXT NOT NULL, reason_code TEXT NOT NULL, last_sequence INTEGER NOT NULL, last_event_id TEXT NOT NULL, last_event_type TEXT NOT NULL, classifier_version TEXT NOT NULL, scanner_actor TEXT NOT NULL, observed_at TEXT NOT NULL, PRIMARY KEY(turn_id,generation));
        CREATE TABLE IF NOT EXISTS ai_turn_recovery_queue(turn_id TEXT NOT NULL, generation INTEGER NOT NULL, reason_code TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed')), attempts INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(turn_id,generation));
        CREATE TABLE IF NOT EXISTS ai_turn_recovery_queue_audit(audit_id INTEGER PRIMARY KEY AUTOINCREMENT, turn_id TEXT NOT NULL, generation INTEGER NOT NULL, attempt INTEGER NOT NULL, status TEXT NOT NULL, reason_code TEXT NOT NULL, owner_id TEXT NOT NULL, new_generation INTEGER NOT NULL, observed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_turn_recovery_reviews(review_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES ai_turns(turn_id), generation INTEGER NOT NULL, project_id TEXT NULL, reason_code TEXT NOT NULL, last_sequence INTEGER NOT NULL, last_event_id TEXT NOT NULL, last_event_type TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('quarantined','kept_quarantined','resume_queued','turn_completed','turn_failed','turn_cancelled','waiting_approval','resume_failed')), revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(turn_id,generation));
        CREATE TABLE IF NOT EXISTS ai_turn_recovery_review_audit(audit_id INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT NOT NULL REFERENCES ai_turn_recovery_reviews(review_id), revision INTEGER NOT NULL, action TEXT NOT NULL, actor_id TEXT NOT NULL, boundary_outcome TEXT NOT NULL, boundary_reason_codes TEXT NOT NULL, policy_revision INTEGER NOT NULL, observed_at TEXT NOT NULL, UNIQUE(review_id,revision));
        CREATE TABLE IF NOT EXISTS ai_model_attempt_reservations(turn_id TEXT NOT NULL REFERENCES ai_turns(turn_id), model_request_id TEXT NOT NULL, attempt_number INTEGER NOT NULL, attempt_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL CHECK(status IN ('committed','terminal')), dispatch_payload_ref TEXT NOT NULL REFERENCES ai_turn_payloads(payload_ref), terminal_receipt_ref TEXT NULL REFERENCES ai_turn_payloads(payload_ref), lease_owner_id TEXT NULL, lease_generation INTEGER NULL, dispatched_at TEXT NOT NULL, terminal_status TEXT NULL, PRIMARY KEY(turn_id,model_request_id,attempt_number), CHECK((lease_owner_id IS NULL) = (lease_generation IS NULL)), CHECK(terminal_status IS NULL OR status='terminal'));
    """)
    _migrate_external_agent_feed(connection)
    _migrate_external_agent_publication_feed(connection)
    _migrate_external_agent_publication_inbox(connection)
    _migrate_external_agent_sessions(connection)
    _migrate_external_agent_session_retention_fields(connection)
    _migrate_run_lease_columns(connection)
    _migrate_recovery_columns(connection)
    row = connection.execute("SELECT version FROM ai_turn_schema").fetchone()
    if row is None or int(row[0]) != SQLiteAITurnStore.schema_version:
        raise RuntimeError("unsupported AI Turn SQLite schema version")


def _migrate_external_agent_feed(connection: sqlite3.Connection) -> None:
    """Replace the abandoned global-rowid preview with project-local cursors."""
    columns = tuple(connection.execute("PRAGMA table_info(ai_project_event_feed)"))
    primary_key = {str(row[1]): int(row[5]) for row in columns if int(row[5]) > 0}
    if primary_key == {"project_id": 1, "cursor": 2}:
        return
    if primary_key != {"cursor": 1}:
        raise RuntimeError("unsupported external agent feed schema")
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DROP INDEX IF EXISTS ai_project_event_feed_project_cursor")
        connection.execute("ALTER TABLE ai_project_event_feed RENAME TO ai_project_event_feed_legacy")
        connection.execute(
            "CREATE TABLE ai_project_event_feed(project_id TEXT NOT NULL,cursor INTEGER NOT NULL,"
            "change_type TEXT NOT NULL,object_ref TEXT NOT NULL,object_revision TEXT NOT NULL,"
            "occurred_at TEXT NOT NULL,PRIMARY KEY(project_id,cursor))"
        )
        connection.execute(
            "INSERT INTO ai_project_event_feed(project_id,cursor,change_type,object_ref,object_revision,occurred_at) "
            "SELECT project_id,ROW_NUMBER() OVER(PARTITION BY project_id ORDER BY cursor),"
            "change_type,object_ref,object_revision,occurred_at FROM ai_project_event_feed_legacy"
        )
        connection.execute("DROP TABLE ai_project_event_feed_legacy")
        connection.execute(
            "CREATE INDEX ai_project_event_feed_project_cursor "
            "ON ai_project_event_feed(project_id,cursor)"
        )
        connection.execute("COMMIT")
    except Exception:
        _rollback(connection)
        raise


def _migrate_external_agent_publication_feed(connection: sqlite3.Connection) -> None:
    """Add publisher identity while preserving all historical feed records."""
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(ai_project_event_feed)")}
    connection.execute("BEGIN IMMEDIATE")
    try:
        if "publication_identity" not in columns:
            connection.execute("ALTER TABLE ai_project_event_feed ADD COLUMN publication_identity TEXT NULL")
        connection.execute("DROP INDEX IF EXISTS ai_project_event_feed_publication_identity")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ai_project_event_feed_publication_identity "
            "ON ai_project_event_feed(project_id,publication_identity) WHERE publication_identity IS NOT NULL"
        )
        connection.execute("COMMIT")
    except Exception:
        _rollback(connection)
        raise


def _migrate_external_agent_publication_inbox(connection: sqlite3.Connection) -> None:
    """Migrate the early global publication identity inbox to project scope."""
    columns = tuple(connection.execute("PRAGMA table_info(ai_external_agent_publication_inbox)"))
    primary_key = {str(row[1]): int(row[5]) for row in columns if int(row[5]) > 0}
    if primary_key == {"project_id": 1, "publication_identity": 2}:
        return
    if primary_key != {"publication_identity": 1}:
        raise RuntimeError("unsupported external agent publication inbox schema")
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("ALTER TABLE ai_external_agent_publication_inbox RENAME TO ai_external_agent_publication_inbox_legacy")
        connection.execute(
            "CREATE TABLE ai_external_agent_publication_inbox("
            "project_id TEXT NOT NULL,publication_identity TEXT NOT NULL,change_type TEXT NOT NULL,"
            "object_ref TEXT NOT NULL,object_revision TEXT NOT NULL,occurred_at TEXT NOT NULL,"
            "change_cursor INTEGER NOT NULL,PRIMARY KEY(project_id,publication_identity))"
        )
        connection.execute(
            "INSERT INTO ai_external_agent_publication_inbox("
            "project_id,publication_identity,change_type,object_ref,object_revision,occurred_at,change_cursor) "
            "SELECT project_id,publication_identity,change_type,object_ref,object_revision,occurred_at,change_cursor "
            "FROM ai_external_agent_publication_inbox_legacy"
        )
        connection.execute("DROP TABLE ai_external_agent_publication_inbox_legacy")
        connection.execute("COMMIT")
    except Exception:
        _rollback(connection)
        raise


def _migrate_external_agent_sessions(connection: sqlite3.Connection) -> None:
    """Add restart-safe delivery cursors without changing existing maps."""
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(ai_external_agent_sessions)")}
    if {"delivered_cursor", "acknowledged_cursor"}.issubset(columns):
        return
    if not columns:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        legacy = tuple(connection.execute(
            "SELECT session_id,session_json FROM ai_external_agent_sessions"
        ))
        if "delivered_cursor" not in columns:
            connection.execute(
                "ALTER TABLE ai_external_agent_sessions "
                "ADD COLUMN delivered_cursor INTEGER NOT NULL DEFAULT 0"
            )
        if "acknowledged_cursor" not in columns:
            connection.execute(
                "ALTER TABLE ai_external_agent_sessions "
                "ADD COLUMN acknowledged_cursor INTEGER NOT NULL DEFAULT 0"
            )
        for session_id, encoded in legacy:
            try:
                event_cursor = _mapping_json(encoded).get("event_cursor")
            except (TypeError, ValueError):
                event_cursor = 0
            if not isinstance(event_cursor, int) or isinstance(event_cursor, bool) or event_cursor < 0:
                event_cursor = 0
            connection.execute(
                "UPDATE ai_external_agent_sessions SET delivered_cursor=?,acknowledged_cursor=? "
                "WHERE session_id=?",
                (event_cursor, event_cursor, session_id),
            )
        connection.execute("COMMIT")
    except Exception:
        _rollback(connection)
        raise


def _migrate_external_agent_session_retention_fields(connection: sqlite3.Connection) -> None:
    """Upgrade short-lived Bridge maps to the explicit feed-retention contract."""
    rows = tuple(connection.execute(
        "SELECT session_id,project_id,session_json FROM ai_external_agent_sessions"
    ))
    updates: list[tuple[str, str]] = []
    for session_id, project_id, encoded in rows:
        try:
            payload = dict(_mapping_json(encoded))
        except (TypeError, ValueError):
            continue
        if {"retained_after_cursor", "earliest_available_cursor"}.issubset(payload):
            continue
        retained_after = _project_event_retained_after_cursor(connection, str(project_id))
        payload["retained_after_cursor"] = retained_after
        payload["earliest_available_cursor"] = retained_after + 1
        updates.append((_encode(payload), str(session_id)))
    if not updates:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.executemany(
            "UPDATE ai_external_agent_sessions SET session_json=? WHERE session_id=?", updates,
        )
        connection.execute("COMMIT")
    except Exception:
        _rollback(connection)
        raise


def _migrate_run_lease_columns(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(ai_turn_run_leases)")}
        column_types = {
            "status": "TEXT NULL",
            "acquired_at": "TEXT NULL",
            "heartbeat_at": "TEXT NULL",
            "stale_after": "TEXT NULL",
            "recovery_attempts": "INTEGER NULL",
            "recovery_attempted_at": "TEXT NULL",
        }
        for name, column_type in column_types.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE ai_turn_run_leases ADD COLUMN {name} {column_type}")
        # Development builds before the shared-lock migration used a separate
        # strict table.  Move it only when no incompatible dual authority exists.
        if _table_exists(connection, "ai_turn_strict_run_leases"):
            conflict = connection.execute(
                "SELECT 1 FROM ai_turn_strict_run_leases strict "
                "JOIN ai_turn_run_leases legacy ON legacy.turn_id=strict.turn_id LIMIT 1"
            ).fetchone()
            if conflict is not None:
                raise RuntimeError("incompatible historical AI Turn lease authorities")
            connection.execute(
                "INSERT INTO ai_turn_run_leases(turn_id, owner_id, generation, status, acquired_at, heartbeat_at, stale_after) "
                "SELECT turn_id, owner_id, generation, status, acquired_at, heartbeat_at, stale_after FROM ai_turn_strict_run_leases"
            )
            connection.execute("DROP TABLE ai_turn_strict_run_leases")
        if _table_exists(connection, "ai_turn_strict_run_generations"):
            connection.execute(
                "INSERT INTO ai_turn_run_generations(turn_id, generation) "
                "SELECT turn_id, generation FROM ai_turn_strict_run_generations "
                "ON CONFLICT(turn_id) DO UPDATE SET generation=MAX(ai_turn_run_generations.generation, excluded.generation)"
            )
            connection.execute("DROP TABLE ai_turn_strict_run_generations")
        connection.execute("COMMIT")
    except Exception:
        _rollback(connection)
        raise


def _migrate_recovery_columns(connection: sqlite3.Connection) -> None:
    """Complete databases initialized by short-lived pre-release builds."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        audit_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(ai_turn_recovery_audit)")}
        audit_additions = {
            "old_owner_id": "TEXT NOT NULL DEFAULT ''",
            "old_acquired_at": "TEXT NOT NULL DEFAULT ''",
            "old_heartbeat_at": "TEXT NOT NULL DEFAULT ''",
            "old_stale_after": "TEXT NOT NULL DEFAULT ''",
            "classifier_version": "TEXT NOT NULL DEFAULT 'ai-recovery-v1'",
            "scanner_actor": "TEXT NOT NULL DEFAULT 'startup-scanner'",
        }
        for name, column_type in audit_additions.items():
            if name not in audit_columns:
                connection.execute(f"ALTER TABLE ai_turn_recovery_audit ADD COLUMN {name} {column_type}")
        queue_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(ai_turn_recovery_queue)")}
        queue_additions = {
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, column_type in queue_additions.items():
            if name not in queue_columns:
                connection.execute(f"ALTER TABLE ai_turn_recovery_queue ADD COLUMN {name} {column_type}")
        queue_audit_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(ai_turn_recovery_queue_audit)")}
        for name, column_type in {"owner_id": "TEXT NOT NULL DEFAULT ''", "new_generation": "INTEGER NOT NULL DEFAULT 0"}.items():
            if name not in queue_audit_columns:
                connection.execute(f"ALTER TABLE ai_turn_recovery_queue_audit ADD COLUMN {name} {column_type}")
        review_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(ai_turn_recovery_reviews)")}
        review_additions = {
            "project_id": "TEXT NULL", "last_sequence": "INTEGER NOT NULL DEFAULT 0",
            "last_event_id": "TEXT NOT NULL DEFAULT ''", "last_event_type": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'quarantined'", "revision": "INTEGER NOT NULL DEFAULT 1",
            "created_at": "TEXT NOT NULL DEFAULT ''", "updated_at": "TEXT NOT NULL DEFAULT ''",
        }
        for name, column_type in review_additions.items():
            if name not in review_columns:
                connection.execute(f"ALTER TABLE ai_turn_recovery_reviews ADD COLUMN {name} {column_type}")
        connection.execute("COMMIT")
    except Exception:
        _rollback(connection)
        raise


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone() is not None


def _model_provider_cursor(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"response_id", "sequence_number"}:
        raise ValueError("model provider cursor fields are invalid")
    identity, sequence = value["response_id"], value["sequence_number"]
    if (
        type(identity) is not str
        or re.fullmatch(r"resp_[A-Za-z0-9_-]{1,200}", identity) is None
        or type(sequence) is not int
        or not 0 <= sequence <= 9_007_199_254_740_991
    ):
        raise ValueError("model provider cursor identity or sequence is invalid")
    return {"response_id": identity, "sequence_number": sequence}


def _encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _immutable_payload_identity(turn_id: str, kind: str) -> None:
    if not isinstance(turn_id, str) or not turn_id.strip():
        raise ValueError("immutable payload turn identity is invalid")
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("immutable payload kind is invalid")


def _payload_ref(turn_id: str, kind: str) -> str:
    return f"crp://session/{turn_id}/{kind}/{uuid4().hex}"


def _immutable_payload_ref(turn_id: str, kind: str) -> str:
    """Stable pre-admission locator for one immutable Turn payload."""
    return f"crp://session/{turn_id}/{kind}"


def _expert_job_ref(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"crp://jobs/[a-z][a-z0-9-]{2,127}", value))


def _event_with_payload_ref(event: Mapping[str, object], payload_ref: str) -> dict[str, object]:
    payload = dict(event)
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("turn event data is invalid")
    copied_data = dict(data)
    if copied_data.get("payload_ref") not in {None, payload_ref}:
        raise ValueError("turn event already has a payload ref")
    copied_data["payload_ref"] = payload_ref
    payload["data"] = copied_data
    return payload


def _event_with_receipt_ref(event: Mapping[str, object], receipt_ref: str) -> dict[str, object]:
    payload = dict(event)
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("turn event data is invalid")
    copied_data = dict(data)
    if copied_data.get("receipt_ref") not in {None, receipt_ref}:
        raise ValueError("turn event already has a receipt ref")
    copied_data["receipt_ref"] = receipt_ref
    payload["data"] = copied_data
    return payload


def _model_terminal_event_with_receipts(
    event: Mapping[str, object],
    receipt_ref: str | None,
    evidence_refs: tuple[str | None, str | None],
) -> dict[str, object]:
    """Attach only refs allocated by the enclosing SQLite transaction."""
    payload = dict(event)
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("turn event data is invalid")
    copied = dict(data)
    if copied.get("receipt_ref") is not None:
        raise ValueError("model terminal event already has a receipt ref")
    if receipt_ref is not None:
        copied["receipt_ref"] = receipt_ref
    supplied = copied.get("evidence_refs")
    if not isinstance(supplied, list) or any(not isinstance(item, str) for item in supplied):
        raise ValueError("model terminal event evidence refs are invalid")
    copied["evidence_refs"] = [*supplied, *(ref for ref in evidence_refs if ref is not None)]
    payload["data"] = copied
    return payload


def _model_attempt_terminal_event_with_receipt(
    event: Mapping[str, object],
    receipt_ref: str,
    dispatch_payload_ref: str,
) -> dict[str, object]:
    """Attach transaction-owned terminal references without accepting a replacement."""
    payload = dict(event)
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("model attempt terminal Event data is invalid")
    copied = dict(data)
    if copied.get("receipt_ref") is not None:
        raise ValueError("model attempt terminal Event already has a receipt ref")
    supplied = copied.get("evidence_refs")
    if not isinstance(supplied, list) or any(not isinstance(item, str) for item in supplied):
        raise ValueError("model attempt terminal Event evidence refs are invalid")
    if any(item != dispatch_payload_ref for item in supplied):
        raise ValueError("model attempt terminal Event evidence identity drifted")
    copied["receipt_ref"] = receipt_ref
    copied["evidence_refs"] = [dispatch_payload_ref]
    payload["data"] = copied
    return payload


def _model_effect_lease_expiry(
    connection: sqlite3.Connection,
    token: RunLeaseToken | None,
    *,
    fallback: int | float,
) -> float:
    if token is None:
        return float(fallback)
    row = connection.execute(
        "SELECT stale_after FROM ai_turn_run_leases "
        "WHERE turn_id=? AND owner_id=? AND generation=? AND status='active'",
        (token.turn_id, token.owner_id, token.generation),
    ).fetchone()
    if row is None:
        raise RunLeaseRevoked()
    return max(float(fallback), _lease_datetime(row[0]).timestamp())


def _model_attempt_identity_matches(
    dispatch: Mapping[str, object],
    receipt: Mapping[str, object],
) -> bool:
    return all(
        dispatch.get(field) == receipt.get(field)
        for field in (
            "attempt_id", "turn_id", "model_request_id", "attempt_number",
            "routing_snapshot_revision", "provider_id", "model_id", "execution_location",
        )
    )


def _event_with_evidence_ref(event: Mapping[str, object], evidence_ref: str) -> dict[str, object]:
    payload = dict(event)
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("turn event data is invalid")
    copied_data = dict(data)
    refs = copied_data.get("evidence_refs")
    if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)):
        raise ValueError("turn event evidence refs are invalid")
    copied_refs = list(refs)
    if evidence_ref not in copied_refs:
        copied_refs.append(evidence_ref)
    copied_data["evidence_refs"] = copied_refs
    payload["data"] = copied_data
    return payload


def _mapping_json(value: object) -> dict[str, object]:
    payload = json.loads(str(value))
    if not isinstance(payload, Mapping):
        raise ValueError("stored AI Turn payload must be an object")
    return dict(payload)


def _receipt_payload(receipt: TurnReceipt) -> dict[str, object]:
    return {"turn_id": receipt.turn_id, "session_id": receipt.session_id, "operation_id": receipt.operation_id, "status": receipt.status, "current_sequence": receipt.current_sequence, "replayed": receipt.replayed}


def _receipt(payload: Mapping[str, object]) -> TurnReceipt:
    return TurnReceipt(str(payload["turn_id"]), str(payload["session_id"]), str(payload["operation_id"]), str(payload["status"]), int(payload["current_sequence"]), bool(payload["replayed"]))  # type: ignore[arg-type]


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.execute("ROLLBACK")


def _lease_times(now: datetime, stale_after: datetime) -> None:
    validate_run_lease_time(now, name="run lease current time")
    validate_run_lease_time(stale_after, name="run lease stale time")
    if stale_after < now:
        raise ValueError("run lease stale time precedes current time")


def _lease_time(value: datetime) -> str:
    return validate_run_lease_time(value, name="run lease time").isoformat()


def _lease_datetime(value: object) -> datetime:
    return validate_run_lease_time(datetime.fromisoformat(str(value)), name="stored run lease time")


def _lease_record(token: RunLeaseToken, status: str, acquired_at: datetime, heartbeat_at: datetime, stale_after: datetime) -> RunLeaseRecord:
    return validate_run_lease_record(RunLeaseRecord(token, status, acquired_at, heartbeat_at, stale_after))


def _request_project_id(request: Mapping[str, object]) -> str | None:
    scope = request.get("scope")
    return scope.get("project_id") if isinstance(scope, Mapping) and isinstance(scope.get("project_id"), str) else None


def _required_project_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 160:
        raise ValueError("project identity is invalid")
    return value.strip()


_EXTERNAL_CHANGE_REF = re.compile(r"^crp://[A-Za-z0-9._~-]{1,64}/[A-Za-z0-9._~/-]{1,384}$")
_EXTERNAL_CHANGE_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:~-]{0,159}$")
_EXTERNAL_CHANGE_NAMESPACES = {
    "context.invalidated": "context",
    "document.published": "documents",
    "memory.published": "memory",
    "memory.invalidated": "memory",
    "project_skill.published": "skills",
    "project_skill.invalidated": "skills",
    "memory.proposed": "proposals",
}
_EXTERNAL_PUBLICATION_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")


def _project_event_row(row: Sequence[object]) -> dict[str, object]:
    return {
        "cursor": int(row[0]), "change_type": str(row[1]),
        "object_ref": str(row[2]), "object_revision": str(row[3]),
        "occurred_at": str(row[4]),
    }


def _project_event_retained_after_cursor(connection: sqlite3.Connection, project_id: str) -> int:
    row = connection.execute(
        "SELECT retained_after_cursor FROM ai_project_event_feed_retention WHERE project_id=?",
        (project_id,),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _project_event_head_cursor(connection: sqlite3.Connection, project_id: str) -> int:
    row = connection.execute(
        "SELECT MAX(cursor) FROM ai_project_event_feed WHERE project_id=?", (project_id,),
    ).fetchone()
    feed_head = int(row[0]) if row and row[0] is not None else 0
    return max(feed_head, _project_event_retained_after_cursor(connection, project_id))


def _external_agent_publication_projection(event: Mapping[str, object]) -> dict[str, str]:
    expected = {
        "publication_identity", "project_id", "change_type", "object_ref",
        "object_revision", "occurred_at",
    }
    if set(event) != expected:
        raise ValueError("external agent publication event schema is invalid")
    validate_external_agent_safe_projection(event)
    identity = event.get("publication_identity")
    if not isinstance(identity, str) or not _EXTERNAL_PUBLICATION_IDENTITY.fullmatch(identity):
        raise ValueError("external agent publication identity is invalid")
    project_id = _required_project_id(event.get("project_id"))
    change = _external_agent_change_projection(
        project_id=project_id, change_type=event.get("change_type"),
        object_ref=event.get("object_ref"), object_revision=event.get("object_revision"),
        occurred_at=event.get("occurred_at"),
    )
    return {"publication_identity": identity, "project_id": project_id, **change}


def _public_publication_change(cursor: int, projected: Mapping[str, str]) -> dict[str, object]:
    """Keep publisher identity in the durable inbox, never in Bridge client data."""
    return {
        "cursor": cursor, "project_id": projected["project_id"],
        "change_type": projected["change_type"], "object_ref": projected["object_ref"],
        "object_revision": projected["object_revision"], "occurred_at": projected["occurred_at"],
    }


def _external_agent_proposal_effect_intent(
    *, operation_id: str, session_id: str, project_id: str,
    request: Mapping[str, object],
) -> EffectIntent:
    admission_id = request.get("admission_id")
    context_revision = request.get("context_manifest_revision")
    policy_revision = request.get("admission_policy_revision")
    if not all(isinstance(value, (str, int)) and str(value) for value in (
        admission_id, context_revision, policy_revision,
    )):
        raise ValueError("external agent proposal effect revisions are invalid")
    return EffectIntent(
        session_id=session_id,
        turn_id=None,
        root_id=session_id,
        parent_id=None,
        step_key=f"memory-propose:{operation_id}",
        kind="memory_propose",
        effect_class=EffectClass.QUERYABLE,
        purpose=EffectPurpose.PRIMARY,
        intent_ref=f"ai-external-agent-proposal:{operation_id}",
        gate_decision_id=str(admission_id),
        rev_set={
            "project": project_id,
            "context_manifest": str(context_revision),
            "policy": str(policy_revision),
        },
        payload=dict(request),
        idem_key=operation_id,
        operation_id_override=operation_id,
    )


def _model_wire_attempt_effect_intent(
    connection: sqlite3.Connection, *, dispatch: Mapping[str, object], dispatch_ref: str,
) -> EffectIntent:
    turn_id = str(dispatch["turn_id"])
    turn = connection.execute(
        "SELECT session_id,operation_id FROM ai_turns WHERE turn_id=?", (turn_id,),
    ).fetchone()
    if turn is None:
        raise TurnStateConflict("model attempt Turn was not found")
    route_revision = str(dispatch["routing_snapshot_revision"])
    attempt_id = str(dispatch["attempt_id"])
    return EffectIntent(
        session_id=str(turn[0]),
        turn_id=turn_id,
        root_id=str(turn[1]),
        parent_id=str(turn[1]),
        step_key=(
            f"model-wire:{dispatch['model_request_id']}:{dispatch['attempt_number']}"
        ),
        kind="model_call",
        effect_class=EffectClass.AT_MOST_ONCE,
        purpose=EffectPurpose.PRIMARY,
        intent_ref=dispatch_ref,
        gate_decision_id=f"frozen-route:{route_revision}",
        rev_set={
            "routing_snapshot": route_revision,
            "provider": str(dispatch["provider_id"]),
            "model": str(dispatch["model_id"]),
        },
        payload=dict(dispatch),
        idem_key=attempt_id,
        operation_id_override=attempt_id,
    )


def _mcp_effect_identity(value: Mapping[str, object]) -> tuple[str, str]:
    turn_id = value.get("turn_id")
    invocation_id = value.get("invocation_id")
    if not isinstance(turn_id, str) or not turn_id.strip():
        raise ValueError("MCP effect Turn identity is invalid")
    if not isinstance(invocation_id, str) or not invocation_id.strip():
        raise ValueError("MCP effect invocation identity is invalid")
    return turn_id, invocation_id


def _mcp_effect_operation_id(invocation_id: str) -> str:
    return f"mcp-effect-{invocation_id}"


def _mcp_side_effect_intent(
    connection: sqlite3.Connection, *, intent: Mapping[str, object], intent_ref: str,
) -> EffectIntent:
    turn_id, invocation_id = _mcp_effect_identity(intent)
    turn = connection.execute(
        "SELECT session_id,operation_id FROM ai_turns WHERE turn_id=?", (turn_id,),
    ).fetchone()
    if turn is None:
        raise TurnStateConflict("MCP side-effect Turn was not found")
    operation_id = intent.get("operation_id")
    if not isinstance(operation_id, str) or operation_id != str(turn[1]):
        raise TurnStateConflict("MCP side-effect root operation drifted")
    revisions = {
        key: intent.get(key)
        for key in ("server_id", "protocol_version", "tool_id", "tool_name")
    }
    if not all(value is not None and str(value) for value in revisions.values()):
        raise ValueError("MCP side-effect revisions are invalid")
    return EffectIntent(
        session_id=str(turn[0]), turn_id=turn_id,
        root_id=operation_id, parent_id=invocation_id,
        step_key=f"mcp:{intent['tool_id']}:{invocation_id}", kind="mcp_call",
        effect_class=EffectClass.QUERYABLE, purpose=EffectPurpose.PRIMARY,
        intent_ref=intent_ref,
        gate_decision_id=(
            f"mcp-frozen:{intent['server_id']}:{intent['protocol_version']}"
        ),
        rev_set=revisions, payload=dict(intent),
        idem_key=str(intent.get("idempotency_key")),
        operation_id_override=_mcp_effect_operation_id(invocation_id),
    )


def _effect_time(value: str) -> int:
    try:
        observed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("external agent proposal effect time is invalid") from error
    if observed.tzinfo is None:
        raise ValueError("external agent proposal effect time is invalid")
    return int(observed.astimezone(timezone.utc).timestamp())


def _external_agent_proposal_operation(
    row: Sequence[object], *, operation_id: str, replayed: bool,
) -> dict[str, object]:
    result = _mapping_json(row[6]) if row[6] is not None else None
    return {
        "operation_id": operation_id,
        "session_id": str(row[0]),
        "project_id": str(row[1]),
        "status": str(row[3]),
        "proposal_ref": str(row[4]) if row[4] is not None else None,
        "proposal_revision": str(row[5]) if row[5] is not None else None,
        "result": result,
        "change_cursor": int(row[7]) if row[7] is not None else None,
        "replayed": replayed,
    }


def _external_agent_change_projection(
    *, project_id: str, change_type: object, object_ref: object,
    object_revision: object, occurred_at: object,
) -> dict[str, str]:
    if change_type not in _EXTERNAL_CHANGE_NAMESPACES:
        raise ValueError("external agent change type is invalid")
    expected_prefix = f"crp://{_EXTERNAL_CHANGE_NAMESPACES[change_type]}/{project_id}/"
    if (
        not isinstance(object_ref, str)
        or not _EXTERNAL_CHANGE_REF.fullmatch(object_ref)
        or not object_ref.startswith(expected_prefix)
        or len(object_ref) == len(expected_prefix)
    ):
        raise ValueError("external agent change ref is invalid")
    if not isinstance(object_revision, str) or not _EXTERNAL_CHANGE_REVISION.fullmatch(object_revision):
        raise ValueError("external agent change revision is invalid")
    if not isinstance(occurred_at, str):
        raise ValueError("external agent change time is invalid")
    try:
        observed_at = datetime.fromisoformat(occurred_at)
    except ValueError as error:
        raise ValueError("external agent change time is invalid") from error
    if observed_at.tzinfo is None:
        raise ValueError("external agent change time is invalid")
    return {
        "change_type": change_type,
        "object_ref": object_ref,
        "object_revision": object_revision,
        "occurred_at": observed_at.isoformat(),
    }


def _latest_event_identity(row: sqlite3.Row | tuple[object, ...] | None) -> tuple[int, str, str]:
    if row is None:
        return (0, "", "")
    return (int(row[0]), str(row[1]), str(_mapping_json(row[2]).get("type", "")))


def _public_review_row(row: tuple[object, ...]) -> PublicRecoveryReview:
    return validate_public_recovery_review(PublicRecoveryReview(
        review_id=str(row[0]), project_id=str(row[1]) if row[1] is not None else None,
        status=str(row[2]), revision=int(row[3]), reason_code=str(row[4]),
        created_at=_lease_datetime(row[5]), updated_at=_lease_datetime(row[6]),
    ))


def _manual_review_terminal_status(result_status: str) -> str:
    statuses = {
        "completed": "turn_completed",
        "failed": "turn_failed",
        "cancelled": "turn_cancelled",
        "waiting_approval": "waiting_approval",
    }
    if result_status not in statuses:
        raise ValueError("recovery completion result status is invalid")
    return statuses[result_status]
