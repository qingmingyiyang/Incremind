"""Versioned, executable inventory of local storage ownership.

The manifest is deliberately descriptive: it makes the current mixed physical
layout visible without creating the future TTL/no-TTL directories on a user's
machine.  A migration must change this manifest and its writer contract in the
same review.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


STORAGE_TOPOLOGY_VERSION = "storage-topology-v1"
_TIERS = frozenset({"authority", "derived-ttl", "derived-nottl"})


@dataclass(frozen=True, slots=True)
class StorageTopologyEntry:
    name: str
    tier: str
    relative_path: str
    writer_owner: str
    rebuild_source: str | None
    deletable: bool
    ttl_seconds: int | None = None

    def __post_init__(self) -> None:
        path = PurePosixPath(self.relative_path)
        if (
            not self.name
            or self.tier not in _TIERS
            or path.is_absolute()
            or ".." in path.parts
            or not self.writer_owner
            or (self.tier == "derived-ttl") != (self.ttl_seconds is not None)
            or self.ttl_seconds is not None and self.ttl_seconds <= 0
            or self.tier == "authority" and self.deletable
        ):
            raise ValueError("storage topology entry is invalid")

    def public_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "tier": self.tier,
            "relative_path": self.relative_path,
            "writer_owner": self.writer_owner,
            "rebuild_source": self.rebuild_source,
            "deletable": self.deletable,
            "ttl_seconds": self.ttl_seconds,
        }


# The entries describe current production paths. A path may be absent when its
# capability has never run, but consumers must not invent an unregistered path.
STORAGE_TOPOLOGY = (
    StorageTopologyEntry("effect-primary", "authority", ".rebuild-data/jobs.sqlite3", "effect-log-core", None, False),
    StorageTopologyEntry("effect-ai-turns", "authority", ".rebuild-data/ai-turns.sqlite3", "effect-log-core", None, False),
    StorageTopologyEntry("effect-ppt-master", "authority", ".rebuild-data/ppt-master-effects.sqlite3", "effect-log-core", None, False),
    StorageTopologyEntry("aggregate-authority", "authority", ".rebuild-data/aggregate-authority.sqlite3", "aggregate-repository", None, False),
    StorageTopologyEntry("structured-records", "authority", ".rebuild-data/structured-records.sqlite3", "structured-record-store", None, False),
    StorageTopologyEntry("versioned-objects", "authority", ".rebuild-data/objects", "json-object-store", None, False),
    StorageTopologyEntry("companion", "authority", ".rebuild-data/companion/companion.sqlite3", "companion-core", None, False),
    StorageTopologyEntry("security", "authority", ".rebuild-data/security", "security-authority", None, False),
    StorageTopologyEntry("capability-packages", "authority", ".rebuild-data/capability-packages.sqlite3", "capability-loader", None, False),
    StorageTopologyEntry("capability-artifacts", "authority", ".rebuild-data/capability-artifacts", "capability-loader", None, False),
    StorageTopologyEntry("session-placement", "authority", ".rebuild-data/session-placement", "session-placement-core", None, False),
    StorageTopologyEntry("original-assets", "authority", "library/assets/originals", "original-asset-store", None, False),
    StorageTopologyEntry("recall-indexes", "derived-nottl", ".rebuild-data/recall-indexes", "recall-index-rebuilder", "published-memory-authority", True),
    StorageTopologyEntry("plugin-materializations", "derived-nottl", ".rebuild-data/plugin-hands-materializations", "plugin-hands-runtime", "capability-package-authority", True),
    StorageTopologyEntry("plugin-workspaces", "derived-nottl", ".rebuild-data/plugin-hands-workspaces", "plugin-hands-runtime", "effect-and-receipt-authority", True),
    StorageTopologyEntry("plugin-hook-workspaces", "derived-nottl", ".rebuild-data/plugin-hook-workspaces", "plugin-hook-runtime", "effect-and-receipt-authority", True),
    StorageTopologyEntry("ppt-profile-steps", "derived-nottl", ".rebuild-data/ppt-master-profile-steps", "ppt-master-runtime", "ppt-profile-authority", True),
    StorageTopologyEntry("ppt-jobs", "derived-nottl", ".rebuild-data/ppt-master-jobs", "ppt-master-runtime", "ppt-effect-and-receipt-authority", True),
    StorageTopologyEntry("model-provider-health", "derived-ttl", ".rebuild-data/model-provider-health.sqlite3", "model-provider-health", "provider-route-authority", True, 60),
)


def storage_topology_payload() -> dict[str, object]:
    return {
        "schema_version": STORAGE_TOPOLOGY_VERSION,
        "entries": [entry.public_payload() for entry in STORAGE_TOPOLOGY],
        "materialization": "current-path-contract",
    }
