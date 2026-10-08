from __future__ import annotations

from core.storage_provider.connection_scope import reusable_connection

import hashlib
import json
import re
import sqlite3
from core.storage_provider.observability import observe_connection, timed_stage
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


_SCHEMA_VERSION = 1
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_PROJECTABLE_FIELDS = frozenset({'id', 'project_id', 'title', 'type', 'status',
    'state', 'kind', 'revision', 'source_refs', 'document_id', 'source_id', 'created_at', 'updated_at',
    'receipt.do.state', 'receipt.do.document_id', 'receipt.do.kernel_turn_id',
    'inputs.project_id', 'inputs.turn_id', 'result.document_id'})


class SQLiteUnitOfWorkError(ValueError):
    """Raised when the explicit SQLite Unit of Work contract is violated."""


class SQLiteUnitOfWorkConflict(SQLiteUnitOfWorkError):
    """Raised when a record revision differs from the caller's expectation."""


@dataclass(frozen=True, slots=True)
class SQLiteStructuredRecord:
    """One structured record as observed through the UoW boundary."""

    collection: str
    object_id: str
    payload: Mapping[str, object]
    revision: int


class SQLiteStructuredRecordStore:
    """Explicit-path SQLite record store for future aggregate adapters.

    This module is deliberately not wired into application composition or the
    legacy JSON repositories. Callers must use a short-lived Unit of Work and
    provide an expected revision for every mutation.
    """

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().resolve(strict=False)

    @property
    def database_path(self) -> Path:
        return self._database_path

    def begin(self) -> SQLiteStructuredRecordUnitOfWork:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            return SQLiteStructuredRecordUnitOfWork(connection)
        except Exception:
            connection.close()
            raise

    @timed_stage("read_records")
    def read(self, collection: str, object_id: str) -> SQLiteStructuredRecord | None:
        _require_segment("collection", collection)
        _require_segment("object_id", object_id)
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT collection, object_id, payload_json, revision
                FROM crp_structured_records
                WHERE collection = ? AND object_id = ?
                """,
                (collection, object_id),
            ).fetchone()
            return _record_from_row(row) if row is not None else None
        finally:
            connection.close()

    def list(self, collection: str) -> tuple[SQLiteStructuredRecord, ...]:
        return self.list_matching(collection)

    @timed_stage('read_records')
    def list_projected(self, collection: str, *, fields: tuple[str, ...], **matching: str):
        """Read whitelisted metadata fields through the existing read lease.

        Body fields and SQL fragments are never accepted. This is detached
        content discovery; mutable authorization keeps its ordinary readers.
        """
        return self._projected(collection, fields=fields, matching=matching)

    @timed_stage('read_records')
    def read_projected(self, collection: str, object_id: str, *, fields: tuple[str, ...]):
        """Read fixed metadata for one bound identity, without its body."""
        _require_segment('object_id', object_id)
        rows = self._projected(collection, fields=fields, matching={}, object_id=object_id)
        return rows[0] if rows else None

    def _projected(self, collection, *, fields, matching, object_id=None):
        _require_segment('collection', collection)
        if not fields or any(field not in _PROJECTABLE_FIELDS for field in fields):
            raise SQLiteUnitOfWorkError('unsupported projected metadata field')
        expressions, parameters = [], []
        for field in fields:
            expressions.extend(('?', 'json_extract(payload_json, ?)'))
            parameters.extend((field, '$.' + field))
        clauses = ['collection = ?']
        parameters.append(collection)
        if object_id is not None:
            clauses.append('object_id = ?')
            parameters.append(object_id)
        for field, value in matching.items():
            if field not in _PROJECTABLE_FIELDS or not isinstance(value, str):
                raise SQLiteUnitOfWorkError('unsupported metadata predicate')
            clauses.append('json_extract(payload_json, ?) = ?')
            parameters.extend(('$.' + field, value))
        connection = self._connect()
        try:
            rows = connection.execute(
                'SELECT collection, object_id, json_object(' + ','.join(expressions)
                + ') AS payload_json, revision FROM crp_structured_records WHERE '
                + ' AND '.join(clauses) + ' ORDER BY object_id', parameters).fetchall()
            result = []
            for row in rows:
                record = _record_from_row(row)
                payload = {}
                for field, value in record.payload.items():
                    target = payload
                    parts = field.split('.')
                    for part in parts[:-1]:
                        target = target.setdefault(part, {})
                    target[parts[-1]] = value
                result.append(SQLiteStructuredRecord(record.collection, record.object_id, payload, record.revision))
            return tuple(result)
        finally:
            connection.close()

    @timed_stage("read_records")
    def read_batch(self, selections: Mapping[str, tuple[str, ...] | None]) -> dict[str, tuple[SQLiteStructuredRecord, ...]]:
        """Detach an explicit read set; ordinary reads and writes stay fresh.

        None selects a whole collection, an empty tuple selects nothing. Large
        ID sets use bounded statements on one short lease, never a transaction
        held across application work or a model call.
        """
        result = {collection: [] for collection in selections}
        clauses, parameters, queries = [], [], []
        for collection, identities in selections.items():
            _require_segment("collection", collection)
            if identities is None:
                groups = (None,)
            else:
                for identity in identities:
                    _require_segment("object_id", identity)
                unique = tuple(dict.fromkeys(identities))
                groups = tuple(unique[start:start + 800] for start in range(0, len(unique), 800))
            for group in groups:
                values = [collection, *(group or ())]
                if len(parameters) + len(values) > 900:
                    queries.append((clauses, parameters))
                    clauses, parameters = [], []
                clause = "collection = ?"
                if group is not None:
                    clause += " AND object_id IN (" + ",".join("?" for _ in group) + ")"
                clauses.append("(" + clause + ")")
                parameters.extend(values)
        if clauses:
            queries.append((clauses, parameters))
        if not queries:
            return {collection: () for collection in selections}
        connection = self._connect()
        try:
            for clauses, parameters in queries:
                rows = connection.execute(
                    "SELECT collection, object_id, payload_json, revision FROM crp_structured_records WHERE "
                    + " OR ".join(clauses) + " ORDER BY collection, object_id", parameters).fetchall()
                for row in rows:
                    record = _record_from_row(row)
                    result[record.collection].append(record)
        finally:
            connection.close()
        return {collection: tuple(sorted(rows, key=lambda row: row.object_id))
                for collection, rows in result.items()}

    @timed_stage("read_records")
    def list_matching(self, collection: str, **fields: str) -> tuple[SQLiteStructuredRecord, ...]:
        """Read exact top-level string fields without decoding unrelated payloads."""
        _require_segment("collection", collection)
        clauses = ["collection = ?"]
        parameters = [collection]
        for key, value in fields.items():
            _require_segment("field", key)
            if not isinstance(value, str):
                raise SQLiteUnitOfWorkError("matching values must be strings")
            clauses.append("json_extract(payload_json, ?) = ?")
            parameters.extend([f'$."{key}"', value])
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT collection, object_id, payload_json, revision
                FROM crp_structured_records
                WHERE """ + " AND ".join(clauses) + """
                ORDER BY object_id ASC
                """,
                parameters,
            ).fetchall()
            return tuple(_record_from_row(row) for row in rows)
        finally:
            connection.close()

    @timed_stage("read_records")
    def list_all(self) -> tuple[SQLiteStructuredRecord, ...]:
        """Return every structured record for fail-closed cross-aggregate scans."""

        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT collection, object_id, payload_json, revision
                FROM crp_structured_records
                ORDER BY collection ASC, object_id ASC
                """
            ).fetchall()
            return tuple(_record_from_row(row) for row in rows)
        finally:
            connection.close()

    def journal_mode(self) -> str:
        connection = self._connect()
        try:
            row = connection.execute("PRAGMA journal_mode").fetchone()
            if row is None or not isinstance(row[0], str):
                raise SQLiteUnitOfWorkError("SQLite journal mode is unavailable")
            return row[0].lower()
        finally:
            connection.close()

    def foreign_keys_enabled(self) -> bool:
        connection = self._connect()
        try:
            row = connection.execute("PRAGMA foreign_keys").fetchone()
            return row is not None and row[0] == 1
        finally:
            connection.close()

    def synchronous_mode(self) -> str:
        connection = self._connect()
        try:
            row = connection.execute("PRAGMA synchronous").fetchone()
            modes = {0: "off", 1: "normal", 2: "full", 3: "extra"}
            if row is None or row[0] not in modes:
                raise SQLiteUnitOfWorkError("SQLite synchronous mode is unavailable")
            return modes[row[0]]
        finally:
            connection.close()

    @timed_stage("read_records")
    def generation_token(self, collections: tuple[str, ...]) -> str:
        """Return a durable token advanced by committed collection mutations."""

        if (
            not isinstance(collections, tuple)
            or not collections
            or len(collections) != len(set(collections))
        ):
            raise SQLiteUnitOfWorkError(
                "generation token requires unique collections"
            )
        for collection in collections:
            _require_segment("collection", collection)
        connection = self._connect()
        try:
            placeholders = ",".join("?" for _ in collections)
            rows = connection.execute(
                f"""
                SELECT collection, generation
                FROM crp_collection_generations
                WHERE collection IN ({placeholders})
                ORDER BY collection ASC
                """,
                collections,
            ).fetchall()
        finally:
            connection.close()
        observed = {
            str(row["collection"]): int(row["generation"])
            for row in rows
        }
        digest = hashlib.sha256()
        for collection in sorted(collections):
            digest.update(collection.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(observed.get(collection, 0)).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    @reusable_connection(row_factory=sqlite3.Row, group='records')
    def _connect(self) -> sqlite3.Connection:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database_path, check_same_thread=False)
        observe_connection(connection)
        connection.row_factory = sqlite3.Row
        try:
            # Concurrent first openers can get SQLITE_BUSY immediately while
            # switching to WAL, even with SQLite's connection busy timeout.
            deadline = time.monotonic() + 5
            while True:
                try:
                    journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
                    break
                except sqlite3.OperationalError as exc:
                    if (
                        getattr(exc, "sqlite_errorcode", 0) & 0xFF != sqlite3.SQLITE_BUSY
                        or time.monotonic() >= deadline
                    ):
                        raise
                    time.sleep(0.01)
                    remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                    connection.execute(f"PRAGMA busy_timeout={remaining_ms}")
            if journal_mode is None or str(journal_mode[0]).lower() != "wal":
                raise SQLiteUnitOfWorkError("SQLite UoW requires WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=5000")
            _initialize_schema(connection)
            return connection
        except Exception:
            connection.close()
            raise


class SQLiteStructuredRecordUnitOfWork:
    """One short transaction for ordered structured-record mutations."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._records: list[SQLiteStructuredRecord] = []
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the open connection for an explicitly enlisted store seam.

        This is intentionally available only while the unit of work remains
        open. Aggregate adapters must use a public connection-bound participant
        rather than starting a second transaction on the same database.
        """

        self._require_open()
        return self._connection

    def __enter__(self) -> SQLiteStructuredRecordUnitOfWork:
        self._require_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if not self._closed:
            self.rollback()
        return False

    @timed_stage("read_records")
    def read(self, collection: str, object_id: str) -> SQLiteStructuredRecord | None:
        """Read through the same open transaction snapshot."""
        self._require_open()
        _require_segment("collection", collection)
        _require_segment("object_id", object_id)
        row = self._connection.execute(
            """
            SELECT collection, object_id, payload_json, revision
            FROM crp_structured_records
            WHERE collection = ? AND object_id = ?
            """,
            (collection, object_id),
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    def list(self, collection: str) -> tuple[SQLiteStructuredRecord, ...]:
        """List one collection through the current transaction snapshot."""
        self._require_open()
        _require_segment("collection", collection)
        rows = self._connection.execute(
            """
            SELECT collection, object_id, payload_json, revision
            FROM crp_structured_records
            WHERE collection = ?
            ORDER BY object_id ASC
            """,
            (collection,),
        ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def delete(self, collection: str, object_id: str, *, expected_revision: int) -> SQLiteStructuredRecord:
        """Stage one mandatory-CAS delete in the current transaction."""
        self._require_open()
        try:
            record = self.read(collection, object_id)
            actual_revision = record.revision if record is not None else 0
            _require_expected_revision(expected_revision)
            if actual_revision != expected_revision:
                raise SQLiteUnitOfWorkConflict(
                    f"expected revision {expected_revision}, found {actual_revision}"
                )
            if record is None:
                raise SQLiteUnitOfWorkConflict("cannot delete a missing structured record")
            self._connection.execute(
                "DELETE FROM crp_structured_records WHERE collection = ? AND object_id = ?",
                (collection, object_id),
            )
            self._records.append(record)
            return record
        except Exception:
            self.rollback()
            raise

    def put(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        *,
        expected_revision: int,
    ) -> SQLiteStructuredRecord:
        """Stage one create/update using mandatory compare-and-swap revision."""

        return self._put_record(collection, object_id, payload,
            expected_revision=expected_revision, include_in_commit=True)

    def _put_sidecar(
        self, collection: str, object_id: str, payload: Mapping[str, object], *, expected_revision: int,
    ) -> SQLiteStructuredRecord:
        """Enlist storage metadata without changing the caller's commit result."""
        return self._put_record(collection, object_id, payload,
            expected_revision=expected_revision, include_in_commit=False)

    def _put_record(
        self, collection: str, object_id: str, payload: Mapping[str, object], *,
        expected_revision: int, include_in_commit: bool,
    ) -> SQLiteStructuredRecord:

        self._require_open()
        try:
            _require_segment("collection", collection)
            _require_segment("object_id", object_id)
            _require_expected_revision(expected_revision)
            payload_json, stored_payload = _payload_json(payload)
            row = self._connection.execute(
                """
                SELECT revision FROM crp_structured_records
                WHERE collection = ? AND object_id = ?
                """,
                (collection, object_id),
            ).fetchone()
            actual_revision = int(row["revision"]) if row is not None else 0
            if actual_revision != expected_revision:
                raise SQLiteUnitOfWorkConflict(
                    f"expected revision {expected_revision}, found {actual_revision}"
                )
            revision = actual_revision + 1
            if row is None:
                self._connection.execute(
                    """
                    INSERT INTO crp_structured_records
                    (collection, object_id, payload_json, revision)
                    VALUES (?, ?, ?, ?)
                    """,
                    (collection, object_id, payload_json, revision),
                )
            else:
                self._connection.execute(
                    """
                    UPDATE crp_structured_records
                    SET payload_json = ?, revision = ?
                    WHERE collection = ? AND object_id = ?
                    """,
                    (payload_json, revision, collection, object_id),
                )
            record = SQLiteStructuredRecord(
                collection=collection,
                object_id=object_id,
                payload=stored_payload,
                revision=revision,
            )
            if row is None:
                from .record_lineage import record_created
                record_created(self, record)
            if include_in_commit:
                self._records.append(record)
            return record
        except Exception:
            self.rollback()
            raise

    def commit(self) -> tuple[SQLiteStructuredRecord, ...]:
        self._require_open()
        try:
            self._connection.execute("COMMIT")
            return tuple(self._records)
        except Exception:
            _rollback_if_needed(self._connection)
            raise
        finally:
            self._close()

    def rollback(self) -> None:
        if self._closed:
            return
        try:
            _rollback_if_needed(self._connection)
        finally:
            self._close()

    def _require_open(self) -> None:
        if self._closed:
            raise SQLiteUnitOfWorkError("SQLite Unit of Work is closed")

    def _close(self) -> None:
        if self._closed:
            return
        self._connection.close()
        self._closed = True


def _initialize_schema(connection: sqlite3.Connection) -> None:
    # Inspect this connection's database, never cached state for a path that can
    # be replaced/restored. Ready databases need no writer lock or record scan.
    if _schema_ready(connection):
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        # Another first opener may have finished while we waited for the lock.
        if not _schema_ready(connection):
            _create_schema(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _schema_ready(connection: sqlite3.Connection) -> bool:
    required = {
        ("table", "crp_uow_meta", "crp_uow_meta"),
        ("table", "crp_structured_records", "crp_structured_records"),
        ("table", "crp_collection_generations", "crp_collection_generations"),
        ("trigger", "crp_generation_after_insert", "crp_structured_records"),
        ("trigger", "crp_generation_after_update", "crp_structured_records"),
        ("trigger", "crp_generation_after_delete", "crp_structured_records"),
    }
    observed = {
        tuple(row)
        for row in connection.execute(
            "SELECT type, name, tbl_name FROM sqlite_master WHERE name IN "
            "('crp_uow_meta', 'crp_structured_records', 'crp_collection_generations', "
            "'crp_generation_after_insert', 'crp_generation_after_update', "
            "'crp_generation_after_delete')"
        )
    }
    if not required <= observed or connection.execute(
        "SELECT value FROM crp_uow_meta WHERE key = 'schema_version'"
    ).fetchone() is None:
        return False
    # Preparing these queries validates required columns without reading rows.
    # A malformed database must still fail even when all object names exist.
    connection.execute(
        "SELECT collection, object_id, payload_json, revision "
        "FROM crp_structured_records LIMIT 0"
    )
    connection.execute("SELECT collection, generation FROM crp_collection_generations LIMIT 0")
    return True


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS crp_uow_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS crp_structured_records (
            collection TEXT NOT NULL,
            object_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            PRIMARY KEY (collection, object_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS crp_collection_generations (
            collection TEXT PRIMARY KEY,
            generation INTEGER NOT NULL CHECK (generation >= 0)
        )
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO crp_collection_generations
            (collection, generation)
        SELECT collection, COUNT(*) + COALESCE(SUM(revision), 0)
        FROM crp_structured_records
        GROUP BY collection
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS crp_generation_after_insert
        AFTER INSERT ON crp_structured_records
        BEGIN
            INSERT INTO crp_collection_generations (collection, generation)
            VALUES (NEW.collection, 1)
            ON CONFLICT(collection) DO UPDATE
            SET generation = generation + 1;
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS crp_generation_after_update
        AFTER UPDATE ON crp_structured_records
        BEGIN
            INSERT INTO crp_collection_generations (collection, generation)
            VALUES (NEW.collection, 1)
            ON CONFLICT(collection) DO UPDATE
            SET generation = generation + 1;
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS crp_generation_after_delete
        AFTER DELETE ON crp_structured_records
        BEGIN
            INSERT INTO crp_collection_generations (collection, generation)
            VALUES (OLD.collection, 1)
            ON CONFLICT(collection) DO UPDATE
            SET generation = generation + 1;
        END
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO crp_uow_meta (key, value)
        VALUES ('schema_version', ?)
        """,
        (str(_SCHEMA_VERSION),),
    )


def _record_from_row(row: sqlite3.Row) -> SQLiteStructuredRecord:
    collection = row["collection"]
    object_id = row["object_id"]
    revision = row["revision"]
    payload_json = row["payload_json"]
    if (
        not isinstance(collection, str)
        or not isinstance(object_id, str)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(payload_json, str)
    ):
        raise SQLiteUnitOfWorkError("SQLite structured record is invalid")
    _require_segment("collection", collection)
    _require_segment("object_id", object_id)
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise SQLiteUnitOfWorkError("SQLite structured record payload is invalid") from exc
    if not isinstance(payload, dict):
        raise SQLiteUnitOfWorkError("SQLite structured record payload must be a JSON object")
    return SQLiteStructuredRecord(collection, object_id, dict(payload), revision)


def _payload_json(payload: Mapping[str, object]) -> tuple[str, Mapping[str, object]]:
    if not isinstance(payload, Mapping):
        raise SQLiteUnitOfWorkError("payload must be a JSON object")
    try:
        serialized = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        normalized = json.loads(serialized)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SQLiteUnitOfWorkError("payload must be a JSON object") from exc
    if not isinstance(normalized, dict):
        raise SQLiteUnitOfWorkError("payload must be a JSON object")
    return serialized, dict(normalized)


def _require_segment(label: str, value: str) -> None:
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise SQLiteUnitOfWorkError(f"{label} must be a safe repository segment")


def _require_expected_revision(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SQLiteUnitOfWorkError("expected_revision must be a non-negative integer")


def _rollback_if_needed(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.execute("ROLLBACK")
