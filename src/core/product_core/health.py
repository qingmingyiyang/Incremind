from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .ports import (
    ContractCatalogPort,
    IndexHealth,
    IndexHealthPort,
    PlatformHealth,
    PlatformHealthPort,
    StorageBoundaryPort,
)


REQUIRED_CONTRACTS = (
    "answer_feedback.schema.json",
    "asset.schema.json",
    "atom.schema.json",
    "document.schema.json",
    "document_revision.schema.json",
    "job.schema.json",
    "memory_candidate.schema.json",
    "memory_transition.schema.json",
    "model_request.schema.json",
    "model_result.schema.json",
    "persona.schema.json",
    "platform_capability.schema.json",
    "platform_recovery_action.schema.json",
    "project.schema.json",
    "project_skill.schema.json",
    "question_response.schema.json",
    "recall_request.schema.json",
    "recall_result.schema.json",
    "scenario.schema.json",
    "series_memory.schema.json",
    "source.schema.json",
    "storage_namespace.schema.json",
)


@dataclass(frozen=True, slots=True)
class ProductHealth:
    status: Literal["ready", "degraded"]
    contract_count: int
    missing_contracts: tuple[str, ...]
    storage_isolated: bool
    legacy_access: Literal["disabled", "read_only"]
    namespace_id: str
    storage_version: int
    root_uri: str
    reference_root_uri: str
    backup_ready: bool
    index_status: str
    index_manifest_present: bool
    index_backend_kind: str | None
    index_entry_count: int
    index_traceable: bool
    index_vector_enabled: bool
    platform_status: str
    platform_capability_count: int
    platform_ready_capabilities: tuple[str, ...]
    platform_degraded_capabilities: tuple[str, ...]
    platform_missing_capabilities: tuple[str, ...]
    platform_os_path_leaks: tuple[str, ...]


class GetProductHealth:
    """Pure health use case for the rebuild composition root."""

    def __init__(
        self,
        contracts: ContractCatalogPort,
        storage: StorageBoundaryPort,
        index: IndexHealthPort | None = None,
        platform: PlatformHealthPort | None = None,
    ) -> None:
        self._contracts = contracts
        self._storage = storage
        self._index = index
        self._platform = platform

    def execute(self) -> ProductHealth:
        names = frozenset(self._contracts.contract_names())
        missing = tuple(name for name in REQUIRED_CONTRACTS if name not in names)
        boundary = self._storage.storage_boundary()
        index = self._index_health()
        platform = self._platform_health()
        namespace_ready = (
            bool(boundary.namespace_id)
            and boundary.storage_version >= 1
            and boundary.app_root_uri.startswith("platform-app-data://")
            and boundary.root_uri == f"crp://{boundary.namespace_id}/"
            and boundary.reference_root_uri == f"crp-ref://{boundary.namespace_id}/"
            and boundary.backup_ready
        )
        ready = (
            not missing
            and boundary.isolated
            and boundary.legacy_access in {"disabled", "read_only"}
            and namespace_ready
            and index.status == "ready"
            and platform.status == "ready"
        )
        return ProductHealth(
            status="ready" if ready else "degraded",
            contract_count=len(names),
            missing_contracts=missing,
            storage_isolated=boundary.isolated,
            legacy_access=boundary.legacy_access,
            namespace_id=boundary.namespace_id,
            storage_version=boundary.storage_version,
            root_uri=boundary.root_uri,
            reference_root_uri=boundary.reference_root_uri,
            backup_ready=boundary.backup_ready,
            index_status=index.status,
            index_manifest_present=index.manifest_present,
            index_backend_kind=index.backend_kind,
            index_entry_count=index.entry_count,
            index_traceable=index.traceable,
            index_vector_enabled=index.vector_enabled,
            platform_status=platform.status,
            platform_capability_count=platform.capability_count,
            platform_ready_capabilities=platform.ready_capabilities,
            platform_degraded_capabilities=platform.degraded_capabilities,
            platform_missing_capabilities=platform.missing_capabilities,
            platform_os_path_leaks=platform.os_path_leaks,
        )

    def _index_health(self) -> IndexHealth:
        if self._index is None:
            return IndexHealth(
                status="ready",
                manifest_present=True,
                backend_kind=None,
                entry_count=0,
                traceable=True,
                vector_enabled=False,
            )
        return self._index.index_health()

    def _platform_health(self) -> PlatformHealth:
        if self._platform is None:
            return PlatformHealth(
                status="ready",
                capability_count=0,
                ready_capabilities=(),
                degraded_capabilities=(),
                missing_capabilities=(),
                os_path_leaks=(),
            )
        return self._platform.platform_health()
