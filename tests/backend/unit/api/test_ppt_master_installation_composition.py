from __future__ import annotations

import pytest

from backend.api.ai_profile_resolvers import (
    ProjectAwareCapabilityManifestResolver,
    TurnProjectProfileSnapshotAuthority,
)
from backend.api.ppt_master_installation_composition import (
    _ArchiveDownloader,
    _GitHubHeadResolver,
    _ProfileProjector,
    _ppt_master_tool_binding,
    _selection_state,
    _valid_profile_step,
    _write_profile_step,
)
from backend.security.network_adapter import LoopbackHttpConnectProxy
from backend.api.ppt_master_capability_runtime import ppt_master_fixed_capability_definition
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from core.ai_kernel import ScopedCapabilityRegistry


INSTALL_OPERATION = "eff2_" + "a" * 64
ROLLBACK_OPERATION = "eff2_" + "b" * 64


def test_github_head_and_archive_adapters_receive_the_same_frozen_egress_proxy(tmp_path, monkeypatch) -> None:
    proxy = LoopbackHttpConnectProxy("127.0.0.1", 7890)
    seen: list[tuple[str, object]] = []

    class TextAdapter:
        def __init__(self, **kwargs) -> None:
            seen.append(("text", kwargs.get("connect_proxy")))

    class BinaryAdapter:
        def __init__(self, *_args, **kwargs) -> None:
            seen.append(("binary", kwargs.get("connect_proxy")))

    monkeypatch.setattr("backend.api.ppt_master_installation_composition.SafeTextNetworkAdapter", TextAdapter)
    monkeypatch.setattr("backend.api.ppt_master_installation_composition.SafeBinaryDownloadAdapter", BinaryAdapter)

    _GitHubHeadResolver(connect_proxy=proxy)
    _ArchiveDownloader(tmp_path, connect_proxy=proxy)

    assert seen == [("text", proxy), ("binary", proxy)]


def _update(store: ProjectCapabilityProfileStore, project_id: str, **changes):
    snapshot = store.get(project_id)
    profile = snapshot.profile
    values = {
        "boundary_profile_id": profile.boundary_profile_id,
        "boundary_profile_revision": profile.boundary_profile_revision,
        "enabled_sources": profile.enabled_sources,
        "enabled_skill_ids": profile.enabled_skill_ids,
        "enabled_plugin_ids": profile.enabled_plugin_ids,
        "enabled_mcp_server_ids": profile.enabled_mcp_server_ids,
        "allowed_tool_ids": profile.allowed_tool_ids,
        "denied_tool_ids": profile.denied_tool_ids,
        "preferred_model_tier": profile.preferred_model_tier,
        "memory_scope": profile.memory_scope,
        "cross_project_grant_ids": profile.cross_project_grant_ids,
        "output_style_profile_id": profile.output_style_profile_id,
        "max_tools": profile.max_tools,
        "max_tool_descriptor_bytes": profile.max_tool_descriptor_bytes,
        "tool_discovery_policy": profile.tool_discovery_policy,
        "tool_selection_bindings": profile.tool_selection_bindings,
        "mcp_server_selection_bindings": profile.mcp_server_selection_bindings,
    }
    values.update(changes)
    return store.update(project_id, expected_revision=snapshot.store_revision, **values)


def test_profile_rollback_preserves_capabilities_that_preceded_this_install(tmp_path) -> None:
    project_id = "project-a"
    store = ProjectCapabilityProfileStore(tmp_path)
    _update(
        store, project_id,
        enabled_sources=("core", "plugin"), enabled_skill_ids=("ppt-master",),
        enabled_plugin_ids=("ppt-master",), allowed_tool_ids=("presentation.pptx.fixed",),
        tool_selection_bindings=(_ppt_master_tool_binding(),),
    )
    projector = _ProfileProjector(tmp_path)

    installed = projector.activate(project_id, plugin_id="ppt-master", skill_id="ppt-master", operation_id=INSTALL_OPERATION)
    assert installed["owned_tool_id"] is False
    projector.deactivate(
        project_id, plugin_id="ppt-master", skill_id="ppt-master", operation_id=ROLLBACK_OPERATION,
        installation_projection=installed,
    )

    profile = store.get(project_id).profile
    assert "ppt-master" in profile.enabled_skill_ids
    assert "ppt-master" in profile.enabled_plugin_ids
    assert "presentation.pptx.fixed" in profile.allowed_tool_ids


def test_profile_rollback_removes_only_capabilities_owned_by_this_install(tmp_path) -> None:
    project_id = "project-b"
    store = ProjectCapabilityProfileStore(tmp_path)
    projector = _ProfileProjector(tmp_path)

    installed = projector.activate(project_id, plugin_id="ppt-master", skill_id="ppt-master", operation_id=INSTALL_OPERATION)
    assert installed["owned_tool_id"] is True
    projector.deactivate(
        project_id, plugin_id="ppt-master", skill_id="ppt-master", operation_id=ROLLBACK_OPERATION,
        installation_projection=installed,
    )

    profile = store.get(project_id).profile
    assert "ppt-master" not in profile.enabled_skill_ids
    assert "ppt-master" not in profile.enabled_plugin_ids
    assert "presentation.pptx.fixed" not in profile.allowed_tool_ids


def test_profile_step_is_operation_idempotent_and_preserves_first_ownership(tmp_path) -> None:
    project_id = "project-c"
    store = ProjectCapabilityProfileStore(tmp_path)
    projector = _ProfileProjector(tmp_path)

    first = projector.activate(project_id, plugin_id="ppt-master", skill_id="ppt-master", operation_id=INSTALL_OPERATION)
    revision = store.get(project_id).store_revision
    replay = projector.activate(project_id, plugin_id="ppt-master", skill_id="ppt-master", operation_id=INSTALL_OPERATION)

    assert first == replay
    assert first["owned_tool_id"] is True
    assert store.get(project_id).store_revision == revision

    removed = projector.deactivate(
        project_id, plugin_id="ppt-master", skill_id="ppt-master", operation_id=ROLLBACK_OPERATION,
        installation_projection=first,
    )
    removed_revision = store.get(project_id).store_revision
    assert projector.deactivate(
        project_id, plugin_id="ppt-master", skill_id="ppt-master", operation_id=ROLLBACK_OPERATION,
        installation_projection=first,
    ) == removed
    assert store.get(project_id).store_revision == removed_revision
    profile = store.get(project_id).profile
    assert "ppt-master" not in profile.enabled_skill_ids
    assert "presentation.pptx.fixed" not in profile.allowed_tool_ids


def test_profile_step_subject_or_state_drift_fails_closed(tmp_path) -> None:
    project_id = "project-d"
    store = ProjectCapabilityProfileStore(tmp_path)
    projector = _ProfileProjector(tmp_path)
    projector.activate(project_id, plugin_id="ppt-master", skill_id="ppt-master", operation_id=INSTALL_OPERATION)

    with pytest.raises(ValueError, match="receipt drifted"):
        projector.activate(project_id, plugin_id="other-plugin", skill_id="ppt-master", operation_id=INSTALL_OPERATION)

    profile = store.get(project_id).profile
    before = _selection_state(profile)
    _write_profile_step(projector._step_path(ROLLBACK_OPERATION), {
        "operation_id": ROLLBACK_OPERATION, "project_id": project_id, "action": "deactivate",
        "subject": {"plugin_id": "ppt-master", "skill_id": "ppt-master"}, "before": before,
        "after": {**before, "enabled_skill_ids": []},
        "projection": {"project_id": project_id, "status": "inactive"},
    })
    _update(store, project_id, enabled_skill_ids=("externally-changed",))
    with pytest.raises(ValueError, match="state conflicts"):
        projector.deactivate(
            project_id, plugin_id="ppt-master", skill_id="ppt-master", operation_id=ROLLBACK_OPERATION,
            installation_projection={"project_id": project_id, "status": "active", "owned_skill_id": True,
                                     "owned_plugin_id": True, "owned_tool_id": True, "owned_plugin_source": True},
        )


def test_profile_step_rejects_unsafe_selection_and_ownership_shape() -> None:
    binding = _ppt_master_tool_binding()
    step = {
        "operation_id": INSTALL_OPERATION, "project_id": "project-safe", "action": "activate",
        "subject": {"plugin_id": "ppt-master", "skill_id": "ppt-master"},
        "before": {"enabled_sources": ["core"], "enabled_skill_ids": [], "enabled_plugin_ids": [], "allowed_tool_ids": [], "tool_selection_bindings": []},
        "after": {"enabled_sources": ["core", "plugin"], "enabled_skill_ids": ["ppt-master"], "enabled_plugin_ids": ["ppt-master"], "allowed_tool_ids": ["presentation.pptx.fixed"], "tool_selection_bindings": [{"stable_id": binding.stable_id, "contract_identity": binding.contract_identity}]},
        "projection": {"project_id": "project-safe", "status": "active", "owned_plugin_source": True,
                       "owned_skill_id": True, "owned_plugin_id": True, "owned_tool_id": True},
    }
    assert _valid_profile_step(step)
    duplicate = {**step, "after": {**step["after"], "enabled_skill_ids": ["ppt-master", "ppt-master"]}}
    assert not _valid_profile_step(duplicate)
    invalid_ownership = {**step, "projection": {**step["projection"], "owned_tool_id": "true"}}
    assert not _valid_profile_step(invalid_ownership)
    invalid_status = {**step, "projection": {**step["projection"], "status": "enabled"}}
    assert not _valid_profile_step(invalid_status)


def test_cached_ai_registry_exposes_ppt_only_to_new_turns_while_profile_is_active(tmp_path) -> None:
    """Installation changes the profile authority, never a cached registry.

    The provider is intentionally registered before installation.  Each new
    Turn resolves its manifest from the then-current durable project profile;
    an earlier manifest remains a frozen value after install and rollback.
    """
    project_id = "project-new-turn"
    registry = ScopedCapabilityRegistry()
    definition = ppt_master_fixed_capability_definition()
    registry.register(definition, object())
    resolver = ProjectAwareCapabilityManifestResolver(
        TurnProjectProfileSnapshotAuthority(
            ProjectCapabilityProfileStore(tmp_path), ProjectBoundaryProfileStore(tmp_path),
        ),
    )

    before = resolver.resolve(
        _turn_request("turn-before-install", project_id), registry.list(),
    )
    assert definition.capability_id not in before.capability_ids

    projection = _ProfileProjector(tmp_path).activate(
        project_id, plugin_id="ppt-master", skill_id="ppt-master", operation_id=INSTALL_OPERATION,
    )
    after_install = resolver.resolve(
        _turn_request("turn-after-install", project_id), registry.list(),
    )
    assert definition.capability_id in after_install.capability_ids
    assert definition.capability_id not in before.capability_ids

    _ProfileProjector(tmp_path).deactivate(
        project_id, plugin_id="ppt-master", skill_id="ppt-master", operation_id=ROLLBACK_OPERATION,
        installation_projection=projection,
    )
    after_rollback = resolver.resolve(
        _turn_request("turn-after-rollback", project_id), registry.list(),
    )
    assert definition.capability_id not in after_rollback.capability_ids
    assert definition.capability_id in after_install.capability_ids


def _turn_request(turn_id: str, project_id: str) -> dict[str, object]:
    return {
        "turn_id": turn_id,
        "scope": {"kind": "project", "project_id": project_id},
        "capability_policy": {
            "allowed": ["presentation.pptx.fixed"], "denied": [], "require_approval": [],
        },
    }
