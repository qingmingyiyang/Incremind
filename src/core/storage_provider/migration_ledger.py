from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from core.storage_provider.observability import observe_connection
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .runtime import ObjectStorePathError, read_json_object_store_collection


_LEDGER_SCHEMA_VERSION = 1
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_SAFE_MIGRATION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SAFE_FAILURE_CODE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class MigrationLedgerError(ValueError):
    """Raised when a migration ledger record or inventory is invalid."""


class MigrationLedgerConflict(MigrationLedgerError):
    """Raised when one migration id is reused for a different input."""


class MigrationLedgerReadOnlyError(MigrationLedgerError):
    """Raised when a recorded migration failure blocks subsequent ledger work."""


@dataclass(frozen=True, slots=True)
class InventoryCollection:
    """Digest-only description of one JSON object collection."""

    collection: str
    object_count: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class JsonObjectStoreInventory:
    """Read-only, path-free inventory of one JsonObjectStore namespace."""

    namespace_id: str
    collections: tuple[InventoryCollection, ...]
    object_count: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class MigrationRecord:
    """Durable migration dry-run or failure record."""

    migration_id: str
    target_schema_version: int
    input_fingerprint: str
    inventory: JsonObjectStoreInventory
    state: str
    rollback_pointer: str
    failure_code: str | None
    created_at: str
    updated_at: str


def scan_json_object_inventory(
    object_store_root: Path,
    *,
    namespace_id: str,
) -> JsonObjectStoreInventory:
    """Return a deterministic inventory without retaining payloads or absolute paths.

    This is deliberately a reader for the legacy JSON object store. It does not
    create directories, mutate files, or treat the result as a transactional
    snapshot. A later cutover must recheck its fingerprint under its own lease.
    """

    _require_segment("namespace_id", namespace_id)
    root = object_store_root.expanduser().resolve(strict=False)
    collections: list[InventoryCollection] = []
    namespace_root = root / "objects" / namespace_id
    if namespace_root.exists():
        if not namespace_root.is_dir() or namespace_root.is_symlink():
            raise MigrationLedgerError("object inventory namespace root must be a directory")
        for collection_path in sorted(namespace_root.iterdir(), key=lambda path: path.name):
            if not collection_path.is_dir() or collection_path.is_symlink():
                raise MigrationLedgerError("object inventory contains an unexpected entry")
            _require_segment("collection", collection_path.name)
            try:
                records = read_json_object_store_collection(
                    root,
                    namespace_id=namespace_id,
                    collection=collection_path.name,
                )
            except ObjectStorePathError as exc:
                raise MigrationLedgerError(str(exc)) from exc
            payload_fingerprints = [
                (f"{record.object_id}.json", _sha256_bytes(record.payload_bytes))
                for record in records
            ]
            collections.append(
                InventoryCollection(
                    collection=collection_path.name,
                    object_count=len(payload_fingerprints),
                    fingerprint=_fingerprint_pairs(payload_fingerprints),
                )
            )
    ordered_collections = tuple(collections)
    object_count = sum(collection.object_count for collection in ordered_collections)
    fingerprint = _fingerprint_parts(
        [
            namespace_id,
            *(
                f"{collection.collection}\0{collection.object_count}\0{collection.fingerprint}"
                for collection in ordered_collections
            ),
        ]
    )
    return JsonObjectStoreInventory(
        namespace_id=namespace_id,
        collections=ordered_collections,
        object_count=object_count,
        fingerprint=fingerprint,
    )


class SQLiteMigrationLedger:
    """Independent SQLite ledger for migration planning, not a runtime write gate.

    The caller must explicitly construct this ledger with a path. It is not
    wired into JsonObjectStore or application composition, so ``assert_writable``
    only fail-closes code that deliberately consults this ledger.
    """

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().resolve(strict=False)

    @property
    def schema_version(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT value FROM migration_ledger_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None or not isinstance(row["value"], str) or not row["value"].isdigit():
                raise MigrationLedgerError("migration ledger schema version is invalid")
            return int(row["value"])
        finally:
            connection.close()

    def journal_mode(self) -> str:
        connection = self._connect()
        try:
            row = connection.execute("PRAGMA journal_mode").fetchone()
            if row is None or not isinstance(row[0], str):
                raise MigrationLedgerError("migration ledger journal mode is unavailable")
            return row[0].lower()
        finally:
            connection.close()

    def plan_dry_run(
        self,
        *,
        migration_id: str,
        target_schema_version: int,
        inventory: JsonObjectStoreInventory,
        rollback_pointer: str,
        now: str | None = None,
    ) -> MigrationRecord:
        """Persist or read an idempotent dry-run record for one immutable input."""

        _require_migration_id(migration_id)
        _require_target_schema_version(target_schema_version)
        _require_inventory(inventory)
        _require_rollback_pointer(rollback_pointer)
        self.assert_writable()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = _select_record(connection, migration_id)
            if existing is not None:
                record = _record_from_row(existing)
                _ensure_same_plan(
                    record,
                    target_schema_version=target_schema_version,
                    inventory=inventory,
                    rollback_pointer=rollback_pointer,
                )
                connection.execute("COMMIT")
                return record
            timestamp = now or _utc_now()
            inventory_json = _inventory_json(inventory)
            connection.execute(
                """
                INSERT INTO migration_ledger (
                    migration_id, target_schema_version, input_fingerprint, inventory_json,
                    state, rollback_pointer, failure_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'dry_run_ready', ?, NULL, ?, ?)
                """,
                (
                    migration_id,
                    target_schema_version,
                    inventory.fingerprint,
                    inventory_json,
                    rollback_pointer,
                    timestamp,
                    timestamp,
                ),
            )
            row = _select_record(connection, migration_id)
            if row is None:  # pragma: no cover - SQLite INSERT must be visible in the same transaction
                raise MigrationLedgerError("migration ledger did not persist dry-run")
            connection.execute("COMMIT")
            return _record_from_row(row)
        except Exception:
            _rollback_if_needed(connection)
            raise
        finally:
            connection.close()

    def mark_failed(
        self,
        migration_id: str,
        *,
        failure_code: str,
        now: str | None = None,
    ) -> MigrationRecord:
        """Record a fail-closed migration result without changing application data."""

        _require_migration_id(migration_id)
        _require_failure_code(failure_code)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = _select_record(connection, migration_id)
            if existing is None:
                raise MigrationLedgerError("migration ledger record was not found")
            record = _record_from_row(existing)
            if record.state == "failed":
                if record.failure_code == failure_code:
                    connection.execute("COMMIT")
                    return record
                raise MigrationLedgerConflict("migration already failed with a different failure code")
            if record.state != "dry_run_ready":
                raise MigrationLedgerError("migration ledger state cannot be marked failed")
            connection.execute(
                """
                UPDATE migration_ledger
                SET state = 'failed', failure_code = ?, updated_at = ?
                WHERE migration_id = ?
                """,
                (failure_code, now or _utc_now(), migration_id),
            )
            row = _select_record(connection, migration_id)
            if row is None:  # pragma: no cover - row exists before UPDATE
                raise MigrationLedgerError("migration ledger failure record was not persisted")
            connection.execute("COMMIT")
            return _record_from_row(row)
        except Exception:
            _rollback_if_needed(connection)
            raise
        finally:
            connection.close()

    def list_records(self) -> tuple[MigrationRecord, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM migration_ledger ORDER BY migration_id ASC"
            ).fetchall()
            return tuple(_record_from_row(row) for row in rows)
        finally:
            connection.close()

    def read_only_reason(self) -> str | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT migration_id, failure_code
                FROM migration_ledger
                WHERE state = 'failed'
                ORDER BY updated_at DESC, migration_id ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            migration_id = row["migration_id"]
            failure_code = row["failure_code"]
            if not isinstance(migration_id, str) or not isinstance(failure_code, str):
                raise MigrationLedgerError("migration ledger failure record is invalid")
            return f"migration {migration_id} failed: {failure_code}"
        finally:
            connection.close()

    def assert_writable(self) -> None:
        reason = self.read_only_reason()
        if reason is not None:
            raise MigrationLedgerReadOnlyError(reason)

    def _connect(self) -> sqlite3.Connection:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database_path)
        observe_connection(connection)
        connection.row_factory = sqlite3.Row
        try:
            journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if journal_mode is None or str(journal_mode[0]).lower() != "wal":
                raise MigrationLedgerError("migration ledger requires SQLite WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=5000")
            _initialize_schema(connection)
            return connection
        except Exception:
            connection.close()
            raise


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS migration_ledger_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS migration_ledger (
            migration_id TEXT PRIMARY KEY,
            target_schema_version INTEGER NOT NULL CHECK (target_schema_version > 0),
            input_fingerprint TEXT NOT NULL,
            inventory_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('dry_run_ready', 'failed')),
            rollback_pointer TEXT NOT NULL,
            failure_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO migration_ledger_meta (key, value)
        VALUES ('schema_version', ?)
        """,
        (str(_LEDGER_SCHEMA_VERSION),),
    )
    connection.commit()


def _select_record(connection: sqlite3.Connection, migration_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM migration_ledger WHERE migration_id = ?",
        (migration_id,),
    ).fetchone()


def _record_from_row(row: sqlite3.Row) -> MigrationRecord:
    inventory_value = row["inventory_json"]
    if not isinstance(inventory_value, str):
        raise MigrationLedgerError("migration ledger inventory is invalid")
    try:
        inventory = _inventory_from_json(inventory_value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MigrationLedgerError("migration ledger inventory is invalid") from exc
    migration_id = row["migration_id"]
    target_schema_version = row["target_schema_version"]
    input_fingerprint = row["input_fingerprint"]
    state = row["state"]
    rollback_pointer = row["rollback_pointer"]
    failure_code = row["failure_code"]
    created_at = row["created_at"]
    updated_at = row["updated_at"]
    if (
        not isinstance(migration_id, str)
        or not isinstance(target_schema_version, int)
        or not isinstance(input_fingerprint, str)
        or not isinstance(state, str)
        or not isinstance(rollback_pointer, str)
        or failure_code is not None and not isinstance(failure_code, str)
        or not isinstance(created_at, str)
        or not isinstance(updated_at, str)
    ):
        raise MigrationLedgerError("migration ledger record is invalid")
    _require_migration_id(migration_id)
    _require_target_schema_version(target_schema_version)
    _require_fingerprint(input_fingerprint)
    _require_rollback_pointer(rollback_pointer)
    if state not in {"dry_run_ready", "failed"}:
        raise MigrationLedgerError("migration ledger state is invalid")
    if failure_code is not None:
        _require_failure_code(failure_code)
    return MigrationRecord(
        migration_id=migration_id,
        target_schema_version=target_schema_version,
        input_fingerprint=input_fingerprint,
        inventory=inventory,
        state=state,
        rollback_pointer=rollback_pointer,
        failure_code=failure_code,
        created_at=created_at,
        updated_at=updated_at,
    )


def _ensure_same_plan(
    record: MigrationRecord,
    *,
    target_schema_version: int,
    inventory: JsonObjectStoreInventory,
    rollback_pointer: str,
) -> None:
    if record.target_schema_version != target_schema_version:
        raise MigrationLedgerConflict("migration id already has a different target schema version")
    if record.input_fingerprint != inventory.fingerprint:
        raise MigrationLedgerConflict("migration id already has a different input fingerprint")
    if record.rollback_pointer != rollback_pointer:
        raise MigrationLedgerConflict("migration id already has a different rollback pointer")


def _inventory_json(inventory: JsonObjectStoreInventory) -> str:
    return json.dumps(
        {
            "namespace_id": inventory.namespace_id,
            "object_count": inventory.object_count,
            "fingerprint": inventory.fingerprint,
            "collections": [
                {
                    "collection": collection.collection,
                    "object_count": collection.object_count,
                    "fingerprint": collection.fingerprint,
                }
                for collection in inventory.collections
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _inventory_from_json(value: str) -> JsonObjectStoreInventory:
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("inventory must be an object")
    namespace_id = payload.get("namespace_id")
    object_count = payload.get("object_count")
    fingerprint = payload.get("fingerprint")
    collections_value = payload.get("collections")
    if not isinstance(namespace_id, str) or not isinstance(object_count, int) or not isinstance(fingerprint, str):
        raise ValueError("inventory fields are invalid")
    if not isinstance(collections_value, list):
        raise ValueError("inventory collections are invalid")
    collections: list[InventoryCollection] = []
    for value_item in collections_value:
        if not isinstance(value_item, dict):
            raise ValueError("inventory collection is invalid")
        collection = value_item.get("collection")
        count = value_item.get("object_count")
        collection_fingerprint = value_item.get("fingerprint")
        if not isinstance(collection, str) or not isinstance(count, int) or not isinstance(collection_fingerprint, str):
            raise ValueError("inventory collection fields are invalid")
        collections.append(InventoryCollection(collection, count, collection_fingerprint))
    inventory = JsonObjectStoreInventory(
        namespace_id=namespace_id,
        collections=tuple(collections),
        object_count=object_count,
        fingerprint=fingerprint,
    )
    _require_inventory(inventory)
    return inventory


def _require_inventory(inventory: JsonObjectStoreInventory) -> None:
    _require_segment("namespace_id", inventory.namespace_id)
    if inventory.object_count < 0:
        raise MigrationLedgerError("inventory object count must be non-negative")
    _require_fingerprint(inventory.fingerprint)
    if inventory.object_count != sum(collection.object_count for collection in inventory.collections):
        raise MigrationLedgerError("inventory object count does not match collections")
    if tuple(sorted(collection.collection for collection in inventory.collections)) != tuple(
        collection.collection for collection in inventory.collections
    ):
        raise MigrationLedgerError("inventory collections must be deterministic")
    for collection in inventory.collections:
        _require_segment("collection", collection.collection)
        if collection.object_count < 0:
            raise MigrationLedgerError("inventory collection count must be non-negative")
        _require_fingerprint(collection.fingerprint)


def _require_segment(name: str, value: str) -> None:
    if not _SAFE_SEGMENT.fullmatch(value):
        raise MigrationLedgerError(f"{name} must be a safe identifier")


def _require_migration_id(value: str) -> None:
    if not _SAFE_MIGRATION_ID.fullmatch(value):
        raise MigrationLedgerError("migration id must be a safe identifier")


def _require_target_schema_version(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MigrationLedgerError("target schema version must be a positive integer")


def _require_fingerprint(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise MigrationLedgerError("fingerprint must be a SHA-256 hex string")


def _require_rollback_pointer(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise MigrationLedgerError("rollback pointer must be a non-empty bounded string")


def _require_failure_code(value: str) -> None:
    if not _SAFE_FAILURE_CODE.fullmatch(value):
        raise MigrationLedgerError("failure code must be a safe identifier")


def _fingerprint_pairs(pairs: Iterable[tuple[str, str]]) -> str:
    return _fingerprint_parts(f"{name}\0{fingerprint}" for name, fingerprint in pairs)


def _fingerprint_parts(parts: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _rollback_if_needed(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.execute("ROLLBACK")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
