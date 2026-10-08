from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from .contracts import ToolDefinition, tool_contract_binding_identity


BoundaryMode = Literal["open", "guarded", "sealed"]
MemoryScope = Literal["project_only", "project_plus_grants"]
ModelTier = Literal["fast", "standard", "deep", "vision", "image_generation"]
ToolDiscoveryPolicy = Literal["auto_discover", "confirm_new", "disabled"]

_SOURCES = frozenset({"core", "platform", "plugin", "mcp", "workflow"})
_BOUNDARY_MODES = frozenset({"open", "guarded", "sealed"})
_MEMORY_SCOPES = frozenset({"project_only", "project_plus_grants"})
_MODEL_TIERS = frozenset({"fast", "standard", "deep", "vision", "image_generation"})
_DISCOVERY_POLICIES = frozenset({"auto_discover", "confirm_new", "disabled"})
_CONTRACT_BINDING = re.compile(r"^contract-sha256:[0-9a-f]{64}$")
_MCP_PROTOCOL_PROFILES = frozenset({"legacy_2025_11_25", "stateless_2026_07_28"})
_MCP_PROTOCOL_VERSIONS = {
    "legacy_2025_11_25": "2025-11-25",
    "stateless_2026_07_28": "2026-07-28",
}
MAX_MODEL_VISIBLE_TOOLS = 12


class ProjectCapabilityProfileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ToolSelectionBinding:
    stable_id: str
    contract_identity: str

    def __post_init__(self) -> None:
        _require_text(self.stable_id, "selection binding stable id")
        if not isinstance(self.contract_identity, str) or not _CONTRACT_BINDING.fullmatch(
            self.contract_identity
        ):
            raise ProjectCapabilityProfileError(
                "selection binding contract identity is invalid"
            )


@dataclass(frozen=True, slots=True)
class MCPServerSelectionBinding:
    """Non-secret Project attestation for one reviewed MCP server identity."""

    server_id: str
    protocol_profile: str
    manifest_revision: int
    endpoint_identity: str
    credential_subject_id: str
    transport_generation: int

    def __post_init__(self) -> None:
        for label, value in (
            ("MCP server id", self.server_id),
            ("MCP endpoint identity", self.endpoint_identity),
            ("MCP credential subject id", self.credential_subject_id),
        ):
            _require_text(value, label)
        if self.protocol_profile not in _MCP_PROTOCOL_PROFILES:
            raise ProjectCapabilityProfileError("MCP protocol profile is unsupported")
        for label, value in (
            ("MCP manifest revision", self.manifest_revision),
            ("MCP transport generation", self.transport_generation),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ProjectCapabilityProfileError(f"{label} must be positive")


@dataclass(frozen=True, slots=True)
class ProjectCapabilityProfile:
    profile_id: str
    project_id: str
    revision: int
    boundary_profile_id: str
    boundary_profile_revision: int
    enabled_sources: tuple[str, ...]
    enabled_skill_ids: tuple[str, ...]
    enabled_plugin_ids: tuple[str, ...]
    enabled_mcp_server_ids: tuple[str, ...]
    allowed_tool_ids: tuple[str, ...]
    denied_tool_ids: tuple[str, ...]
    preferred_model_tier: ModelTier
    memory_scope: MemoryScope
    cross_project_grant_ids: tuple[str, ...]
    output_style_profile_id: str | None
    max_tools: int = MAX_MODEL_VISIBLE_TOOLS
    max_tool_descriptor_bytes: int = 32 * 1024
    tool_discovery_policy: ToolDiscoveryPolicy = "auto_discover"
    tool_selection_bindings: tuple[ToolSelectionBinding, ...] = ()
    mcp_server_selection_bindings: tuple[MCPServerSelectionBinding, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.profile_id, "profile_id")
        _require_text(self.project_id, "project_id")
        _require_text(self.boundary_profile_id, "boundary_profile_id")
        if not isinstance(self.boundary_profile_revision, int) or isinstance(self.boundary_profile_revision, bool) or self.boundary_profile_revision < 1:
            raise ProjectCapabilityProfileError("boundary profile revision must be positive")
        if self.revision < 1:
            raise ProjectCapabilityProfileError("profile revision must be positive")
        _unique_names(self.enabled_sources, "enabled source")
        if any(source not in _SOURCES for source in self.enabled_sources):
            raise ProjectCapabilityProfileError("enabled source is unsupported")
        for values, label in (
            (self.enabled_skill_ids, "skill"),
            (self.enabled_plugin_ids, "plugin"),
            (self.enabled_mcp_server_ids, "MCP server"),
            (self.allowed_tool_ids, "allowed tool"),
            (self.denied_tool_ids, "denied tool"),
            (self.cross_project_grant_ids, "cross-project grant"),
        ):
            _unique_names(values, label)
        if set(self.allowed_tool_ids) & set(self.denied_tool_ids):
            raise ProjectCapabilityProfileError("tool cannot be both allowed and denied")
        if self.preferred_model_tier not in _MODEL_TIERS:
            raise ProjectCapabilityProfileError("preferred model tier is unsupported")
        if self.memory_scope not in _MEMORY_SCOPES:
            raise ProjectCapabilityProfileError("memory scope is unsupported")
        if self.memory_scope == "project_only" and self.cross_project_grant_ids:
            raise ProjectCapabilityProfileError("project-only memory cannot carry cross-project grants")
        if self.output_style_profile_id is not None:
            _require_text(self.output_style_profile_id, "output_style_profile_id")
        if self.max_tools < 1 or self.max_tools > MAX_MODEL_VISIBLE_TOOLS:
            raise ProjectCapabilityProfileError("max_tools is outside the supported range")
        if self.max_tool_descriptor_bytes < 1024 or self.max_tool_descriptor_bytes > 262_144:
            raise ProjectCapabilityProfileError("tool descriptor budget is outside the supported range")
        if self.tool_discovery_policy not in _DISCOVERY_POLICIES:
            raise ProjectCapabilityProfileError("tool discovery policy is unsupported")
        binding_ids = tuple(item.stable_id for item in self.tool_selection_bindings)
        if len(binding_ids) != len(set(binding_ids)):
            raise ProjectCapabilityProfileError(
                "tool selection binding identities must be unique"
            )
        mcp_binding_ids = tuple(item.server_id for item in self.mcp_server_selection_bindings)
        if len(mcp_binding_ids) != len(set(mcp_binding_ids)):
            raise ProjectCapabilityProfileError(
                "MCP server selection binding identities must be unique"
            )


@dataclass(frozen=True, slots=True)
class EffectiveToolResolution:
    project_id: str
    profile_revision: int
    boundary_mode: BoundaryMode
    tools: tuple[ToolDefinition, ...]
    excluded_reason_counts: tuple[tuple[str, int], ...]
    descriptor_bytes: int


class EffectiveToolPolicyResolver:
    def resolve(
        self,
        profile: ProjectCapabilityProfile,
        tools: tuple[ToolDefinition, ...],
        *,
        turn_allowed: tuple[str, ...],
        turn_denied: tuple[str, ...] = (),
        task_tool_ids: tuple[str, ...] = (),
        boundary_mode: BoundaryMode,
    ) -> EffectiveToolResolution:
        if boundary_mode not in _BOUNDARY_MODES:
            raise ProjectCapabilityProfileError("boundary mode is unsupported")
        _unique_names(turn_allowed, "turn allowed tool")
        _unique_names(turn_denied, "turn denied tool")
        _unique_names(task_tool_ids, "task tool hint")
        reasons: dict[str, int] = {}
        included: list[ToolDefinition] = []
        descriptor_bytes = 0
        for tool in sorted(tools, key=lambda item: item.tool_id):
            reason = self._exclude_reason(
                profile,
                tool,
                turn_allowed=set(turn_allowed),
                turn_denied=set(turn_denied),
                task_tool_ids=set(task_tool_ids),
                boundary_mode=boundary_mode,
            )
            if reason is not None:
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            size = _descriptor_bytes(tool)
            if len(included) >= profile.max_tools or descriptor_bytes + size > profile.max_tool_descriptor_bytes:
                reasons["descriptor_budget"] = reasons.get("descriptor_budget", 0) + 1
                continue
            included.append(tool)
            descriptor_bytes += size
        return EffectiveToolResolution(
            project_id=profile.project_id,
            profile_revision=profile.revision,
            boundary_mode=boundary_mode,
            tools=tuple(included),
            excluded_reason_counts=tuple(sorted(reasons.items())),
            descriptor_bytes=descriptor_bytes,
        )

    @staticmethod
    def _exclude_reason(
        profile: ProjectCapabilityProfile,
        tool: ToolDefinition,
        *,
        turn_allowed: set[str],
        turn_denied: set[str],
        task_tool_ids: set[str],
        boundary_mode: BoundaryMode,
    ) -> str | None:
        if not tool.available:
            return "unavailable"
        if tool.source not in profile.enabled_sources:
            return "source_disabled"
        if tool.source == "plugin" and tool.owner_id not in profile.enabled_plugin_ids:
            return "plugin_disabled"
        if tool.source == "mcp" and tool.owner_id not in profile.enabled_mcp_server_ids:
            return "mcp_disabled"
        if tool.source == "mcp":
            profile_binding = next(
                (
                    item for item in profile.mcp_server_selection_bindings
                    if item.server_id == tool.owner_id
                ),
                None,
            )
            if profile_binding is None:
                return "mcp_authority_unbound"
            connection = tool.connection_identity
            if connection is None or MCPServerSelectionBinding(
                server_id=connection.server_id,
                protocol_profile=_protocol_profile(connection.protocol_version),
                manifest_revision=connection.manifest_revision,
                endpoint_identity=connection.endpoint_identity,
                credential_subject_id=connection.credential_subject_id,
                transport_generation=connection.transport_generation,
            ) != profile_binding:
                return "mcp_authority_drift"
        if tool.tool_id in profile.denied_tool_ids:
            return "project_denied"
        bindings = {
            item.stable_id: item.contract_identity
            for item in profile.tool_selection_bindings
        }
        binding = bindings.get(tool.tool_id)
        if binding is not None and binding != tool_contract_binding_identity(tool):
            return "selection_revision_drift"
        if profile.tool_discovery_policy == "auto_discover":
            if profile.allowed_tool_ids and tool.tool_id not in profile.allowed_tool_ids:
                return "project_not_allowed"
            if tool.tool_id in profile.allowed_tool_ids and binding is None:
                return "selection_unbound"
        else:
            if tool.tool_id not in profile.allowed_tool_ids:
                return (
                    "discovery_disabled"
                    if profile.tool_discovery_policy == "disabled"
                    else "confirmation_required"
                )
            if binding is None:
                return "selection_unbound"
        if tool.tool_id not in turn_allowed or tool.tool_id in turn_denied:
            return "turn_restricted"
        if task_tool_ids and tool.tool_id not in task_tool_ids:
            return "task_not_selected"
        if boundary_mode == "sealed" and (
            tool.destination != "local" or tool.effect in {"external", "platform"}
        ):
            return "sealed_destination"
        return None


def _protocol_profile(protocol_version: str) -> str:
    for profile, version in _MCP_PROTOCOL_VERSIONS.items():
        if version == protocol_version:
            return profile
    return "unsupported"


def _descriptor_bytes(tool: ToolDefinition) -> int:
    values = (
        tool.tool_id,
        str(tool.version),
        tool.display_name,
        tool.description,
        tool.source,
        tool.owner_id,
        tool.effect,
        tool.destination,
        tool.input_schema_uri,
        tool.output_schema_uri,
        tool.receipt_schema_uri or "",
        *tool.data_classes,
        *tool.required_scopes,
        *tool.boundary_requirements,
    )
    return sum(len(value.encode("utf-8")) for value in values)


def _unique_names(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ProjectCapabilityProfileError(f"{label} values must be unique")
    for value in values:
        _require_text(value, label)


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ProjectCapabilityProfileError(f"{label} must be non-empty")
