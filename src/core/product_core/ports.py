from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol


LegacyAccess = Literal["disabled", "read_only"]


class ContractCatalogPort(Protocol):
    """Lists the rebuild contracts available to ProductCore."""

    def contract_names(self) -> tuple[str, ...]:
        """Return contract file names without loading product data."""


class ObjectStorePort(Protocol):
    """Stores Product Core's versioned structured objects."""

    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None:
        """Read one object."""

    def read_including_deleted(self, collection: str, object_id: str) -> Mapping[str, object] | None:
        """Read one object for an explicit lifecycle recovery operation."""

    def list(self, collection: str) -> Sequence[Mapping[str, object]]:
        """List objects in deterministic repository order."""

    def delete(self, collection: str, object_id: str) -> bool:
        """Delete one object and return whether it existed."""

    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int:
        """Write one object and return the new revision."""

    def revision(self, collection: str, object_id: str) -> int:
        """Return the current CAS revision without exposing storage paths."""


@dataclass(frozen=True, slots=True)
class StorageBoundary:
    rebuild_root: str
    legacy_root: str
    legacy_access: LegacyAccess
    isolated: bool
    namespace_id: str
    storage_version: int
    app_root_uri: str
    root_uri: str
    reference_root_uri: str
    backup_ready: bool


class StorageBoundaryPort(Protocol):
    """Describes storage boundaries without creating directories."""

    def storage_boundary(self) -> StorageBoundary:
        """Return the configured new and legacy storage relationship."""


IndexReadinessStatus = Literal["ready", "degraded"]
PlatformReadinessStatus = Literal["ready", "degraded"]


@dataclass(frozen=True, slots=True)
class IndexHealth:
    status: IndexReadinessStatus
    manifest_present: bool
    backend_kind: str | None
    entry_count: int
    traceable: bool
    vector_enabled: bool


class IndexHealthPort(Protocol):
    """Reports Recall index readiness without coupling Product Core to storage."""

    def index_health(self) -> IndexHealth:
        """Return the current persistent Recall index health summary."""


@dataclass(frozen=True, slots=True)
class PlatformHealth:
    status: PlatformReadinessStatus
    capability_count: int
    ready_capabilities: tuple[str, ...]
    degraded_capabilities: tuple[str, ...]
    missing_capabilities: tuple[str, ...]
    os_path_leaks: tuple[str, ...]


class PlatformHealthPort(Protocol):
    """Reports platform capability health without exposing OS-specific APIs."""

    def platform_health(self) -> PlatformHealth:
        """Return the current platform capability health summary."""
