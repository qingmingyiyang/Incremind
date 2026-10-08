"""Read-only A06 governance inventory for historical Job and Effect metadata.

This module deliberately opens a Vault SQLite file in ``mode=ro`` and selects
only identity, lifecycle and receipt-presence metadata.  It never loads legacy
payload bodies, starts an Effect, or changes a historical row.  A dry-run plan
is a review artifact: an explicit rebuild remains a separate new admission and
must retain the old identity as provenance.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path


class HistoryCategory(StrEnum):
    LEGACY = "legacy"
    EFFECT_V2 = "effect-v2"
    UNKNOWN = "unknown"
    FAILED = "failed"
    VERIFIED_SUCCESS = "verified-success"


class GovernanceAction(StrEnum):
    RETAIN_LEGACY_READONLY = "retain-legacy-readonly"
    OBSERVE_EFFECT_V2 = "observe-existing-effect-v2"
    RETAIN_UNKNOWN_NO_RESEND = "retain-unknown-no-resend"
    RETAIN_FAILED_FOR_DIAGNOSIS = "retain-failed-for-diagnosis"
    RETAIN_VERIFIED_SUCCESS = "retain-verified-success"


@dataclass(frozen=True, slots=True)
class HistoryMetadata:
    """A content-free historical identity used by the governance UI."""

    identity: str
    source: str
    kind: str | None
    lifecycle_state: str | None
    contract_version: str
    receipt_evidence_present: bool
    category: HistoryCategory


@dataclass(frozen=True, slots=True)
class GovernancePlanItem:
    identity: str
    source: str
    category: HistoryCategory
    action: GovernanceAction
    auto_resend: bool = False


@dataclass(frozen=True, slots=True)
class HistoryGovernanceDryRun:
    """An immutable no-write review plan for one metadata inventory."""

    items: tuple[GovernancePlanItem, ...]

    @property
    def counts(self) -> Mapping[HistoryCategory, int]:
        return {category: sum(item.category is category for item in self.items) for category in HistoryCategory}


@dataclass(frozen=True, slots=True)
class ExplicitRebuildLink:
    """A proposed new admission; this object cannot execute or enqueue work."""

    old_identity: str
    new_request_identity: str
    source: str
    reason: str
    action: str = "require-explicit-new-admission"


class HistoryGovernanceDisplay:
    """Reversible in-memory display overlay, never a Vault mutation."""

    def __init__(self, inventory: Iterable[HistoryMetadata]) -> None:
        self._baseline = {_display_key(item.source, item.identity): item for item in inventory}
        self._display = dict(self._baseline)

    def apply(self, dry_run: HistoryGovernanceDryRun) -> tuple[GovernancePlanItem, ...]:
        for item in dry_run.items:
            if _display_key(item.source, item.identity) not in self._baseline:
                raise KeyError(item.identity)
        return dry_run.items

    def visible(self) -> tuple[HistoryMetadata, ...]:
        return tuple(self._display[key] for key in self._baseline)

    def set_display_category(
        self, *, source: str, identity: str, category: HistoryCategory,
    ) -> HistoryMetadata:
        """Apply a local display-only review decision that ``rollback`` can undo."""

        key = _display_key(source, identity)
        current = self._display.get(key)
        if current is None:
            raise KeyError(identity)
        updated = replace(current, category=category)
        self._display[key] = updated
        return updated

    def rollback(self) -> tuple[HistoryMetadata, ...]:
        self._display = dict(self._baseline)
        return self.visible()


def scan_vault_history_metadata(vault_database: Path) -> tuple[HistoryMetadata, ...]:
    """Read the known history tables through a SQLite read-only handle only."""

    path = Path(vault_database).expanduser().resolve(strict=True)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        return scan_history_metadata_in_connection(connection)
    finally:
        connection.close()


def scan_history_metadata_in_connection(connection: sqlite3.Connection) -> tuple[HistoryMetadata, ...]:
    """Classify only metadata from a caller-provided query-only connection."""

    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise ValueError("history governance requires a query-only SQLite connection")
    inventory: list[HistoryMetadata] = []
    if _table_exists(connection, "legacy_job_history"):
        inventory.extend(_scan_legacy_job_history(connection))
    if _table_exists(connection, "effect"):
        inventory.extend(_scan_effects(connection))
    return tuple(sorted(inventory, key=lambda item: (item.source, item.identity)))


def build_history_governance_dry_run(
    inventory: Iterable[HistoryMetadata],
) -> HistoryGovernanceDryRun:
    """Map classifications to non-executable governance actions."""

    actions = {
        HistoryCategory.LEGACY: GovernanceAction.RETAIN_LEGACY_READONLY,
        HistoryCategory.EFFECT_V2: GovernanceAction.OBSERVE_EFFECT_V2,
        HistoryCategory.UNKNOWN: GovernanceAction.RETAIN_UNKNOWN_NO_RESEND,
        HistoryCategory.FAILED: GovernanceAction.RETAIN_FAILED_FOR_DIAGNOSIS,
        HistoryCategory.VERIFIED_SUCCESS: GovernanceAction.RETAIN_VERIFIED_SUCCESS,
    }
    items = tuple(
        GovernancePlanItem(item.identity, item.source, item.category, actions[item.category])
        for item in sorted(inventory, key=lambda value: (value.source, value.identity))
    )
    if any(item.auto_resend for item in items):  # defensive contract guard
        raise RuntimeError("history governance may not auto-resend")
    return HistoryGovernanceDryRun(items)


def plan_explicit_rebuild(
    item: HistoryMetadata,
    *,
    new_request_identity: str,
    reason: str,
) -> ExplicitRebuildLink:
    """Prepare provenance for a separate new admission without executing it."""

    if item.category in {HistoryCategory.EFFECT_V2, HistoryCategory.VERIFIED_SUCCESS}:
        raise ValueError("historical item is not eligible for an explicit rebuild")
    if not new_request_identity.strip() or new_request_identity == item.identity:
        raise ValueError("explicit rebuild requires a distinct new request identity")
    if not reason.strip():
        raise ValueError("explicit rebuild requires a reason")
    return ExplicitRebuildLink(
        old_identity=item.identity,
        new_request_identity=new_request_identity,
        source=item.source,
        reason=reason,
    )


def _scan_legacy_job_history(connection: sqlite3.Connection) -> tuple[HistoryMetadata, ...]:
    columns = _columns(connection, "legacy_job_history")
    required = {"job_id", "payload_json"}
    if not required.issubset(columns):
        return ()
    rows = connection.execute(
        "SELECT job_id, CASE WHEN json_valid(payload_json) "
        "THEN json_extract(payload_json, '$.status') ELSE NULL END AS lifecycle_state "
        "FROM legacy_job_history ORDER BY job_id"
    ).fetchall()
    return tuple(
        _classify(
            identity=str(row["job_id"]),
            source="legacy_job_history",
            kind=None,
            lifecycle_state=_optional_string(row["lifecycle_state"]),
            contract_version="legacy-v1-readonly",
            receipt_evidence_present=False,
        )
        for row in rows
    )


def _scan_effects(connection: sqlite3.Connection) -> tuple[HistoryMetadata, ...]:
    columns = _columns(connection, "effect")
    required = {"operation_id", "state"}
    if not required.issubset(columns):
        return ()
    version = "contract_version" if "contract_version" in columns else "'legacy-v1'"
    kind = "kind" if "kind" in columns else "NULL"
    receipt = (
        "CASE WHEN result_ref IS NOT NULL AND length(trim(result_ref)) > 0 THEN 1 ELSE 0 END"
        if "result_ref" in columns else "0"
    )
    rows = connection.execute(
        f"SELECT operation_id,{kind} AS kind,state,{version} AS contract_version,"
        f"{receipt} AS receipt_evidence_present FROM effect ORDER BY operation_id"
    ).fetchall()
    return tuple(
        _classify(
            identity=str(row["operation_id"]),
            source="effect",
            kind=_optional_string(row["kind"]),
            lifecycle_state=_optional_string(row["state"]),
            contract_version=_optional_string(row["contract_version"]) or "legacy-v1",
            receipt_evidence_present=bool(row["receipt_evidence_present"]),
        )
        for row in rows
    )


def _classify(
    *, identity: str, source: str, kind: str | None, lifecycle_state: str | None,
    contract_version: str, receipt_evidence_present: bool,
) -> HistoryMetadata:
    state = (lifecycle_state or "").upper()
    if state == "SETTLED_OK" and receipt_evidence_present:
        category = HistoryCategory.VERIFIED_SUCCESS
    elif state in {"FAILED", "CANCELLED", "SETTLED_ERR", "COMPENSATED", "ABANDONED"}:
        category = HistoryCategory.FAILED
    elif state in {"UNKNOWN", "PENDING", "RUNNING", "WAITING_USER", ""}:
        category = HistoryCategory.UNKNOWN
    elif state in {"PLANNED", "INFLIGHT"}:
        category = HistoryCategory.EFFECT_V2 if contract_version == "effect-v2" else HistoryCategory.UNKNOWN
    elif contract_version == "effect-v2":
        category = HistoryCategory.EFFECT_V2
    else:
        category = HistoryCategory.LEGACY
    return HistoryMetadata(identity, source, kind, lifecycle_state, contract_version, receipt_evidence_present, category)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _optional_string(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _display_key(source: str, identity: str) -> tuple[str, str]:
    return source, identity
