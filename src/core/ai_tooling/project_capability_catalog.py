"""Read-only, project-scoped projection of registered AI capabilities.

The catalog is intentionally a UI/diagnostic contract.  It receives an
already captured registry snapshot and profile snapshots, and cannot register,
dispatch, or reveal model-facing tool descriptions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Literal, Protocol

from .contracts import LegacyCapabilityView, ToolDefinition, tool_from_capability
from .project_profile import BoundaryMode, EffectiveToolPolicyResolver, ProjectCapabilityProfile


class BoundaryProfileView(Protocol):
    """Narrow structural view; the tooling layer owns no Boundary authority."""

    profile_id: str
    project_id: str
    revision: int
    mode: BoundaryMode


CatalogKind = Literal["tool"]
CatalogState = Literal["available", "unavailable"]


class ProjectCapabilityCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectCapabilityCatalogEntry:
    kind: CatalogKind
    stable_id: str
    display_name: str
    source: str
    owner_id: str
    selected: bool
    state: CatalogState
    reason: str
    revision_identity: dict[str, object]


@dataclass(frozen=True, slots=True)
class ProjectCapabilityCatalog:
    project_id: str
    capability_profile_id: str
    capability_profile_revision: int
    boundary_profile_id: str
    boundary_profile_revision: int
    registry_generation: int
    supported_kinds: tuple[CatalogKind, ...]
    entries: tuple[ProjectCapabilityCatalogEntry, ...]
    excluded_reason_counts: tuple[tuple[str, int], ...]

    def to_public_dict(self) -> dict[str, object]:
        """Return the intentionally narrow API DTO without schemas or prose."""
        return {
            "project_id": self.project_id,
            "capability_profile": {
                "profile_id": self.capability_profile_id,
                "revision": self.capability_profile_revision,
            },
            "boundary_profile": {
                "profile_id": self.boundary_profile_id,
                "revision": self.boundary_profile_revision,
            },
            "registry_generation": self.registry_generation,
            "supported_kinds": list(self.supported_kinds),
            "entries": [asdict(entry) for entry in self.entries],
            # Excluded tools are intentionally anonymous.  A project is never
            # told the stable identity, owner, display name, or revision of a
            # capability that it is not allowed to see.
            "excluded_reason_counts": [
                {"reason": reason, "count": count}
                for reason, count in self.excluded_reason_counts
            ],
        }


class ProjectCapabilityCatalogProjector:
    """Projects a sparse catalog from one registry/profile snapshot set."""

    def __init__(self, resolver: EffectiveToolPolicyResolver | None = None) -> None:
        self._resolver = resolver or EffectiveToolPolicyResolver()

    def project(
        self,
        *,
        profile: ProjectCapabilityProfile,
        boundary: BoundaryProfileView,
        capabilities: Sequence[LegacyCapabilityView],
        registry_generation: int,
    ) -> ProjectCapabilityCatalog:
        _validate_profile_binding(profile, boundary)
        if not isinstance(registry_generation, int) or isinstance(registry_generation, bool) or registry_generation < 0:
            raise ProjectCapabilityCatalogError("registry generation must be non-negative")
        tools = tuple(tool_from_capability(capability) for capability in capabilities)
        resolution = self._resolver.resolve(
            profile,
            tools,
            # A catalog deliberately has no Turn-specific grants.  Passing
            # only actual registry IDs prevents it from expanding planner
            # authority with configured-but-unregistered tool IDs.
            turn_allowed=tuple(tool.tool_id for tool in tools),
            boundary_mode=boundary.mode,
        )
        entries = tuple(
            _entry(tool)
            for tool in resolution.tools
        )
        return ProjectCapabilityCatalog(
            project_id=profile.project_id,
            capability_profile_id=profile.profile_id,
            capability_profile_revision=profile.revision,
            boundary_profile_id=boundary.profile_id,
            boundary_profile_revision=boundary.revision,
            registry_generation=registry_generation,
            supported_kinds=("tool",),
            entries=entries,
            excluded_reason_counts=resolution.excluded_reason_counts,
        )


def _validate_profile_binding(
    profile: ProjectCapabilityProfile,
    boundary: BoundaryProfileView,
) -> None:
    if profile.project_id != boundary.project_id:
        raise ProjectCapabilityCatalogError("project profile identity drifted")
    if profile.boundary_profile_id != boundary.profile_id:
        raise ProjectCapabilityCatalogError("capability and Boundary profile identities do not match")
    if profile.boundary_profile_revision != boundary.revision:
        raise ProjectCapabilityCatalogError("capability and Boundary profile revisions do not match")


def _entry(
    tool: ToolDefinition,
) -> ProjectCapabilityCatalogEntry:
    return ProjectCapabilityCatalogEntry(
        kind="tool",
        stable_id=tool.tool_id,
        display_name=tool.display_name,
        source=tool.source,
        owner_id=tool.owner_id,
        selected=True,
        state="available",
        reason="selected",
        revision_identity=_revision_identity(tool),
    )


def _revision_identity(tool: ToolDefinition) -> dict[str, object]:
    identity: dict[str, object] = {"contract_version": tool.version}
    if tool.connection_identity is not None:
        connection = tool.connection_identity
        identity["mcp"] = {
            "manifest_revision": connection.manifest_revision,
            "transport_generation": connection.transport_generation,
            "catalog_revision": connection.catalog_revision,
            "tool_schema_revision": connection.tool_schema_revision,
        }
    return identity
