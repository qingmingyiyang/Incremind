"""Durable single authority for approved MCP server snapshots.

This module intentionally has no API, manager, registry, or project binding
dependency.  It records the ordered fence that those later adapters must obey:
old runtime revocation happens between ``cutover_started`` and pointer commit;
the inverse happens for rollback.  SQLite owns the sole active pointer once it
has been bootstrapped from the legacy JSON input.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from threading import Lock
from uuid import uuid4

from blake3 import blake3

from backend.security.mcp_approved_server_contracts import (
    MCPApprovedServerSnapshot,
    MCPApprovedServerStoreError,
    canonical_mcp_approved_server_payload,
    parse_mcp_approved_server_payload,
)
from backend.shared.interprocess_lock import interprocess_file_lock


_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PROVENANCE_REF = re.compile(r"^crp://[A-Za-z0-9][A-Za-z0-9._~:/-]{7,247}$")
_EXTERNAL_IMPORT_PROVENANCE_FIELDS = frozenset({
    "schema_version", "provenance_ref", "kind", "project_id", "extension_id",
    "intake_ref", "artifact_ref", "artifact_receipt_ref",
    "artifact_content_sha256", "manifest_identity", "review_plan_identity",
    "candidate_identity", "affected_server_ids", "confirmation_ids", "actor",
    "reason", "expected_active_snapshot_revision",
})
_STATES = frozenset({
    "previewed", "confirmed", "cutover_started", "old_revoked",
    "cutover_committed", "rollback_started", "new_revoked", "rolled_back",
})
_LOCK = Lock()


class MCPApprovedServerMigrationError(ValueError):
    """A safe migration-authority failure with no reviewed profile contents."""


class MCPApprovedServerMigrationConflict(MCPApprovedServerMigrationError):
    """CAS, command reuse, or ordered-transition conflict."""


@dataclass(frozen=True, slots=True)
class MCPApprovedServerMigrationStatus:
    migration_id: str
    revision: int
    state: str
    active_snapshot_revision: int
    affected_server_count: int
    affected_server_ids: tuple[str, ...] = ()
    provenance_ref: str | None = None

    def public(self) -> dict[str, object]:
        result: dict[str, object] = {
            "migration_id": self.migration_id,
            "revision": self.revision,
            "state": self.state,
            "active_snapshot_revision": self.active_snapshot_revision,
            "affected_server_count": self.affected_server_count,
        }
        if self.provenance_ref is not None:
            result["provenance_ref"] = self.provenance_ref
        return result


class MCPApprovedServerMigrationAuthority:
    """SQLite authority for immutable candidates and an active snapshot pointer."""

    _DATABASE = "mcp-approved-server-authority.sqlite3"

    def __init__(self, root_dir: Path) -> None:
        root = Path(root_dir)
        self._security = root / ".rebuild-data" / "security"
        self._path = self._security / self._DATABASE

    @classmethod
    def database_exists(cls, security_dir: Path) -> bool:
        return (Path(security_dir) / cls._DATABASE).is_file()

    def active_snapshot(self) -> MCPApprovedServerSnapshot:
        return self.active_snapshot_state()[1]

    def active_snapshot_state(self) -> tuple[int, MCPApprovedServerSnapshot]:
        """Read the active revision and snapshot under one authority lock."""
        with _LOCK, closing(self._connection()) as conn:
            self._bootstrap(conn)
            row = self._active_snapshot_row(conn)
            try:
                return (
                    self._active_revision(conn),
                    parse_mcp_approved_server_payload(row["payload"]),
                )
            except MCPApprovedServerStoreError:
                raise MCPApprovedServerMigrationError("MCP approved authority is invalid") from None

    def preview(
        self,
        *,
        candidate: object,
        command_id: str,
        expected_active_snapshot_revision: int | None = None,
        provenance_ref: str | None = None,
        provenance: Mapping[str, object] | None = None,
    ) -> MCPApprovedServerMigrationStatus:
        command_id = _command_id(command_id)
        if expected_active_snapshot_revision is not None:
            expected_active_snapshot_revision = _revision(expected_active_snapshot_revision)
        try:
            payload, _ = canonical_mcp_approved_server_payload(candidate)
        except MCPApprovedServerStoreError:
            raise MCPApprovedServerMigrationError("MCP approved migration candidate is invalid") from None
        receipt_ref, provenance_payload = _provenance_payload(
            provenance_ref,
            provenance,
            candidate_payload=payload,
            expected_active_snapshot_revision=expected_active_snapshot_revision,
        )
        with _LOCK, closing(self._connection()) as conn:
            self._bootstrap(conn)
            self._begin(conn)
            try:
                existing = self._command(conn, command_id)
                if existing is not None:
                    if (
                        existing["operation"] != "preview"
                        or not _target_payload_matches(
                            conn, str(existing["migration_id"]), payload,
                        )
                        or not _migration_provenance_matches(
                            conn,
                            str(existing["migration_id"]),
                            receipt_ref,
                            provenance_payload,
                        )
                    ):
                        raise MCPApprovedServerMigrationConflict("migration command identity is already used")
                    conn.commit()
                    return self._command_result(conn, existing)
                source_revision = self._active_revision(conn)
                if (
                    expected_active_snapshot_revision is not None
                    and source_revision != expected_active_snapshot_revision
                ):
                    raise MCPApprovedServerMigrationConflict(
                        "active approved pointer changed"
                    )
                affected_ids = self._affected_ids(conn, source_revision, payload)
                if not affected_ids:
                    raise MCPApprovedServerMigrationConflict("MCP approved migration has no changes")
                if provenance_payload is not None:
                    _require_provenance_contract(
                        provenance_payload,
                        active_snapshot_revision=source_revision,
                        affected_server_ids=affected_ids,
                    )
                    self._put_provenance(conn, receipt_ref, provenance_payload)
                    linked = conn.execute(
                        "SELECT migration_id FROM mcp_approved_migrations "
                        "WHERE provenance_ref = ?",
                        (receipt_ref,),
                    ).fetchone()
                    if linked is not None:
                        raise MCPApprovedServerMigrationConflict(
                            "MCP migration provenance is already linked"
                        )
                target_revision = self._insert_snapshot(conn, payload)
                migration_id = uuid4().hex
                conn.execute(
                    "INSERT INTO mcp_approved_migrations "
                    "(migration_id, source_snapshot_revision, target_snapshot_revision, state, revision, affected_server_count, affected_server_ids, provenance_ref, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'previewed', 1, ?, ?, ?, ?, ?)",
                    (
                        migration_id, source_revision, target_revision,
                        len(affected_ids), json.dumps(affected_ids), receipt_ref,
                        _now(), _now(),
                    ),
                )
                result = self._status(conn, migration_id)
                self._record_command(conn, command_id, "preview", migration_id, 0, result)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def confirm(self, *, migration_id: str, expected_revision: int, command_id: str, confirmed: bool, provenance_ref: str | None = None) -> MCPApprovedServerMigrationStatus:
        if confirmed is not True:
            raise MCPApprovedServerMigrationError("migration confirmation is required")
        return self._transition(
            migration_id=migration_id, expected_revision=expected_revision,
            command_id=command_id, operation="confirm", from_state="previewed", to_state="confirmed",
            provenance_ref=provenance_ref,
        )

    def begin_cutover(self, *, migration_id: str, expected_revision: int, command_id: str, provenance_ref: str | None = None) -> MCPApprovedServerMigrationStatus:
        return self._transition(
            migration_id=migration_id, expected_revision=expected_revision,
            command_id=command_id, operation="begin_cutover", from_state="confirmed", to_state="cutover_started", provenance_ref=provenance_ref,
        )

    def mark_old_revoked(self, *, migration_id: str, expected_revision: int, command_id: str, provenance_ref: str | None = None) -> MCPApprovedServerMigrationStatus:
        return self._transition(
            migration_id=migration_id, expected_revision=expected_revision,
            command_id=command_id, operation="mark_old_revoked", from_state="cutover_started", to_state="old_revoked", provenance_ref=provenance_ref,
        )

    def commit_cutover(self, *, migration_id: str, expected_revision: int, command_id: str, provenance_ref: str | None = None) -> MCPApprovedServerMigrationStatus:
        return self._transition(
            migration_id=migration_id, expected_revision=expected_revision,
            command_id=command_id, operation="commit_cutover", from_state="old_revoked", to_state="cutover_committed", advance_pointer=True, provenance_ref=provenance_ref,
        )

    def begin_rollback(self, *, migration_id: str, expected_revision: int, command_id: str, provenance_ref: str | None = None) -> MCPApprovedServerMigrationStatus:
        return self._transition(
            migration_id=migration_id, expected_revision=expected_revision,
            command_id=command_id, operation="begin_rollback", from_state="cutover_committed", to_state="rollback_started", provenance_ref=provenance_ref,
        )

    def mark_new_revoked(self, *, migration_id: str, expected_revision: int, command_id: str, provenance_ref: str | None = None) -> MCPApprovedServerMigrationStatus:
        return self._transition(
            migration_id=migration_id, expected_revision=expected_revision,
            command_id=command_id, operation="mark_new_revoked", from_state="rollback_started", to_state="new_revoked", provenance_ref=provenance_ref,
        )

    def commit_rollback(self, *, migration_id: str, expected_revision: int, command_id: str, provenance_ref: str | None = None) -> MCPApprovedServerMigrationStatus:
        return self._transition(
            migration_id=migration_id, expected_revision=expected_revision,
            command_id=command_id, operation="commit_rollback", from_state="new_revoked", to_state="rolled_back", advance_pointer=True, provenance_ref=provenance_ref,
        )

    def status(self, migration_id: str) -> MCPApprovedServerMigrationStatus | None:
        with _LOCK, closing(self._connection()) as conn:
            self._bootstrap(conn)
            row = conn.execute("SELECT 1 FROM mcp_approved_migrations WHERE migration_id = ?", (migration_id,)).fetchone()
            return self._status(conn, migration_id) if row is not None else None

    def blocking_migration(self) -> MCPApprovedServerMigrationStatus | None:
        """Read the switching fence for later startup/manager fail-closed use."""
        with _LOCK, closing(self._connection()) as conn:
            self._bootstrap(conn)
            row = conn.execute(
                "SELECT migration_id FROM mcp_approved_migrations "
                "WHERE state IN ('cutover_started', 'old_revoked', 'rollback_started', 'new_revoked') "
                "ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            return self._status(conn, str(row["migration_id"])) if row is not None else None

    def terminal_operation_receipt(
        self,
        *,
        migration_id: str,
        expected_revision: int,
        command_id: str,
        operation: str,
        record: bool = False,
        provenance_ref: str | None = None,
    ) -> MCPApprovedServerMigrationStatus | None:
        """Read or atomically seal one outer cutover/rollback command receipt.

        Sub-step receipts retain crash recovery.  This terminal receipt prevents
        a response retry from replaying runtime revocation after pointer commit.
        """
        migration_id = _migration_id(migration_id)
        expected_revision = _revision(expected_revision)
        command_id = _command_id(command_id)
        if operation not in {"cutover", "rollback"}:
            raise MCPApprovedServerMigrationError("migration operation is invalid")
        terminal = "cutover_committed" if operation == "cutover" else "rolled_back"
        with _LOCK, closing(self._connection()) as conn:
            self._bootstrap(conn)
            self._begin(conn)
            try:
                self._require_transition_provenance(
                    conn, migration_id, provenance_ref,
                )
                existing = self._command(conn, command_id)
                if existing is not None:
                    if (
                        existing["operation"], existing["migration_id"],
                        existing["expected_revision"],
                    ) != (operation, migration_id, expected_revision):
                        raise MCPApprovedServerMigrationConflict(
                            "migration command identity is already used"
                        )
                    conn.commit()
                    return self._command_result(conn, existing)
                if not record:
                    conn.commit()
                    return None
                result = self._status(conn, migration_id)
                if result.state != terminal:
                    raise MCPApprovedServerMigrationConflict(
                        "migration terminal result is unavailable"
                    )
                self._validate_subcommand_chain(
                    conn, migration_id=migration_id,
                    expected_revision=expected_revision,
                    command_id=command_id, operation=operation,
                )
                self._record_command(
                    conn, command_id, operation, migration_id,
                    expected_revision, result,
                )
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def _validate_subcommand_chain(
        self,
        conn: sqlite3.Connection,
        *,
        migration_id: str,
        expected_revision: int,
        command_id: str,
        operation: str,
    ) -> None:
        operations = (
            ("begin_cutover", "mark_old_revoked", "commit_cutover")
            if operation == "cutover"
            else ("begin_rollback", "mark_new_revoked", "commit_rollback")
        )
        expected = expected_revision
        for suffix, expected_operation in zip(("begin", "revoked", "commit"), operations):
            row = self._command(conn, f"{command_id}.{suffix}")
            if row is None or (
                row["operation"], row["migration_id"], row["expected_revision"]
            ) != (expected_operation, migration_id, expected):
                raise MCPApprovedServerMigrationConflict(
                    "migration command chain is incomplete"
                )
            expected = int(row["result_revision"])

    def _transition(self, *, migration_id: str, expected_revision: int, command_id: str, operation: str, from_state: str, to_state: str, advance_pointer: bool = False, provenance_ref: str | None = None) -> MCPApprovedServerMigrationStatus:
        migration_id = _migration_id(migration_id)
        expected_revision = _revision(expected_revision)
        command_id = _command_id(command_id)
        with _LOCK, closing(self._connection()) as conn:
            self._bootstrap(conn)
            self._begin(conn)
            try:
                self._require_transition_provenance(
                    conn, migration_id, provenance_ref,
                )
                existing = self._command(conn, command_id)
                if existing is not None:
                    if (existing["operation"], existing["migration_id"], existing["expected_revision"]) != (operation, migration_id, expected_revision):
                        raise MCPApprovedServerMigrationConflict("migration command identity is already used")
                    conn.commit()
                    return self._command_result(conn, existing)
                current = self._status(conn, migration_id)
                if current.revision != expected_revision or current.state != from_state:
                    raise MCPApprovedServerMigrationConflict("migration revision or state changed")
                next_revision = current.revision + 1
                row = conn.execute("SELECT source_snapshot_revision, target_snapshot_revision FROM mcp_approved_migrations WHERE migration_id = ?", (migration_id,)).fetchone()
                assert row is not None
                required_pointer = (
                    int(row["source_snapshot_revision"])
                    if operation in {"begin_cutover", "mark_old_revoked", "commit_cutover"}
                    else int(row["target_snapshot_revision"])
                    if operation in {"begin_rollback", "mark_new_revoked", "commit_rollback"}
                    else None
                )
                if required_pointer is not None and self._active_revision(conn) != required_pointer:
                    raise MCPApprovedServerMigrationConflict("active approved pointer changed")
                if to_state in {"cutover_started", "rollback_started"}:
                    switching = conn.execute(
                        "SELECT 1 FROM mcp_approved_migrations WHERE migration_id != ? "
                        "AND state IN ('cutover_started', 'old_revoked', 'rollback_started', 'new_revoked') LIMIT 1",
                        (migration_id,),
                    ).fetchone()
                    if switching is not None:
                        raise MCPApprovedServerMigrationConflict("another MCP approved migration is switching")
                if advance_pointer:
                    pointer = int(row["target_snapshot_revision"] if to_state == "cutover_committed" else row["source_snapshot_revision"])
                    conn.execute("UPDATE mcp_approved_meta SET value = ? WHERE key = 'active_snapshot_revision'", (str(pointer),))
                conn.execute(
                    "UPDATE mcp_approved_migrations SET state = ?, revision = ?, updated_at = ? WHERE migration_id = ?",
                    (to_state, next_revision, _now(), migration_id),
                )
                result = self._status(conn, migration_id)
                self._record_command(conn, command_id, operation, migration_id, expected_revision, result)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def _connection(self) -> sqlite3.Connection:
        self._security.mkdir(parents=True, exist_ok=True)
        try:
            with interprocess_file_lock(self._path):
                conn = sqlite3.connect(self._path, timeout=5.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA foreign_keys=ON")
                self._initialize(conn)
                check = conn.execute("PRAGMA quick_check").fetchone()
                if check is None or check[0] != "ok":
                    conn.close()
                    raise sqlite3.DatabaseError("quick check failed")
                return conn
        except (sqlite3.Error, TimeoutError) as error:
            raise MCPApprovedServerMigrationConflict("MCP approved migration authority is busy") from error

    def _initialize(self, conn: sqlite3.Connection) -> None:
        # All schema objects and the readiness marker commit as one short
        # transaction.  A crash before commit is retried, never mistaken for a
        # bootstrapped active authority.
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS mcp_approved_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS mcp_approved_snapshots (snapshot_revision INTEGER PRIMARY KEY AUTOINCREMENT, payload BLOB NOT NULL UNIQUE, created_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS mcp_approved_migration_provenance (provenance_ref TEXT PRIMARY KEY, payload BLOB NOT NULL, created_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS mcp_approved_migrations (migration_id TEXT PRIMARY KEY, source_snapshot_revision INTEGER NOT NULL, target_snapshot_revision INTEGER NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL, affected_server_count INTEGER NOT NULL, affected_server_ids TEXT NOT NULL DEFAULT '[]', provenance_ref TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(source_snapshot_revision) REFERENCES mcp_approved_snapshots(snapshot_revision), FOREIGN KEY(target_snapshot_revision) REFERENCES mcp_approved_snapshots(snapshot_revision), FOREIGN KEY(provenance_ref) REFERENCES mcp_approved_migration_provenance(provenance_ref))")
            conn.execute("CREATE TABLE IF NOT EXISTS mcp_approved_migration_commands (command_id TEXT PRIMARY KEY, operation TEXT NOT NULL, migration_id TEXT NOT NULL, expected_revision INTEGER NOT NULL, result_revision INTEGER NOT NULL, result_state TEXT NOT NULL DEFAULT 'previewed', result_active_snapshot_revision INTEGER NOT NULL DEFAULT 0, result_affected_server_count INTEGER NOT NULL DEFAULT 0, result_affected_server_ids TEXT NOT NULL DEFAULT '[]', result_provenance_ref TEXT, created_at TEXT NOT NULL, FOREIGN KEY(migration_id) REFERENCES mcp_approved_migrations(migration_id))")
            self._ensure_column(conn, "mcp_approved_migrations", "affected_server_ids", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "mcp_approved_migrations", "provenance_ref", "TEXT")
            self._ensure_column(conn, "mcp_approved_migration_commands", "result_state", "TEXT NOT NULL DEFAULT 'previewed'")
            self._ensure_column(conn, "mcp_approved_migration_commands", "result_active_snapshot_revision", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "mcp_approved_migration_commands", "result_affected_server_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "mcp_approved_migration_commands", "result_affected_server_ids", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "mcp_approved_migration_commands", "result_provenance_ref", "TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_mcp_migration_provenance ON mcp_approved_migrations(provenance_ref) WHERE provenance_ref IS NOT NULL")
            conn.execute("INSERT OR REPLACE INTO mcp_approved_meta(key, value) VALUES ('schema_version', '3')")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _begin(self, conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")

    def _bootstrap(self, conn: sqlite3.Connection) -> None:
        if conn.execute("SELECT 1 FROM mcp_approved_meta WHERE key = 'active_snapshot_revision'").fetchone() is not None:
            self._active_snapshot_row(conn)
            return
        unified = self._security / "mcp-approved-servers.json"
        legacy = self._security / "mcp-approved-stdio.json"
        if unified.exists() and legacy.exists():
            raise MCPApprovedServerMigrationError("MCP approved authority migration is incomplete")
        path = unified if unified.exists() else legacy
        if path.is_file():
            try:
                payload = path.read_bytes()
                parse_mcp_approved_server_payload(payload)
            except MCPApprovedServerStoreError:
                raise MCPApprovedServerMigrationError("MCP approved authority is invalid") from None
        else:
            payload, _ = canonical_mcp_approved_server_payload({"schema_version": "1.2.0", "servers": []})
        revision = self._insert_snapshot(conn, payload)
        conn.execute("INSERT INTO mcp_approved_meta(key, value) VALUES ('active_snapshot_revision', ?)", (str(revision),))
        conn.commit()
        self._active_snapshot_row(conn)

    def _active_snapshot_row(self, conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute("SELECT value FROM mcp_approved_meta WHERE key = 'active_snapshot_revision'").fetchone()
        try:
            revision = int(row["value"]) if row is not None else 0
        except (TypeError, ValueError):
            revision = 0
        if revision < 1:
            raise MCPApprovedServerMigrationError("MCP approved authority is unavailable")
        snapshot = conn.execute(
            "SELECT payload FROM mcp_approved_snapshots WHERE snapshot_revision = ?", (revision,)
        ).fetchone()
        if snapshot is None or not isinstance(snapshot["payload"], bytes):
            raise MCPApprovedServerMigrationError("MCP approved authority is unavailable")
        try:
            parse_mcp_approved_server_payload(snapshot["payload"])
        except MCPApprovedServerStoreError:
            raise MCPApprovedServerMigrationError("MCP approved authority is invalid") from None
        return snapshot

    def _insert_snapshot(self, conn: sqlite3.Connection, payload: bytes) -> int:
        conn.execute("INSERT OR IGNORE INTO mcp_approved_snapshots(payload, created_at) VALUES (?, ?)", (payload, _now()))
        row = conn.execute("SELECT snapshot_revision FROM mcp_approved_snapshots WHERE payload = ?", (payload,)).fetchone()
        if row is None:
            raise MCPApprovedServerMigrationError("MCP approved snapshot is unavailable")
        return int(row["snapshot_revision"])

    def _active_revision(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM mcp_approved_meta WHERE key = 'active_snapshot_revision'").fetchone()
        return int(row["value"]) if row is not None else 0

    def _server_ids(self, conn: sqlite3.Connection, snapshot_revision: int) -> set[str]:
        row = conn.execute("SELECT payload FROM mcp_approved_snapshots WHERE snapshot_revision = ?", (snapshot_revision,)).fetchone()
        if row is None or not isinstance(row["payload"], bytes):
            raise MCPApprovedServerMigrationError("MCP approved snapshot is unavailable")
        return {record.server_id for record in parse_mcp_approved_server_payload(row["payload"]).servers}

    def _affected_ids(self, conn: sqlite3.Connection, source_snapshot_revision: int, target_payload: bytes) -> tuple[str, ...]:
        row = conn.execute("SELECT payload FROM mcp_approved_snapshots WHERE snapshot_revision = ?", (source_snapshot_revision,)).fetchone()
        if row is None or not isinstance(row["payload"], bytes):
            raise MCPApprovedServerMigrationError("MCP approved snapshot is unavailable")
        source = _canonical_records(row["payload"])
        target = _canonical_records(target_payload)
        return tuple(sorted(server_id for server_id in source.keys() | target.keys() if source.get(server_id) != target.get(server_id)))

    def _put_provenance(
        self,
        conn: sqlite3.Connection,
        provenance_ref: str | None,
        payload: bytes,
    ) -> None:
        if provenance_ref is None:
            raise MCPApprovedServerMigrationError(
                "MCP migration provenance reference is missing"
            )
        existing = conn.execute(
            "SELECT payload FROM mcp_approved_migration_provenance "
            "WHERE provenance_ref = ?",
            (provenance_ref,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO mcp_approved_migration_provenance "
                "(provenance_ref, payload, created_at) VALUES (?, ?, ?)",
                (provenance_ref, payload, _now()),
            )
        elif existing["payload"] != payload:
            raise MCPApprovedServerMigrationConflict(
                "MCP migration provenance identity is already used"
            )

    def _require_transition_provenance(
        self,
        conn: sqlite3.Connection,
        migration_id: str,
        supplied: str | None,
    ) -> None:
        row = conn.execute(
            "SELECT provenance_ref FROM mcp_approved_migrations "
            "WHERE migration_id = ?",
            (migration_id,),
        ).fetchone()
        if row is None:
            raise MCPApprovedServerMigrationError(
                "MCP approved migration is unavailable"
            )
        expected = row["provenance_ref"]
        if expected is None:
            if supplied is not None:
                raise MCPApprovedServerMigrationConflict(
                    "migration does not accept provenance"
                )
            return
        if _optional_provenance_ref(supplied) != str(expected):
            raise MCPApprovedServerMigrationConflict(
                "migration provenance confirmation is required"
            )
        _load_provenance(conn, str(expected))

    def _command(self, conn: sqlite3.Connection, command_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM mcp_approved_migration_commands WHERE command_id = ?", (command_id,)).fetchone()

    def _record_command(self, conn: sqlite3.Connection, command_id: str, operation: str, migration_id: str, expected_revision: int, result: MCPApprovedServerMigrationStatus) -> None:
        conn.execute(
            "INSERT INTO mcp_approved_migration_commands "
            "(command_id, operation, migration_id, expected_revision, result_revision, result_state, result_active_snapshot_revision, result_affected_server_count, result_affected_server_ids, result_provenance_ref, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (command_id, operation, migration_id, expected_revision, result.revision, result.state,
             result.active_snapshot_revision, result.affected_server_count,
             json.dumps(result.affected_server_ids), result.provenance_ref, _now()),
        )

    def _command_result(
        self, conn: sqlite3.Connection, row: sqlite3.Row,
    ) -> MCPApprovedServerMigrationStatus:
        try:
            ids = tuple(json.loads(str(row["result_affected_server_ids"])))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise MCPApprovedServerMigrationError("MCP approved migration receipt is invalid") from None
        if any(not isinstance(server_id, str) for server_id in ids):
            raise MCPApprovedServerMigrationError("MCP approved migration receipt is invalid")
        provenance_ref = row["result_provenance_ref"]
        if provenance_ref is not None:
            provenance_ref = str(provenance_ref)
            _load_provenance(conn, provenance_ref)
        return MCPApprovedServerMigrationStatus(
            str(row["migration_id"]), int(row["result_revision"]), str(row["result_state"]),
            int(row["result_active_snapshot_revision"]),
            int(row["result_affected_server_count"]), ids, provenance_ref,
        )

    def _status(self, conn: sqlite3.Connection, migration_id: str) -> MCPApprovedServerMigrationStatus:
        row = conn.execute(
            "SELECT migration_id, revision, state, affected_server_count, "
            "affected_server_ids, provenance_ref FROM mcp_approved_migrations "
            "WHERE migration_id = ?",
            (migration_id,),
        ).fetchone()
        if row is None or row["state"] not in _STATES:
            raise MCPApprovedServerMigrationError("MCP approved migration is unavailable")
        try:
            ids = tuple(json.loads(str(row["affected_server_ids"])))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise MCPApprovedServerMigrationError("MCP approved migration is invalid") from None
        if any(not isinstance(server_id, str) for server_id in ids) or len(ids) != int(row["affected_server_count"]):
            raise MCPApprovedServerMigrationError("MCP approved migration is invalid")
        provenance_ref = row["provenance_ref"]
        if provenance_ref is not None:
            provenance_ref = str(provenance_ref)
            _load_provenance(conn, provenance_ref)
        return MCPApprovedServerMigrationStatus(
            str(row["migration_id"]), int(row["revision"]), str(row["state"]),
            self._active_revision(conn), int(row["affected_server_count"]), ids,
            provenance_ref,
        )


def _target_payload_matches(conn: sqlite3.Connection, migration_id: str, payload: bytes) -> bool:
    row = conn.execute("SELECT s.payload FROM mcp_approved_migrations m JOIN mcp_approved_snapshots s ON s.snapshot_revision = m.target_snapshot_revision WHERE m.migration_id = ?", (migration_id,)).fetchone()
    return row is not None and row["payload"] == payload


def _migration_provenance_matches(
    conn: sqlite3.Connection,
    migration_id: str,
    provenance_ref: str | None,
    provenance_payload: bytes | None,
) -> bool:
    row = conn.execute(
        "SELECT provenance_ref FROM mcp_approved_migrations WHERE migration_id = ?",
        (migration_id,),
    ).fetchone()
    if row is None:
        return False
    stored = row["provenance_ref"]
    if stored is None:
        return provenance_ref is None and provenance_payload is None
    if provenance_ref != str(stored) or provenance_payload is None:
        return False
    return _load_provenance(conn, str(stored)) == provenance_payload


def _load_provenance(conn: sqlite3.Connection, provenance_ref: str) -> bytes:
    _optional_provenance_ref(provenance_ref)
    row = conn.execute(
        "SELECT payload FROM mcp_approved_migration_provenance "
        "WHERE provenance_ref = ?",
        (provenance_ref,),
    ).fetchone()
    if row is None or not isinstance(row["payload"], bytes):
        raise MCPApprovedServerMigrationError(
            "MCP migration provenance is unavailable"
        )
    try:
        value = json.loads(row["payload"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MCPApprovedServerMigrationError(
            "MCP migration provenance is invalid"
        ) from None
    if not isinstance(value, dict) or value.get("provenance_ref") != provenance_ref:
        raise MCPApprovedServerMigrationError(
            "MCP migration provenance is invalid"
        )
    return row["payload"]


def _provenance_payload(
    provenance_ref: str | None,
    provenance: Mapping[str, object] | None,
    *,
    candidate_payload: bytes,
    expected_active_snapshot_revision: int | None,
) -> tuple[str | None, bytes | None]:
    if provenance_ref is None and provenance is None:
        return None, None
    if provenance_ref is None or provenance is None:
        raise MCPApprovedServerMigrationError(
            "migration provenance is incomplete"
        )
    reference = _optional_provenance_ref(provenance_ref)
    if expected_active_snapshot_revision is None:
        raise MCPApprovedServerMigrationError(
            "migration provenance requires an active snapshot revision"
        )
    value = dict(provenance)
    if set(value) != _EXTERNAL_IMPORT_PROVENANCE_FIELDS:
        raise MCPApprovedServerMigrationError(
            "migration provenance fields are invalid"
        )
    if value.get("schema_version") != "1.0.0" or value.get("kind") != (
        "external_extension_mcp_import_review"
    ):
        raise MCPApprovedServerMigrationError(
            "migration provenance contract is invalid"
        )
    if value.get("provenance_ref") != reference:
        raise MCPApprovedServerMigrationError(
            "migration provenance reference drifted"
        )
    expected_candidate_identity = _candidate_identity(candidate_payload)
    if value.get("candidate_identity") != expected_candidate_identity:
        raise MCPApprovedServerMigrationError(
            "migration provenance candidate drifted"
        )
    if value.get("expected_active_snapshot_revision") != (
        expected_active_snapshot_revision
    ):
        raise MCPApprovedServerMigrationError(
            "migration provenance active snapshot drifted"
        )
    for key in (
        "project_id", "extension_id", "intake_ref", "artifact_ref",
        "artifact_receipt_ref", "actor", "reason",
    ):
        item = value.get(key)
        if not isinstance(item, str) or not item or len(item) > 2048:
            raise MCPApprovedServerMigrationError(
                "migration provenance identity is invalid"
            )
    for key in (
        "artifact_content_sha256", "manifest_identity", "review_plan_identity",
    ):
        item = value.get(key)
        if (
            not isinstance(item, str)
            or len(item) != 64
            or any(character not in "0123456789abcdef" for character in item)
        ):
            raise MCPApprovedServerMigrationError(
                "migration provenance evidence is invalid"
            )
    confirmations = value.get("confirmation_ids")
    if (
        not isinstance(confirmations, list)
        or not confirmations
        or any(not isinstance(item, str) or not item for item in confirmations)
        or confirmations != sorted(set(confirmations))
    ):
        raise MCPApprovedServerMigrationError(
            "migration provenance confirmations are invalid"
        )
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise MCPApprovedServerMigrationError(
            "migration provenance is not serializable"
        ) from None
    if len(encoded) > 64 * 1024:
        raise MCPApprovedServerMigrationError(
            "migration provenance is too large"
        )
    return reference, encoded


def _require_provenance_contract(
    payload: bytes,
    *,
    active_snapshot_revision: int,
    affected_server_ids: tuple[str, ...],
) -> None:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MCPApprovedServerMigrationError(
            "migration provenance is invalid"
        ) from None
    if (
        value.get("expected_active_snapshot_revision")
        != active_snapshot_revision
        or value.get("affected_server_ids") != list(affected_server_ids)
    ):
        raise MCPApprovedServerMigrationConflict(
            "migration provenance does not match the reviewed change set"
        )


def _candidate_identity(payload: bytes) -> str:
    return f"blake3:{blake3(payload).hexdigest()}"


def _canonical_records(payload: bytes) -> dict[str, bytes]:
    """Compare reviewed records by their canonical stored bytes, not display data."""
    try:
        value = json.loads(payload.decode("utf-8"))
        records = value["servers"]
        if not isinstance(records, list):
            raise ValueError
        result: dict[str, bytes] = {}
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("server_id"), str):
                raise ValueError
            result[record["server_id"]] = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return result
    except (UnicodeDecodeError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        raise MCPApprovedServerMigrationError("MCP approved snapshot is invalid") from None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _command_id(value: str) -> str:
    value = str(value).strip()
    if not _COMMAND_ID.fullmatch(value):
        raise MCPApprovedServerMigrationError("migration command identity is invalid")
    return value


def _migration_id(value: str) -> str:
    value = str(value).strip()
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise MCPApprovedServerMigrationError("migration identity is invalid")
    return value


def _optional_provenance_ref(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _PROVENANCE_REF.fullmatch(value):
        raise MCPApprovedServerMigrationError(
            "migration provenance reference is invalid"
        )
    return value


def _revision(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MCPApprovedServerMigrationError("migration revision is invalid")
    return value
