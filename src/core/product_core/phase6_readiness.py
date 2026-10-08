from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from .ports import IndexHealthPort, StorageBoundaryPort


Phase6ReadinessStatus = Literal["ready", "degraded"]

REQUIRED_PHASE6_CAPABILITIES = (
    "app_data_dir",
    "file_picker",
    "backup_destination",
    "worker_lifecycle",
    "system_info",
)


class PlatformCapabilityProbePort(Protocol):
    """Returns platform-neutral capability probe records."""

    def platform_capabilities(self) -> tuple[Mapping[str, object], ...]:
        """Return Platform Capability payloads without exposing OS APIs."""


@dataclass(frozen=True, slots=True)
class Phase6ReadinessCheck:
    name: str
    status: Phase6ReadinessStatus
    detail: str


@dataclass(frozen=True, slots=True)
class Phase6Readiness:
    status: Phase6ReadinessStatus
    checks: tuple[Phase6ReadinessCheck, ...]
    required_capabilities: tuple[str, ...]
    ready_capabilities: tuple[str, ...]


class GetPhase6Readiness:
    """Executable entry gate for Platform / Storage / Index hardening."""

    def __init__(
        self,
        *,
        storage: StorageBoundaryPort,
        index: IndexHealthPort,
        platform: PlatformCapabilityProbePort,
    ) -> None:
        self._storage = storage
        self._index = index
        self._platform = platform

    def execute(self) -> Phase6Readiness:
        boundary = self._storage.storage_boundary()
        index = self._index.index_health()
        capabilities = self._platform.platform_capabilities()
        ready_capabilities = tuple(
            sorted(
                str(capability["name"])
                for capability in capabilities
                if _capability_ready_for_phase6(capability)
            )
        )
        checks = (
            _check(
                "storage_namespace_portable",
                boundary.isolated
                and boundary.backup_ready
                and boundary.app_root_uri.startswith("platform-app-data://")
                and boundary.root_uri == f"crp://{boundary.namespace_id}/"
                and boundary.reference_root_uri == f"crp-ref://{boundary.namespace_id}/",
                "storage namespace is isolated, platform-neutral and backup-ready",
            ),
            _check(
                "legacy_library_protected",
                boundary.isolated and boundary.legacy_access in {"disabled", "read_only"},
                "legacy library remains disabled or read-only behind storage boundary",
            ),
            _check(
                "platform_capability_minimum",
                all(name in ready_capabilities for name in REQUIRED_PHASE6_CAPABILITIES),
                "required Platform Capability probes are present and acceptable",
            ),
            _check(
                "platform_uri_portability",
                bool(capabilities) and all(_platform_capability_uri_portable(item) for item in capabilities),
                "Platform Capability payloads use platform-neutral URIs and no OS paths",
            ),
            _check(
                "persistent_index_ready",
                index.status == "ready"
                and index.manifest_present
                and index.backend_kind == "object_store_lexical"
                and index.entry_count > 0
                and index.traceable
                and not index.vector_enabled,
                "persistent Recall index is traceable, lexical and vector-disabled",
            ),
        )
        return Phase6Readiness(
            status="ready" if all(check.status == "ready" for check in checks) else "degraded",
            checks=checks,
            required_capabilities=REQUIRED_PHASE6_CAPABILITIES,
            ready_capabilities=ready_capabilities,
        )


def _check(name: str, passed: bool, detail: str) -> Phase6ReadinessCheck:
    return Phase6ReadinessCheck(name, "ready" if passed else "degraded", detail)


def _capability_ready_for_phase6(capability: Mapping[str, object]) -> bool:
    name = capability.get("name")
    if name not in REQUIRED_PHASE6_CAPABILITIES:
        return False
    if not _platform_capability_uri_portable(capability):
        return False
    if capability.get("available") is True:
        return capability.get("error") is None and capability.get("permission") != "denied"
    if name == "worker_lifecycle":
        error = capability.get("error")
        return (
            isinstance(error, Mapping)
            and error.get("degradation") == "local_only"
            and error.get("retryable") is True
        )
    return False


def _platform_capability_uri_portable(capability: Mapping[str, object]) -> bool:
    if capability.get("platform") == "unknown" and capability.get("available") is True:
        return False
    uri = capability.get("provided_uri")
    if uri is None:
        return True
    if not isinstance(uri, str):
        return False
    if "\\" in uri or uri.startswith("file:"):
        return False
    return uri.startswith(("platform-app-data://", "platform-documents://", "platform-backup://"))
