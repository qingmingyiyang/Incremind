from __future__ import annotations

import pytest

from core.ai_tooling import (
    EffectiveToolPolicyResolver,
    MCPServerSelectionBinding,
    ProjectCapabilityProfile,
    ProjectCapabilityProfileError,
    ToolConnectionIdentity,
    ToolDefinition,
    ToolRetryPolicy,
    ToolSelectionBinding,
    tool_contract_binding_identity,
)


def test_same_registry_resolves_different_tools_for_different_projects() -> None:
    tools = (
        _tool("memory.recall", source="core", owner="memory"),
        _tool("docs.export", source="plugin", owner="document-plugin"),
        _tool("calendar.read", source="mcp", owner="calendar-server"),
    )
    resolver = EffectiveToolPolicyResolver()
    project_a = resolver.resolve(
        _profile(enabled_sources=("core", "plugin"), enabled_plugin_ids=("document-plugin",)),
        tools,
        turn_allowed=tuple(tool.tool_id for tool in tools),
        boundary_mode="guarded",
    )
    project_b = resolver.resolve(
        _profile(
            project_id="project-b",
            enabled_sources=("core", "mcp"),
            enabled_mcp_server_ids=("calendar-server",),
            mcp_server_selection_bindings=(_mcp_binding("calendar-server"),),
        ),
        tools,
        turn_allowed=tuple(tool.tool_id for tool in tools),
        boundary_mode="guarded",
    )
    assert [tool.tool_id for tool in project_a.tools] == ["docs.export", "memory.recall"]
    assert [tool.tool_id for tool in project_b.tools] == ["calendar.read", "memory.recall"]


def test_turn_policy_can_only_narrow_project_tool_set() -> None:
    tools = (_tool("memory.recall"), _tool("document.read"))
    resolution = EffectiveToolPolicyResolver().resolve(
        _profile(
            allowed_tool_ids=("memory.recall",),
            tool_selection_bindings=(_binding(_tool("memory.recall")),),
        ),
        tools,
        turn_allowed=("memory.recall", "document.read", "not-installed"),
        boundary_mode="open",
    )
    assert [tool.tool_id for tool in resolution.tools] == ["memory.recall"]
    assert dict(resolution.excluded_reason_counts)["project_not_allowed"] == 1


def test_denied_unavailable_and_task_filter_are_traced() -> None:
    tools = (
        _tool("a.read"),
        _tool("b.read", available=False),
        _tool("c.read"),
    )
    resolution = EffectiveToolPolicyResolver().resolve(
        _profile(denied_tool_ids=("a.read",)),
        tools,
        turn_allowed=("a.read", "b.read", "c.read"),
        task_tool_ids=("a.read",),
        boundary_mode="guarded",
    )
    assert resolution.tools == ()
    assert dict(resolution.excluded_reason_counts) == {
        "project_denied": 1,
        "task_not_selected": 1,
        "unavailable": 1,
    }


def test_plugin_and_mcp_require_exact_project_enablement() -> None:
    tools = (
        _tool("plugin.tool", source="plugin", owner="plugin-a"),
        _tool("mcp.tool", source="mcp", owner="server-a"),
    )
    resolution = EffectiveToolPolicyResolver().resolve(
        _profile(enabled_sources=("plugin", "mcp")),
        tools,
        turn_allowed=("plugin.tool", "mcp.tool"),
        boundary_mode="guarded",
    )
    assert resolution.tools == ()
    assert dict(resolution.excluded_reason_counts) == {"mcp_disabled": 1, "plugin_disabled": 1}


def test_sealed_profile_does_not_expose_remote_or_platform_tools() -> None:
    tools = (
        _tool("local.read"),
        _tool("remote.answer", effect="external", destination="provider"),
        _tool("platform.notify", effect="platform", destination="platform"),
    )
    resolution = EffectiveToolPolicyResolver().resolve(
        _profile(enabled_sources=("core",)),
        tools,
        turn_allowed=tuple(tool.tool_id for tool in tools),
        boundary_mode="sealed",
    )
    assert [tool.tool_id for tool in resolution.tools] == ["local.read"]
    assert dict(resolution.excluded_reason_counts)["sealed_destination"] == 2


def test_tool_count_and_descriptor_byte_budgets_are_deterministic() -> None:
    tools = tuple(_tool(f"tool-{index}.read", description="x" * 800) for index in range(4))
    resolution = EffectiveToolPolicyResolver().resolve(
        _profile(max_tools=2, max_tool_descriptor_bytes=2048),
        tools,
        turn_allowed=tuple(tool.tool_id for tool in tools),
        boundary_mode="guarded",
    )
    assert len(resolution.tools) == 2
    assert resolution.descriptor_bytes <= 2048
    assert dict(resolution.excluded_reason_counts)["descriptor_budget"] == 2


def test_model_visible_tool_default_and_hard_limit_are_twelve() -> None:
    profile = _profile()
    assert profile.max_tools == 12
    with pytest.raises(ProjectCapabilityProfileError, match="supported range"):
        _profile(max_tools=13)

    tools = tuple(_tool(f"tool-{index}.read") for index in range(13))
    resolution = EffectiveToolPolicyResolver().resolve(
        profile,
        tools,
        turn_allowed=tuple(tool.tool_id for tool in tools),
        boundary_mode="guarded",
    )
    assert len(resolution.tools) == 12
    assert dict(resolution.excluded_reason_counts)["descriptor_budget"] == 1


def test_profile_rejects_dual_tool_policy_and_duplicate_grant_authority() -> None:
    with pytest.raises(ProjectCapabilityProfileError, match="both allowed and denied"):
        _profile(allowed_tool_ids=("memory.recall",), denied_tool_ids=("memory.recall",))
    assert "persistent_grants" not in ProjectCapabilityProfile.__dataclass_fields__


def test_versioned_selection_binding_fails_closed_on_contract_replacement() -> None:
    original = _tool("memory.recall")
    replacement = _tool("memory.recall", owner="replacement-owner")
    resolution = EffectiveToolPolicyResolver().resolve(
        _profile(tool_selection_bindings=(_binding(original),)),
        (replacement,), turn_allowed=("memory.recall",), boundary_mode="open",
    )
    assert resolution.tools == ()
    assert dict(resolution.excluded_reason_counts) == {
        "selection_revision_drift": 1,
    }


def test_contract_binding_ignores_prose_but_changes_with_execution_contract() -> None:
    original = _tool("memory.recall")
    assert tool_contract_binding_identity(original) == tool_contract_binding_identity(
        _tool("memory.recall", description="new user-facing prose"),
    )
    assert tool_contract_binding_identity(original) != tool_contract_binding_identity(
        _tool("memory.recall", owner="replacement-owner"),
    )
    identity = tool_contract_binding_identity(original)
    assert identity.startswith("contract-sha256:")
    assert "schema" not in identity and "owner" not in identity


def test_legacy_explicit_allowlist_without_binding_fails_closed() -> None:
    resolution = EffectiveToolPolicyResolver().resolve(
        _profile(allowed_tool_ids=("memory.recall",)),
        (_tool("memory.recall"),),
        turn_allowed=("memory.recall",),
        boundary_mode="open",
    )
    assert resolution.tools == ()
    assert dict(resolution.excluded_reason_counts) == {"selection_unbound": 1}


def test_confirm_new_and_disabled_require_explicit_bound_selection() -> None:
    selected = _tool("memory.recall")
    new_tool = _tool("document.read")
    for policy, reason in (
        ("confirm_new", "confirmation_required"),
        ("disabled", "discovery_disabled"),
    ):
        resolution = EffectiveToolPolicyResolver().resolve(
            _profile(
                tool_discovery_policy=policy,
                allowed_tool_ids=(selected.tool_id,),
                tool_selection_bindings=(_binding(selected),),
            ),
            (selected, new_tool),
            turn_allowed=(selected.tool_id, new_tool.tool_id),
            boundary_mode="guarded",
        )
        assert [tool.tool_id for tool in resolution.tools] == ["memory.recall"]
        assert dict(resolution.excluded_reason_counts) == {reason: 1}


def _profile(**changes: object) -> ProjectCapabilityProfile:
    values: dict[str, object] = {
        "profile_id": "capability-project-a",
        "project_id": "project-a",
        "revision": 1,
        "boundary_profile_id": "project-boundary-project-a",
        "boundary_profile_revision": 1,
        "enabled_sources": ("core",),
        "enabled_skill_ids": (),
        "enabled_plugin_ids": (),
        "enabled_mcp_server_ids": (),
        "allowed_tool_ids": (),
        "denied_tool_ids": (),
        "preferred_model_tier": "standard",
        "memory_scope": "project_only",
        "cross_project_grant_ids": (),
        "output_style_profile_id": None,
    }
    values.update(changes)
    if "project_id" in changes and "profile_id" not in changes:
        values["profile_id"] = f"capability-{changes['project_id']}"
        values["boundary_profile_id"] = f"project-boundary-{changes['project_id']}"
    return ProjectCapabilityProfile(**values)  # type: ignore[arg-type]


def _tool(
    tool_id: str,
    *,
    source: str = "core",
    owner: str = "core",
    effect: str = "read",
    destination: str = "local",
    available: bool = True,
    description: str = "Read project data",
) -> ToolDefinition:
    side_effect = effect != "read"
    is_mcp = source == "mcp"
    effective_destination = "mcp" if is_mcp else destination
    is_remote = is_mcp or effect == "external"
    return ToolDefinition(
        tool_id=tool_id,
        version=1,
        display_name=tool_id,
        description=description,
        source=source,  # type: ignore[arg-type]
        owner_id=owner,
        effect=effect,  # type: ignore[arg-type]
        data_classes=("project_content",),
        destination=effective_destination,  # type: ignore[arg-type]
        input_schema_uri="crp://input",
        output_schema_uri="crp://output",
        receipt_schema_uri="crp://receipt" if side_effect else None,
        operation_semantics="receipt_required" if side_effect else "read_only",
        execution_mode="parallel" if not side_effect else "exclusive",
        resource_locks=() if not side_effect else (f"tool:{tool_id}",),
        idempotency="idempotent" if not side_effect else "never_retry",
        retry_policy=ToolRetryPolicy(
            2 if not side_effect else 1,
            250 if not side_effect else 0,
            ("timeout",) if not side_effect else (),
        ),
        verification_tool_id=None,
        compensation_tool_id=None,
        mutability="read_only" if not side_effect else "irreversible",
        egress_class="remote" if is_remote else ("local" if side_effect else "none"),
        network_scope=(owner,) if is_remote else (),
        data_egress_scope=("project_content",) if is_remote else (),
        timeout_ms=10_000,
        required_scopes=("project",),
        boundary_requirements=(),
        available=available,
        connection_identity=(
            ToolConnectionIdentity(
                "mcp", owner, "2025-11-25", 1,
                f"{owner}-local", f"{owner}-personal", 1, 1, 1,
            ) if is_mcp else None
        ),
    )


def _binding(tool: ToolDefinition) -> ToolSelectionBinding:
    return ToolSelectionBinding(
        tool.tool_id, tool_contract_binding_identity(tool),
    )


def _mcp_binding(server_id: str) -> MCPServerSelectionBinding:
    return MCPServerSelectionBinding(
        server_id=server_id,
        protocol_profile="legacy_2025_11_25",
        manifest_revision=1,
        endpoint_identity=f"{server_id}-local",
        credential_subject_id=f"{server_id}-personal",
        transport_generation=1,
    )
