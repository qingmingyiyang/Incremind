from __future__ import annotations

import re
import sqlite3
from core.storage_provider.observability import observe_connection
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_SCHEMA_VERSION = 2
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_SAFE_MIGRATION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_TARGET_IDENTITY = re.compile(r"^sqlite:[A-Za-z0-9][A-Za-z0-9._:-]{0,239}$")
_STATES = {"json_active", "sqlite_staged", "sqlite_active", "rollback_required"}
_TRANSITIONS = {
    "json_active": {"sqlite_staged"},
    "sqlite_staged": {"sqlite_active", "json_active"},
    "sqlite_active": {"rollback_required"},
    "rollback_required": {"json_active"},
}


class AggregateAuthorityError(ValueError):
    """Raised when aggregate authority state or evidence is invalid."""


class AggregateAuthorityConflict(AggregateAuthorityError):
    """Raised for stale revisions or duplicate authority records."""


@dataclass(frozen=True, slots=True)
class AggregateAuthorityEvidence:
    migration_id: str
    source_fingerprint: str | None
    target_fingerprint: str | None
    target_identity: str
    verification_method: str = "fingerprint"
    verified_record_count: int | None = None


@dataclass(frozen=True, slots=True)
class AggregateAuthorityRecord:
    namespace_id: str
    aggregate: str
    state: str
    revision: int
    evidence: AggregateAuthorityEvidence | None
    reason: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AggregateAuthorityTransition:
    namespace_id: str
    aggregate: str
    expected_revision: int
    to_state: str
    reason: str
    evidence: AggregateAuthorityEvidence | None = None
    rollback_verified: bool = False


class SQLiteAggregateAuthorityStore:
    """Explicit-path authority state machine, not wired into runtime factories."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().resolve(strict=False)

    def create_json_active(
        self,
        *,
        namespace_id: str,
        aggregate: str,
        reason: str,
        now: str | None = None,
    ) -> AggregateAuthorityRecord:
        _require_segment("namespace_id", namespace_id)
        _require_segment("aggregate", aggregate)
        _require_reason(reason)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if _select(connection, namespace_id, aggregate) is not None:
                raise AggregateAuthorityConflict("aggregate authority already exists")
            timestamp = now or _utc_now()
            connection.execute(
                """
                INSERT INTO aggregate_authority (
                    namespace_id, aggregate, state, revision,
                    migration_id, source_fingerprint, target_fingerprint, target_identity,
                    verification_method, verified_record_count,
                    reason, created_at, updated_at
                ) VALUES (?, ?, 'json_active', 1, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, ?)
                """,
                (namespace_id, aggregate, reason, timestamp, timestamp),
            )
            row = _select(connection, namespace_id, aggregate)
            connection.execute("COMMIT")
            if row is None:  # pragma: no cover
                raise AggregateAuthorityError("aggregate authority was not persisted")
            return _record(row)
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def get(self, namespace_id: str, aggregate: str) -> AggregateAuthorityRecord | None:
        _require_segment("namespace_id", namespace_id)
        _require_segment("aggregate", aggregate)
        connection = self._connect()
        try:
            row = _select(connection, namespace_id, aggregate)
            return _record(row) if row is not None else None
        finally:
            connection.close()

    def list_records(self) -> tuple[AggregateAuthorityRecord, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM aggregate_authority
                ORDER BY namespace_id ASC, aggregate ASC
                """
            ).fetchall()
            return tuple(_record(row) for row in rows)
        finally:
            connection.close()

    def transition(
        self,
        *,
        namespace_id: str,
        aggregate: str,
        expected_revision: int,
        to_state: str,
        reason: str,
        evidence: AggregateAuthorityEvidence | None = None,
        rollback_verified: bool = False,
        now: str | None = None,
    ) -> AggregateAuthorityRecord:
        _require_segment("namespace_id", namespace_id)
        _require_segment("aggregate", aggregate)
        _require_revision(expected_revision)
        _require_state(to_state)
        _require_reason(reason)
        if not isinstance(rollback_verified, bool):
            raise AggregateAuthorityError("rollback_verified must be boolean")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _select(connection, namespace_id, aggregate)
            if row is None:
                raise AggregateAuthorityError("aggregate authority does not exist")
            current = _record(row)
            if current.revision != expected_revision:
                raise AggregateAuthorityConflict(
                    f"expected revision {expected_revision}, found {current.revision}"
                )
            if to_state not in _TRANSITIONS[current.state]:
                raise AggregateAuthorityError(
                    f"illegal aggregate authority transition {current.state} -> {to_state}"
                )
            next_evidence = _transition_evidence(current, to_state, evidence)
            _require_aggregate_evidence(aggregate, next_evidence)
            if current.state == "rollback_required" and to_state == "json_active":
                if rollback_verified is not True:
                    raise AggregateAuthorityError(
                        "rollback_required -> json_active requires rollback_verified"
                    )
            elif rollback_verified:
                raise AggregateAuthorityError(
                    "rollback_verified is only valid for rollback_required -> json_active"
                )
            timestamp = now or _utc_now()
            next_revision = current.revision + 1
            values = _evidence_values(next_evidence)
            connection.execute(
                """
                UPDATE aggregate_authority
                SET state = ?, revision = ?, migration_id = ?, source_fingerprint = ?,
                    target_fingerprint = ?, target_identity = ?, verification_method = ?,
                    verified_record_count = ?, reason = ?, updated_at = ?
                WHERE namespace_id = ? AND aggregate = ? AND revision = ?
                """,
                (
                    to_state,
                    next_revision,
                    *values,
                    reason,
                    timestamp,
                    namespace_id,
                    aggregate,
                    expected_revision,
                ),
            )
            updated = _select(connection, namespace_id, aggregate)
            connection.execute("COMMIT")
            if updated is None:  # pragma: no cover
                raise AggregateAuthorityError("aggregate authority transition was not persisted")
            return _record(updated)
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def transition_many(
        self,
        transitions: tuple[AggregateAuthorityTransition, ...],
        *,
        now: str | None = None,
    ) -> tuple[AggregateAuthorityRecord, ...]:
        """Apply multiple authority transitions in one SQLite transaction."""

        if not transitions:
            raise AggregateAuthorityError("authority transitions cannot be empty")
        keys = tuple((item.namespace_id, item.aggregate) for item in transitions)
        if len(set(keys)) != len(keys):
            raise AggregateAuthorityError("authority transitions must target unique aggregates")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            prepared = []
            timestamp = now or _utc_now()
            for item in transitions:
                _require_segment("namespace_id", item.namespace_id)
                _require_segment("aggregate", item.aggregate)
                _require_revision(item.expected_revision)
                _require_state(item.to_state)
                _require_reason(item.reason)
                if not isinstance(item.rollback_verified, bool):
                    raise AggregateAuthorityError("rollback_verified must be boolean")
                row = _select(connection, item.namespace_id, item.aggregate)
                if row is None:
                    raise AggregateAuthorityError("aggregate authority does not exist")
                current = _record(row)
                if current.revision != item.expected_revision:
                    raise AggregateAuthorityConflict(
                        f"expected revision {item.expected_revision}, found {current.revision}"
                    )
                if item.to_state not in _TRANSITIONS[current.state]:
                    raise AggregateAuthorityError(
                        f"illegal aggregate authority transition {current.state} -> {item.to_state}"
                    )
                next_evidence = _transition_evidence(current, item.to_state, item.evidence)
                _require_aggregate_evidence(item.aggregate, next_evidence)
                if current.state == "rollback_required" and item.to_state == "json_active":
                    if item.rollback_verified is not True:
                        raise AggregateAuthorityError(
                            "rollback_required -> json_active requires rollback_verified"
                        )
                elif item.rollback_verified:
                    raise AggregateAuthorityError(
                        "rollback_verified is only valid for rollback_required -> json_active"
                    )
                prepared.append((item, current, next_evidence))
            for item, current, next_evidence in prepared:
                values = _evidence_values(next_evidence)
                cursor = connection.execute(
                    """
                    UPDATE aggregate_authority
                    SET state = ?, revision = ?, migration_id = ?, source_fingerprint = ?,
                        target_fingerprint = ?, target_identity = ?, verification_method = ?,
                        verified_record_count = ?, reason = ?, updated_at = ?
                    WHERE namespace_id = ? AND aggregate = ? AND revision = ?
                    """,
                    (
                        item.to_state,
                        current.revision + 1,
                        *values,
                        item.reason,
                        timestamp,
                        item.namespace_id,
                        item.aggregate,
                        item.expected_revision,
                    ),
                )
                if cursor.rowcount != 1:  # pragma: no cover - transaction lock prevents drift
                    raise AggregateAuthorityConflict("aggregate authority changed during transition")
            updated = tuple(
                _record(_select(connection, item.namespace_id, item.aggregate))
                for item in transitions
            )
            connection.execute("COMMIT")
            return updated
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def journal_mode(self) -> str:
        connection = self._connect()
        try:
            row = connection.execute("PRAGMA journal_mode").fetchone()
            return str(row[0]).lower() if row is not None else ""
        finally:
            connection.close()

    def synchronous_mode(self) -> str:
        connection = self._connect()
        try:
            row = connection.execute("PRAGMA synchronous").fetchone()
            modes = {0: "off", 1: "normal", 2: "full", 3: "extra"}
            return modes.get(row[0], "") if row is not None else ""
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database_path)
        observe_connection(connection)
        connection.row_factory = sqlite3.Row
        try:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                raise AggregateAuthorityError("aggregate authority requires WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=5000")
            _initialize(connection)
            return connection
        except Exception:
            connection.close()
            raise


def _initialize(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS aggregate_authority_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS aggregate_authority (
            namespace_id TEXT NOT NULL,
            aggregate TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN ('json_active', 'sqlite_staged', 'sqlite_active', 'rollback_required')
            ),
            revision INTEGER NOT NULL CHECK (revision > 0),
            migration_id TEXT,
            source_fingerprint TEXT,
            target_fingerprint TEXT,
            target_identity TEXT,
            verification_method TEXT,
            verified_record_count INTEGER,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (namespace_id, aggregate)
        )
        """
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(aggregate_authority)")}
    if "verification_method" not in columns:
        connection.execute("ALTER TABLE aggregate_authority ADD COLUMN verification_method TEXT")
    if "verified_record_count" not in columns:
        connection.execute("ALTER TABLE aggregate_authority ADD COLUMN verified_record_count INTEGER")
    connection.execute(
        """
        INSERT OR IGNORE INTO aggregate_authority_meta (key, value)
        VALUES ('schema_version', ?)
        """,
        (str(_SCHEMA_VERSION),),
    )
    connection.execute(
        "UPDATE aggregate_authority_meta SET value = ? WHERE key = 'schema_version'",
        (str(_SCHEMA_VERSION),),
    )
    connection.commit()


def _select(connection, namespace_id: str, aggregate: str):
    return connection.execute(
        """
        SELECT * FROM aggregate_authority
        WHERE namespace_id = ? AND aggregate = ?
        """,
        (namespace_id, aggregate),
    ).fetchone()


def _record(row: sqlite3.Row) -> AggregateAuthorityRecord:
    _require_segment("namespace_id", row["namespace_id"])
    _require_segment("aggregate", row["aggregate"])
    _require_reason(row["reason"])
    if not isinstance(row["created_at"], str) or not isinstance(row["updated_at"], str):
        raise AggregateAuthorityError("aggregate authority timestamps are invalid")
    state = row["state"]
    _require_state(state)
    evidence_values = (
        row["migration_id"],
        row["source_fingerprint"],
        row["target_fingerprint"],
        row["target_identity"],
        row["verification_method"],
        row["verified_record_count"],
    )
    if all(value is None for value in evidence_values):
        evidence = None
    elif isinstance(row["migration_id"], str) and isinstance(row["target_identity"], str):
        evidence = AggregateAuthorityEvidence(
            migration_id=row["migration_id"],
            source_fingerprint=row["source_fingerprint"],
            target_fingerprint=row["target_fingerprint"],
            target_identity=row["target_identity"],
            verification_method=row["verification_method"] or "fingerprint",
            verified_record_count=row["verified_record_count"],
        )
        _require_evidence(evidence)
        _require_aggregate_evidence(row["aggregate"], evidence)
    else:
        raise AggregateAuthorityError("aggregate authority evidence is incomplete")
    revision = row["revision"]
    _require_revision(revision)
    if state != "json_active" and evidence is None:
        raise AggregateAuthorityError("non-JSON authority state requires evidence")
    return AggregateAuthorityRecord(
        namespace_id=row["namespace_id"],
        aggregate=row["aggregate"],
        state=state,
        revision=revision,
        evidence=evidence,
        reason=row["reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _transition_evidence(current, to_state, evidence):
    if current.state == "json_active" and to_state == "sqlite_staged":
        if evidence is None:
            raise AggregateAuthorityError("sqlite_staged requires migration evidence")
        _require_evidence(evidence)
        return evidence
    if evidence is not None:
        _require_evidence(evidence)
        if current.evidence is not None and evidence != current.evidence:
            raise AggregateAuthorityError("aggregate authority evidence cannot drift")
    return current.evidence


def _evidence_values(evidence):
    if evidence is None:
        return None, None, None, None, None, None
    return (
        evidence.migration_id,
        evidence.source_fingerprint,
        evidence.target_fingerprint,
        evidence.target_identity,
        evidence.verification_method,
        evidence.verified_record_count,
    )


def _require_evidence(evidence: AggregateAuthorityEvidence) -> None:
    if not _SAFE_MIGRATION_ID.fullmatch(evidence.migration_id):
        raise AggregateAuthorityError("migration_id is invalid")
    if evidence.verification_method == "fingerprint":
        if not isinstance(evidence.source_fingerprint, str) or not _FINGERPRINT.fullmatch(evidence.source_fingerprint):
            raise AggregateAuthorityError("source_fingerprint is invalid")
        if not isinstance(evidence.target_fingerprint, str) or not _FINGERPRINT.fullmatch(evidence.target_fingerprint):
            raise AggregateAuthorityError("target_fingerprint is invalid")
        if evidence.verified_record_count is not None:
            raise AggregateAuthorityError("fingerprint evidence cannot carry a record count")
    elif evidence.verification_method == "exact_records":
        if evidence.source_fingerprint is not None or evidence.target_fingerprint is not None:
            raise AggregateAuthorityError("exact-record evidence cannot carry fingerprints")
        if (not isinstance(evidence.verified_record_count, int)
                or isinstance(evidence.verified_record_count, bool)
                or evidence.verified_record_count < 0):
            raise AggregateAuthorityError("verified_record_count must be nonnegative")
    else:
        raise AggregateAuthorityError("verification_method is invalid")
    if not isinstance(evidence.target_identity, str) or not _TARGET_IDENTITY.fullmatch(
        evidence.target_identity
    ):
        raise AggregateAuthorityError("target_identity must be an opaque sqlite identity")


def _require_aggregate_evidence(aggregate: str, evidence: AggregateAuthorityEvidence | None) -> None:
    if evidence is not None and evidence.verification_method == "exact_records" and aggregate != "documents":
        raise AggregateAuthorityError("exact-record evidence is only supported for documents")


def _require_state(state: str) -> None:
    if state not in _STATES:
        raise AggregateAuthorityError("aggregate authority state is invalid")


def _require_segment(label: str, value: str) -> None:
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise AggregateAuthorityError(f"{label} is invalid")


def _require_revision(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AggregateAuthorityError("expected revision must be a positive integer")


def _require_reason(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise AggregateAuthorityError("reason is invalid")


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.execute("ROLLBACK")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
