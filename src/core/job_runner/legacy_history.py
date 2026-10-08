"""Read-only legacy Job history imported during the Scheme D migration.

Legacy Job rows are evidence for display only.  This module deliberately does
not initialise, plan, transition, or query-mutate the Effect/Job projection
tables.  A caller that owns a SQLite upgrade transaction can import a frozen
legacy snapshot here and later render its explicit manual-resolution state.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone


_LEGACY_HISTORY_TABLE = "legacy_job_history"
_IMPORT_FENCE_TABLE = "legacy_job_history_import_fence"


@dataclass(frozen=True, slots=True)
class LegacyJobHistoryRecord:
    """A frozen legacy Job snapshot and its non-executable display state."""

    job_id: str
    payload: Mapping[str, object]
    legacy_revision: int
    imported_at: str
    display_status: str
    requires_manual_resolution: bool
    readonly: bool = True


@dataclass(frozen=True, slots=True)
class LegacyJobHistorySnapshot:
    """One immutable source row supplied by an explicit migration command."""

    source_ref: str
    job_id: str
    payload: Mapping[str, object]
    legacy_revision: int


class LegacyJobHistoryProjection:
    """Import and read legacy Job snapshots without giving them execution power."""

    def exists_in_connection(
        self, connection: sqlite3.Connection, *, job_id: str,
    ) -> bool:
        """Check the immutable identity fence and its execution isolation."""

        return self.validate_job_in_connection(connection, job_id=job_id)

    def validate_job_in_connection(
        self, connection: sqlite3.Connection, *, job_id: str,
    ) -> bool:
        """Fail closed when one frozen identity also has executable authority."""

        _require_connection(connection)
        history = _history_row(connection, job_id=job_id)
        if history is None:
            _reject_orphan_fence(connection, job_id=job_id)
            return False
        _validate_fence_mapping(connection, job_id=job_id, history=history)
        _reject_execution_authority_conflict(connection, job_id)
        return True

    def validate_inventory_in_connection(self, connection: sqlite3.Connection) -> None:
        """Validate every frozen row before a read, rebuild, or new import.

        A database produced by the pre-fence upgrade can contain both frozen
        legacy history and executable Job/Effect rows.  Treat that as an
        upgrade failure, never as an ordering choice between two authorities.
        """

        _require_connection(connection)
        if not _table_exists(connection, _LEGACY_HISTORY_TABLE):
            _reject_any_orphan_fence(connection)
            return
        rows = connection.execute(
            f"SELECT job_id,payload_json,legacy_revision,imported_at FROM {_LEGACY_HISTORY_TABLE}"
        ).fetchall()
        for row in rows:
            job_id = str(_row_value(row, "job_id", 0))
            _validate_fence_mapping(connection, job_id=job_id, history=row)
            _reject_execution_authority_conflict(connection, job_id)
        _reject_any_orphan_fence(connection)
        _reject_source_mapping_drift(connection)

    def import_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        migration_id: str,
        source_kind: str,
        source_ref: str,
        job_id: str,
        payload: Mapping[str, object],
        revision: int,
        imported_at: str | None = None,
    ) -> LegacyJobHistoryRecord:
        """Freeze one legacy snapshot in the caller-owned transaction.

        An exact replay is a no-op.  Any changed payload or revision for the
        same id fails closed, as does a collision with any existing executable
        Job fact, Effect root, or operation id. This API never begins, commits,
        or rolls back a transaction, so upgrade orchestration retains atomicity.
        """

        _require_connection(connection)
        _validate_import_identity(
            migration_id=migration_id,
            source_kind=source_kind,
            source_ref=source_ref,
        )
        _validate_snapshot(job_id=job_id, payload=payload, revision=revision)
        initialize_legacy_job_history_schema(connection)
        self.validate_inventory_in_connection(connection)
        _reject_execution_authority_conflict(connection, job_id)
        canonical_payload = _canonical_payload(payload)
        frozen_at = imported_at or datetime.now(timezone.utc).isoformat()
        existing = connection.execute(
            f"SELECT payload_json,legacy_revision,imported_at FROM {_LEGACY_HISTORY_TABLE} WHERE job_id=?",
            (job_id,),
        ).fetchone()
        fence = connection.execute(
            f"SELECT job_id,legacy_revision FROM {_IMPORT_FENCE_TABLE} "
            "WHERE migration_id=? AND source_kind=? AND source_ref=?",
            (migration_id, source_kind, source_ref),
        ).fetchone()
        source_mapping = connection.execute(
            f"SELECT job_id FROM {_IMPORT_FENCE_TABLE} "
            "WHERE source_kind=? AND source_ref=? LIMIT 1",
            (source_kind, source_ref),
        ).fetchone()
        if source_mapping is not None and str(source_mapping["job_id"]) != job_id:
            raise ValueError("legacy Job history source identity drifted")
        if existing is not None and (
            str(existing["payload_json"]) != canonical_payload
            or int(existing["legacy_revision"]) != revision
        ):
            raise ValueError("legacy Job history import drifted")
        if fence is not None and (
            str(fence["job_id"]) != job_id
            or int(fence["legacy_revision"]) != revision
        ):
            raise ValueError("legacy Job history import fence drifted")
        if existing is None and fence is not None:
            raise ValueError("legacy Job history import fence is inconsistent")

        if existing is None:
            connection.execute(
                f"INSERT INTO {_LEGACY_HISTORY_TABLE}(job_id,payload_json,legacy_revision,imported_at) VALUES(?,?,?,?)",
                (job_id, canonical_payload, revision, frozen_at),
            )
        if fence is None:
            connection.execute(
                f"INSERT INTO {_IMPORT_FENCE_TABLE}("
                "migration_id,source_kind,source_ref,job_id,legacy_revision,imported_at"
                ") VALUES(?,?,?,?,?,?)",
                (migration_id, source_kind, source_ref, job_id, revision, frozen_at),
            )
        row = connection.execute(
            f"SELECT payload_json,legacy_revision,imported_at FROM {_LEGACY_HISTORY_TABLE} WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("legacy Job history import was not persisted")
        return _record(job_id, row)

    def read_in_connection(
        self, connection: sqlite3.Connection, *, job_id: str,
    ) -> LegacyJobHistoryRecord:
        """Return a display-only snapshot, or raise ``KeyError`` when absent."""

        _require_connection(connection)
        if not _table_exists(connection, _LEGACY_HISTORY_TABLE):
            raise KeyError(job_id)
        row = _history_row(connection, job_id=job_id)
        if row is None:
            _reject_orphan_fence(connection, job_id=job_id)
            raise KeyError(job_id)
        _validate_fence_mapping(connection, job_id=job_id, history=row)
        _reject_execution_authority_conflict(connection, job_id)
        return _record(job_id, row)

    def all_in_connection(
        self, connection: sqlite3.Connection,
    ) -> tuple[LegacyJobHistoryRecord, ...]:
        """Return every frozen history record without mutating the database."""

        _require_connection(connection)
        if not _table_exists(connection, _LEGACY_HISTORY_TABLE):
            _reject_any_orphan_fence(connection)
            return ()
        self.validate_inventory_in_connection(connection)
        rows = connection.execute(
            f"SELECT job_id,payload_json,legacy_revision,imported_at "
            f"FROM {_LEGACY_HISTORY_TABLE} ORDER BY job_id"
        ).fetchall()
        records: list[LegacyJobHistoryRecord] = []
        for row in rows:
            job_id = str(_row_value(row, "job_id", 0))
            records.append(_record(job_id, row))
        return tuple(records)


def initialize_legacy_job_history_schema(connection: sqlite3.Connection) -> None:
    """Create only the isolated legacy-history tables, never execution tables."""

    _require_connection(connection)
    # ``executescript`` commits any active SQLite transaction before it runs;
    # each statement must remain transaction-bound for upgrade rollback.
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {_LEGACY_HISTORY_TABLE} ("
        "job_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, "
        "legacy_revision INTEGER NOT NULL CHECK(legacy_revision > 0), "
        "imported_at TEXT NOT NULL)"
    )
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {_IMPORT_FENCE_TABLE} ("
        "migration_id TEXT NOT NULL, source_kind TEXT NOT NULL, source_ref TEXT NOT NULL, "
        f"job_id TEXT NOT NULL REFERENCES {_LEGACY_HISTORY_TABLE}(job_id), "
        "legacy_revision INTEGER NOT NULL CHECK(legacy_revision > 0), imported_at TEXT NOT NULL, "
        "PRIMARY KEY(migration_id,source_kind,source_ref), "
        "UNIQUE(migration_id,job_id))"
    )


def legacy_job_history_display_payload(
    record: LegacyJobHistoryRecord,
    *,
    history_source: str = "legacy_job_history",
) -> dict[str, object]:
    """Expose a compatibility DTO that cannot authorize execution."""

    payload = dict(record.payload)
    payload["legacy_status"] = payload.get("status")
    payload["status"] = record.display_status
    payload["display_status"] = record.display_status
    payload["execution_version"] = "legacy-v1-readonly"
    payload["history_source"] = history_source
    payload["lease"] = None
    payload["execution_action"] = {
        "kind": (
            "manual_confirmation_required"
            if record.requires_manual_resolution
            else "none"
        ),
        "enabled": False,
        "reason": "legacy_history",
    }
    return payload


def legacy_job_readonly_payload(
    payload: Mapping[str, object],
    *,
    history_source: str,
) -> dict[str, object]:
    """Render an unimported legacy source without writing migration state."""

    job_id = payload.get("id")
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("legacy Job history payload identity is invalid")
    canonical = _canonical_payload(payload)
    row = {
        "payload_json": canonical,
        "legacy_revision": 1,
        "imported_at": "not-imported",
    }
    return legacy_job_history_display_payload(
        _record(job_id, row), history_source=history_source,
    )


def _reject_execution_authority_conflict(
    connection: sqlite3.Connection, job_id: str,
) -> None:
    for table in ("job_effect_fact", "job_effect_node"):
        if not _table_exists(connection, table):
            continue
        conflict = connection.execute(
            f"SELECT 1 FROM {table} WHERE job_id=? LIMIT 1", (job_id,),
        ).fetchone()
        if conflict is not None:
            raise ValueError("legacy Job history conflicts with executable Job authority")
    if not _table_exists(connection, "effect"):
        return
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(effect)")}
    if not {"operation_id", "root_id"}.issubset(columns):
        return
    conflict = connection.execute(
        "SELECT 1 FROM effect WHERE operation_id=? OR root_id=? LIMIT 1",
        (job_id, job_id),
    ).fetchone()
    if conflict is not None:
        raise ValueError("legacy Job history conflicts with executable Job authority")


def _history_row(
    connection: sqlite3.Connection, *, job_id: str,
) -> sqlite3.Row | None:
    if not _table_exists(connection, _LEGACY_HISTORY_TABLE):
        return None
    return connection.execute(
        f"SELECT job_id,payload_json,legacy_revision,imported_at "
        f"FROM {_LEGACY_HISTORY_TABLE} WHERE job_id=?",
        (job_id,),
    ).fetchone()


def _validate_fence_mapping(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    history: Mapping[str, object],
) -> None:
    if not _table_exists(connection, _IMPORT_FENCE_TABLE):
        raise ValueError("legacy Job history import fence is inconsistent")
    rows = connection.execute(
        f"SELECT migration_id,source_kind,source_ref,job_id,legacy_revision "
        f"FROM {_IMPORT_FENCE_TABLE} WHERE job_id=?",
        (job_id,),
    ).fetchall()
    if not rows:
        raise ValueError("legacy Job history import fence is inconsistent")
    revision = int(_row_value(history, "legacy_revision", 2))
    for row in rows:
        if (
            str(_row_value(row, "job_id", 3)) != job_id
            or int(_row_value(row, "legacy_revision", 4)) != revision
        ):
            raise ValueError("legacy Job history import fence is inconsistent")


def _reject_orphan_fence(connection: sqlite3.Connection, *, job_id: str) -> None:
    if not _table_exists(connection, _IMPORT_FENCE_TABLE):
        return
    fence = connection.execute(
        f"SELECT 1 FROM {_IMPORT_FENCE_TABLE} WHERE job_id=? LIMIT 1", (job_id,),
    ).fetchone()
    if fence is not None:
        raise ValueError("legacy Job history import fence is inconsistent")


def _reject_any_orphan_fence(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, _IMPORT_FENCE_TABLE):
        return
    if not _table_exists(connection, _LEGACY_HISTORY_TABLE):
        fence = connection.execute(
            f"SELECT 1 FROM {_IMPORT_FENCE_TABLE} LIMIT 1"
        ).fetchone()
    else:
        fence = connection.execute(
            f"SELECT 1 FROM {_IMPORT_FENCE_TABLE} AS fence "
            f"LEFT JOIN {_LEGACY_HISTORY_TABLE} AS history "
            "ON history.job_id=fence.job_id WHERE history.job_id IS NULL LIMIT 1"
        ).fetchone()
    if fence is not None:
        raise ValueError("legacy Job history import fence is inconsistent")


def _reject_source_mapping_drift(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, _IMPORT_FENCE_TABLE):
        return
    drift = connection.execute(
        f"SELECT 1 FROM {_IMPORT_FENCE_TABLE} "
        "GROUP BY source_kind,source_ref HAVING COUNT(DISTINCT job_id) > 1 LIMIT 1"
    ).fetchone()
    if drift is not None:
        raise ValueError("legacy Job history source identity drifted")


def _record(job_id: str, row: Mapping[str, object]) -> LegacyJobHistoryRecord:
    payload = json.loads(str(_row_value(row, "payload_json", 1)))
    if not isinstance(payload, dict):
        raise ValueError("stored legacy Job history payload is invalid")
    status = payload.get("status")
    if status in {"completed", "failed", "cancelled"}:
        display_status, manual = str(status), False
    else:
        # Pending, running, waiting and malformed legacy states have no
        # executable interpretation after migration.
        display_status, manual = "legacy_unknown", True
    return LegacyJobHistoryRecord(
        job_id=job_id,
        payload=dict(payload),
        legacy_revision=int(_row_value(row, "legacy_revision", 2)),
        imported_at=str(_row_value(row, "imported_at", 3)),
        display_status=display_status,
        requires_manual_resolution=manual,
    )


def _validate_snapshot(*, job_id: str, payload: Mapping[str, object], revision: int) -> None:
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("legacy Job history requires a non-empty job_id")
    if not isinstance(payload, Mapping) or dict(payload).get("id") != job_id:
        raise ValueError("legacy Job history payload identity is invalid")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
        raise ValueError("legacy Job history revision must be positive")
    if payload.get("execution_version") == "effect-v2":
        raise ValueError("effect-v2 Job cannot be imported as legacy history")


def _validate_import_identity(
    *, migration_id: str, source_kind: str, source_ref: str,
) -> None:
    for label, value in (
        ("migration_id", migration_id),
        ("source_kind", source_kind),
        ("source_ref", source_ref),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"legacy Job history {label} must be non-empty")


def _canonical_payload(payload: Mapping[str, object]) -> str:
    try:
        return json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("legacy Job history payload must be JSON serializable") from exc


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,),
    ).fetchone() is not None


def _require_connection(connection: sqlite3.Connection) -> None:
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a SQLite connection")


def _row_value(row: Mapping[str, object] | tuple[object, ...], name: str, index: int) -> object:
    """Read sqlite rows from both store-bound tuples and row-factory mappings."""

    if isinstance(row, sqlite3.Row):
        return row[name]
    if isinstance(row, Mapping):
        return row[name]
    return row[index]
