"""Read-only HTTP composition for retained Plugin Hands lifecycle attempts."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from core.plugin_hands.durable_lifecycle import (
    PluginHandsDurableLifecycleError,
    PluginHandsLifecycleAuditProjection,
    PluginHandsLifecycleReader,
)
from core.storage_provider import SQLiteStructuredRecordStore


# The route catches this exact alias without importing lifecycle execution
# internals into the governance router.
LifecycleProjectionReadError = PluginHandsDurableLifecycleError


def read_plugin_hands_lifecycle_attempt(
    root_dir: Path,
    attempt_id: str,
) -> PluginHandsLifecycleAuditProjection | None:
    """Observe one attempt from the durable ledger without recovery or replay."""

    root = root_dir.expanduser().resolve(strict=False)
    reader = PluginHandsLifecycleReader(
        SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3")
    )
    return reader.read_attempt(attempt_id)


def serialize_plugin_hands_lifecycle_attempt(
    projection: PluginHandsLifecycleAuditProjection,
) -> dict[str, object]:
    """Return only safe lifecycle facts; no workspace or process details escape."""

    return {
        "schema_version": "1.0.0",
        "attempt_id": projection.attempt_id,
        "limit": 1,
        "replay": False,
        "retention": {
            "record": "durable_while_present",
            "workspace": "not_reported",
        },
        "attempt": asdict(projection),
    }


__all__ = [
    "read_plugin_hands_lifecycle_attempt",
    "serialize_plugin_hands_lifecycle_attempt",
    "LifecycleProjectionReadError",
]
