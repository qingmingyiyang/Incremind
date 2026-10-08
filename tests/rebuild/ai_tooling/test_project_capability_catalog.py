from __future__ import annotations

from dataclasses import replace

import pytest

from core.ai_boundary import ProjectBoundaryProfile
from core.ai_kernel import CapabilityDefinition, ScopedCapabilityRegistry
from core.ai_tooling import (
    ProjectCapabilityCatalogError,
    ProjectCapabilityCatalogProjector,
    ProjectCapabilityProfile,
    MCPServerSelectionBinding,
    ToolConnectionIdentity,
    ToolDefinition,
    ToolRetryPolicy,
    ToolSelectionBinding,
    tool_contract_binding_identity,
)


class _Provider:
    def invoke(self, _arguments):
        return {"status": "completed"}


def test_catalog_is_project_sparse_keeps_same_name_different_owners() -> None:
    registry = ScopedCapabilityRegistry()
    registry.register(_capability(_tool("core.read", "Read notes", source="core", owner="core")), _Provider())
    registry.register(_capability(_tool("plugin.a", "Search", source="plugin", owner="plugin-a")), _Provider())
    registry.register(_capability(_tool("plugin.b", "Search", source="plugin", owner="plugin-b")), _Provider())
    registry.register(_capability(_tool("mcp.calendar", "Calendar", source="mcp", owner="calendar")), _Provider())
    snapshot = registry.snapshot()

    project_a = _projector().project(
        profile=_profile(enabled_sources=("core", "plugin"), enabled_plugin_ids=("plugin-a",)),
        boundary=_boundary(), capabilities=snapshot.definitions, registry_generation=snapshot.generation,
    )
    project_b = _projector().project(
        profile=_profile(
            project_id="project-b", enabled_sources=("core", "mcp"),
            enabled_mcp_server_ids=("calendar",),
            mcp_server_selection_bindings=(_mcp_binding("calendar"),),
        ),
        boundary=_boundary("project-b"), capabilities=snapshot.definitions,
        registry_generation=snapshot.generation,
    )

    b = {item.stable_id: item for item in project_b.entries}
    assert {item.stable_id for item in project_a.entries} == {"core.read", "plugin.a"}
    assert {item.stable_id for item in project_b.entries} == {"core.read", "mcp.calendar"}
    assert {item.display_name for item in project_a.entries} == {"Read notes", "Search"}
    assert {item.owner_id for item in project_a.entries} == {"core", "plugin-a"}
    assert dict(project_a.excluded_reason_counts) == {
        "plugin_disabled": 1,
        "source_disabled": 1,
    }
    assert "plugin-b" not in str(project_a.to_public_dict())
    assert "calendar" not in str(project_a.to_public_dict())
    assert b["mcp.calendar"].revision_identity == {
        "contract_version": 7,
        "mcp": {"manifest_revision": 2, "transport_generation": 3, "catalog_revision": 5, "tool_schema_revision": 7},
    }
    assert project_a.supported_kinds == ("tool",)
    assert all(item.kind == "tool" for item in project_a.entries)


def test_catalog_anonymizes_allow_deny_sealed_and_budget_exclusions() -> None:
    registry = ScopedCapabilityRegistry()
    for tool in (
        _tool("core.keep", "Keep", source="core", owner="core"),
        _tool("core.drop", "Drop", source="core", owner="core"),
        _tool("core.extra", "Extra", source="core", owner="core"),
        _tool("mcp.remote", "Remote", source="mcp", owner="remote"),
    ):
        registry.register(_capability(tool), _Provider())
    snapshot = registry.snapshot()

    allowed = _projector().project(
        profile=_profile(
            allowed_tool_ids=("core.keep",),
            tool_selection_bindings=(_binding(
                _tool("core.keep", "Keep", source="core", owner="core"),
            ),),
        ), boundary=_boundary(),
        capabilities=snapshot.definitions, registry_generation=snapshot.generation,
    )
    denied = _projector().project(
        profile=_profile(denied_tool_ids=("core.drop",)), boundary=_boundary(),
        capabilities=snapshot.definitions, registry_generation=snapshot.generation,
    )
    sealed = _projector().project(
        profile=_profile(
            enabled_sources=("core", "mcp"), enabled_mcp_server_ids=("remote",),
            mcp_server_selection_bindings=(_mcp_binding("remote"),),
        ),
        boundary=_boundary(mode="sealed"), capabilities=snapshot.definitions,
        registry_generation=snapshot.generation,
    )
    budget = _projector().project(
        profile=_profile(max_tools=1), boundary=_boundary(), capabilities=snapshot.definitions,
        registry_generation=snapshot.generation,
    )

    assert [item.stable_id for item in allowed.entries] == ["core.keep"]
    assert dict(allowed.excluded_reason_counts) == {"project_not_allowed": 2, "source_disabled": 1}
    assert "core.drop" not in str(allowed.to_public_dict())
    assert [item.stable_id for item in denied.entries] == ["core.extra", "core.keep"]
    assert dict(denied.excluded_reason_counts) == {"project_denied": 1, "source_disabled": 1}
    assert "core.drop" not in str(denied.to_public_dict())
    assert {item.stable_id for item in sealed.entries} == {"core.drop", "core.extra", "core.keep"}
    assert dict(sealed.excluded_reason_counts) == {"sealed_destination": 1}
    assert "mcp.remote" not in str(sealed.to_public_dict())
    assert [item.stable_id for item in budget.entries] == ["core.drop"]
    assert dict(budget.excluded_reason_counts) == {"descriptor_budget": 2, "source_disabled": 1}
    assert "core.extra" not in str(budget.to_public_dict())


def test_catalog_display_name_does_not_change_stable_identity_or_selection() -> None:
    registry = ScopedCapabilityRegistry()
    registration = registry.register(
        _capability(_tool("stable.read", "Initial label", source="core", owner="core")), _Provider()
    )
    first_snapshot = registry.snapshot()
    registration.close()
    registry.register(_capability(_tool("stable.read", "Renamed label", source="core", owner="core")), _Provider())
    second_snapshot = registry.snapshot()

    first = _projector().project(profile=_profile(), boundary=_boundary(), capabilities=first_snapshot.definitions, registry_generation=first_snapshot.generation)
    second = _projector().project(profile=_profile(), boundary=_boundary(), capabilities=second_snapshot.definitions, registry_generation=second_snapshot.generation)
    assert first.entries[0].stable_id == second.entries[0].stable_id == "stable.read"
    assert first.entries[0].display_name == "Initial label"
    assert second.entries[0].display_name == "Renamed label"
    assert first.entries[0].selected is second.entries[0].selected is True
    assert second.registry_generation > first.registry_generation


def test_registry_removal_advances_generation_and_catalog_removes_entry() -> None:
    registry = ScopedCapabilityRegistry()
    registration = registry.register(_capability(_tool("core.read", "Read", source="core", owner="core")), _Provider())
    before = registry.snapshot()
    registration.close()
    after = registry.snapshot()

    assert after.generation > before.generation
    catalog = _projector().project(profile=_profile(), boundary=_boundary(), capabilities=after.definitions, registry_generation=after.generation)
    assert catalog.entries == ()


def test_catalog_rejects_boundary_revision_drift() -> None:
    with pytest.raises(ProjectCapabilityCatalogError, match="revisions do not match"):
        _projector().project(
            profile=_profile(boundary_profile_revision=1),
            boundary=ProjectBoundaryProfile("project-boundary-project-a", "project-a", "guarded", 2, "review"),
            capabilities=(), registry_generation=0,
        )


def test_mcp_identity_binding_is_required_and_live_drift_is_fail_closed() -> None:
    tool = _tool("mcp.calendar", "Calendar", source="mcp", owner="calendar")
    binding = _mcp_binding("calendar")
    profile = _profile(
        enabled_sources=("core", "mcp"), enabled_mcp_server_ids=("calendar",),
        mcp_server_selection_bindings=(binding,),
    )
    resolver = ProjectCapabilityCatalogProjector()._resolver
    baseline = resolver.resolve(
        profile, (tool,), turn_allowed=(tool.tool_id,), boundary_mode="guarded",
    )
    assert baseline.tools == (tool,)
    for field, changed in (
        ("protocol_version", "2026-07-28"), ("manifest_revision", 3),
        ("endpoint_identity", "other-server"),
        ("credential_subject_id", "other-user"), ("transport_generation", 4),
    ):
        drifted = replace(
            tool,
            connection_identity=replace(tool.connection_identity, **{field: changed}),
        )
        resolution = resolver.resolve(
            profile, (drifted,), turn_allowed=(tool.tool_id,), boundary_mode="guarded",
        )
        assert resolution.tools == ()
        assert dict(resolution.excluded_reason_counts) == {"mcp_authority_drift": 1}
    unbound = replace(profile, mcp_server_selection_bindings=())
    resolution = resolver.resolve(
        unbound, (tool,), turn_allowed=(tool.tool_id,), boundary_mode="guarded",
    )
    assert dict(resolution.excluded_reason_counts) == {"mcp_authority_unbound": 1}
    core = _tool("core.read", "Read", source="core", owner="core")
    assert resolver.resolve(
        _profile(), (core,), turn_allowed=(core.tool_id,), boundary_mode="guarded",
    ).tools == (core,)


def _projector() -> ProjectCapabilityCatalogProjector:
    return ProjectCapabilityCatalogProjector()


def _profile(project_id: str = "project-a", **changes: object) -> ProjectCapabilityProfile:
    values: dict[str, object] = {
        "profile_id": f"project-capability-{project_id}", "project_id": project_id, "revision": 2,
        "boundary_profile_id": f"project-boundary-{project_id}", "boundary_profile_revision": 1,
        "enabled_sources": ("core",), "enabled_skill_ids": (), "enabled_plugin_ids": (),
        "enabled_mcp_server_ids": (), "allowed_tool_ids": (), "denied_tool_ids": (),
        "preferred_model_tier": "standard", "memory_scope": "project_only",
        "cross_project_grant_ids": (), "output_style_profile_id": None,
    }
    values.update(changes)
    return ProjectCapabilityProfile(**values)  # type: ignore[arg-type]


def _boundary(project_id: str = "project-a", *, mode: str = "guarded") -> ProjectBoundaryProfile:
    return ProjectBoundaryProfile(f"project-boundary-{project_id}", project_id, mode, 1, "review")  # type: ignore[arg-type]


def _capability(tool: ToolDefinition) -> CapabilityDefinition:
    return CapabilityDefinition(
        tool.tool_id, tool.version, tool.effect, tool.operation_semantics == "receipt_required",
        tool.operation_semantics, tool.input_schema_uri, tool.output_schema_uri, tool,
    )


def _tool(tool_id: str, display_name: str, *, source: str, owner: str) -> ToolDefinition:
    mcp = source == "mcp"
    return ToolDefinition(
        tool_id=tool_id, version=7, display_name=display_name, description="private model prose omitted from catalog",
        source=source, owner_id=owner, effect="read", data_classes=("project_content",),
        destination="mcp" if mcp else "local", input_schema_uri="crp://private/input", output_schema_uri="crp://private/output",
        receipt_schema_uri=None, operation_semantics="read_only", execution_mode="parallel", resource_locks=(),
        idempotency="idempotent", retry_policy=ToolRetryPolicy(2, 100, ("timeout",)), verification_tool_id=None,
        compensation_tool_id=None, mutability="read_only", egress_class="remote" if mcp else "none",
        network_scope=("mcp",) if mcp else (), data_egress_scope=("project_content",) if mcp else (), timeout_ms=5_000,
        required_scopes=(), boundary_requirements=(), connection_identity=(
            ToolConnectionIdentity("mcp", owner, "2025-11-25", 2, "local-server", "personal", 3, 5, 7) if mcp else None
        ),
    )  # type: ignore[arg-type]


def _binding(tool: ToolDefinition) -> ToolSelectionBinding:
    return ToolSelectionBinding(
        tool.tool_id, tool_contract_binding_identity(tool),
    )


def _mcp_binding(server_id: str) -> MCPServerSelectionBinding:
    return MCPServerSelectionBinding(
        server_id, "legacy_2025_11_25", 2,
        "local-server", "personal", 3,
    )
