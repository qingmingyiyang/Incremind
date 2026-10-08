"""Bounded, side-effect-free operations health for fixed Effect partitions."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Protocol

from backend.api.effect_partition_inventory import (
    EFFECT_PARTITION_INVENTORY,
    effect_partition_enabled,
    enabled_effect_partition_specs,
)


class RecoverySnapshotPort(Protocol):
    def snapshot(self) -> dict[str, object]: ...


class RecoveryCoordinatorSnapshotPort(Protocol):
    def partition_names(self) -> tuple[str, ...]: ...
    def expected_partition_names(self) -> tuple[str, ...] | None: ...
    def partition_recovery_snapshot(self) -> tuple[object, ...]: ...


def build_effect_operations_health(
    root_dir: Path | None,
    recovery_service: RecoverySnapshotPort | None,
    *,
    recovery_coordinator: RecoveryCoordinatorSnapshotPort | None = None,
    application_state: object | None = None,
    now: int | None = None,
) -> dict[str, object]:
    """Return fixed aggregate state without constructing a Store or runtime."""

    root = Path(root_dir) if root_dir is not None else None
    registered = set(recovery_coordinator.partition_names()) if recovery_coordinator else set()
    expected = (
        recovery_coordinator.expected_partition_names()
        if recovery_coordinator is not None else None
    )
    enabled = (
        tuple(spec.name for spec in enabled_effect_partition_specs(application_state))
        if application_state is not None else ()
    )
    last_pass_by_partition = {
        str(getattr(item, "partition")): item
        for item in (
            recovery_coordinator.partition_recovery_snapshot()
            if recovery_coordinator is not None else ()
        )
    }
    snapshot_at = int(time.time()) if now is None else int(now)
    partitions = [
        _partition_health(
            root,
            spec,
            configured=(
                application_state is None
                or effect_partition_enabled(spec, application_state)
            ),
            registered=(spec.name in registered if recovery_coordinator is not None else None),
            last_pass=last_pass_by_partition.get(spec.name),
            now=snapshot_at,
        )
        for spec in EFFECT_PARTITION_INVENTORY
    ]
    recovery = recovery_service.snapshot() if recovery_service is not None else {
        "running": False,
        "status": "not_started",
        "last_outcome": "not_run",
        "last_started_at": None,
        "last_completed_at": None,
        "last_failure_type": None,
    }
    inventory = _inventory_registration_payload(
        enabled=enabled,
        registered=registered,
        expected=expected,
        coordinator_present=recovery_coordinator is not None,
    )
    statuses = {str(item["status"]) for item in partitions}
    overall = (
        "degraded"
        if (
            inventory["status"] in {"drift", "unconfigured"}
            or statuses.intersection({"degraded", "unavailable", "invalid"})
        )
        else "attention"
        if "attention" in statuses
        else "ready"
    )
    return {
        "schema_version": "effect-operations-health-v1",
        "status": overall,
        "snapshot_at": snapshot_at,
        "partitions": partitions,
        "inventory": inventory,
        "recovery": recovery,
    }


def _partition_health(
    root: Path | None,
    spec,
    *,
    configured: bool,
    registered: bool | None,
    last_pass: object | None,
    now: int,
) -> dict[str, object]:
    storage = {
        "database_bytes": 0,
        "wal_bytes": 0,
        "shm_bytes": 0,
        "page_bytes": 0,
        "freelist_bytes": 0,
    }
    effects = {
        "planned": 0,
        "inflight": 0,
        "expired_inflight": 0,
        "unknown": 0,
        "terminal": 0,
        "counts_available": False,
    }
    payload: dict[str, object] = {
        "name": spec.name,
        "status": "disabled" if not configured else "missing",
        "configured": configured,
        "registered_with_recovery": registered,
        "last_recovery_pass": _safe_last_pass(last_pass),
        "lease_seconds": spec.lease_seconds,
        "heartbeat_seconds": spec.lease_heartbeat_seconds,
        "storage": storage,
        "effects": effects,
        "schema": {"status": "unavailable", "version": None},
        "error_type": None,
    }
    if not configured:
        return payload
    if root is None:
        payload["status"] = "unavailable"
        return payload
    path = spec.database(root)
    try:
        metadata = path.stat()
    except FileNotFoundError:
        if registered is False:
            payload["status"] = "degraded"
        return payload
    except OSError:
        payload.update(status="unavailable", error_type="os_error")
        return payload
    if not path.is_file():
        payload.update(status="invalid", error_type="not_regular_file")
        return payload
    storage.update(
        database_bytes=max(0, int(metadata.st_size)),
        wal_bytes=_safe_size(Path(f"{path}-wal")),
        shm_bytes=_safe_size(Path(f"{path}-shm")),
    )
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.05,
        )
        try:
            deadline = time.monotonic() + 0.05
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=40")
            connection.set_progress_handler(
                lambda: 1 if time.monotonic() > deadline else 0, 100,
            )
            state_counts = {
                str(state): int(count)
                for state, count in connection.execute(
                    "SELECT state, COUNT(*) FROM effect GROUP BY state"
                ).fetchall()
            }
            expired = int(connection.execute(
                "SELECT COUNT(*) FROM effect WHERE state='INFLIGHT' "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at<=?",
                (now,),
            ).fetchone()[0])
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            freelist = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
            schema_row = connection.execute(
                "SELECT value FROM effect_contract_meta WHERE key='schema_version'"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        payload.update(status="unavailable", error_type="sqlite_error")
        return payload
    except OSError:
        payload.update(status="unavailable", error_type="os_error")
        return payload
    storage.update(
        page_bytes=page_size * page_count,
        freelist_bytes=page_size * freelist,
    )
    effects.update(
        planned=state_counts.get("PLANNED", 0),
        inflight=state_counts.get("INFLIGHT", 0),
        expired_inflight=expired,
        unknown=state_counts.get("UNKNOWN", 0),
        terminal=state_counts.get("SETTLED_OK", 0) + state_counts.get("SETTLED_ERR", 0),
        counts_available=True,
    )
    payload["schema"] = {
        "status": "ready" if schema_row is not None else "unavailable",
        "version": None if schema_row is None else str(schema_row[0]),
    }
    payload["status"] = (
        "degraded"
        if registered is False or schema_row is None
        else "attention"
        if expired or effects["unknown"]
        else "ready"
    )
    return payload


def _inventory_registration_payload(
    *, enabled: tuple[str, ...], registered: set[str], expected: tuple[str, ...] | None,
    coordinator_present: bool,
) -> dict[str, object]:
    """Expose registration drift without runtime, operation, or error detail."""

    expected_names = tuple(sorted(expected if expected is not None else enabled))
    registered_names = tuple(sorted(registered))
    missing = tuple(name for name in expected_names if name not in registered)
    unexpected = tuple(name for name in registered_names if name not in expected_names)
    configured = expected is not None
    return {
        "status": (
            "ready" if coordinator_present and configured and not missing and not unexpected
            else "drift" if coordinator_present and (missing or unexpected)
            else "unconfigured" if coordinator_present else "unavailable"
        ),
        "expected_enabled_names": expected_names,
        "registered_names": registered_names,
        "missing_names": missing,
        "unexpected_names": unexpected,
    }


def _safe_last_pass(value: object | None) -> dict[str, object] | None:
    if value is None:
        return None
    partition = getattr(value, "partition", None)
    count = getattr(value, "recovered_count", None)
    status = getattr(value, "status", None)
    completed_at = getattr(value, "completed_at", None)
    if (
        not isinstance(partition, str)
        or not isinstance(count, int) or isinstance(count, bool) or count < 0
        or status not in {"idle", "recovered"}
        or not isinstance(completed_at, int) or isinstance(completed_at, bool)
    ):
        return None
    return {
        "status": status,
        "recovered_count": count,
        "completed_at": completed_at,
    }


def _safe_size(path: Path) -> int:
    try:
        return max(0, int(path.stat().st_size)) if path.is_file() else 0
    except OSError:
        return 0
