from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.security.project_capability_profiles import (
    ProjectCapabilityProfileConflict,
    ProjectCapabilityProfileStore,
    ProjectCapabilityProfileStoreError,
)
from core.ai_tooling import MCPServerSelectionBinding, ToolSelectionBinding


def _mcp_binding(**changes: object) -> MCPServerSelectionBinding:
    values: dict[str, object] = {
        "server_id": "calendar", "protocol_profile": "stateless_2026_07_28",
        "manifest_revision": 7,
        "endpoint_identity": "calendar-endpoint", "credential_subject_id": "local-user",
        "transport_generation": 2,
    }
    values.update(changes)
    return MCPServerSelectionBinding(**values)  # type: ignore[arg-type]


def test_default_profile_is_guarded_by_project_boundary_without_persistence(tmp_path: Path) -> None:
    snapshot = ProjectCapabilityProfileStore(tmp_path).get("project-a")
    assert snapshot.persisted is False and snapshot.store_revision == 0
    assert snapshot.profile.project_id == "project-a"
    assert snapshot.profile.boundary_profile_id == "project-boundary-project-a"
    assert snapshot.profile.boundary_profile_revision == 1
    assert snapshot.profile.enabled_sources == ("core",)
    assert snapshot.profile.max_tools == 12
    assert snapshot.compatibility_diagnostics == ()
    assert not (tmp_path / "library/projects/project-a/ai/capability-profile.json").exists()


@pytest.mark.parametrize("historical_max_tools", (13, 64))
def test_historical_tool_limits_are_clamped_without_rewriting_or_startup_failure(
    tmp_path: Path, historical_max_tools: int,
) -> None:
    store = ProjectCapabilityProfileStore(tmp_path)
    store.update(
        "project-a", expected_revision=0,
        boundary_profile_id="project-boundary-project-a",
        boundary_profile_revision=1,
    )
    path = tmp_path / "library/projects/project-a/ai/capability-profile.json"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    persisted["max_tools"] = historical_max_tools
    path.write_text(json.dumps(persisted), encoding="utf-8")

    loaded = ProjectCapabilityProfileStore(tmp_path).get("project-a")

    assert loaded.profile.max_tools == 12
    assert loaded.compatibility_diagnostics == ("max_tools_clamped_to_12",)
    assert json.loads(path.read_text(encoding="utf-8"))["max_tools"] == historical_max_tools


def test_new_profile_writes_reject_tool_limits_above_twelve(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="supported range"):
        ProjectCapabilityProfileStore(tmp_path).update(
            "project-a", expected_revision=0,
            boundary_profile_id="project-boundary-project-a",
            boundary_profile_revision=1,
            max_tools=13,
        )


def test_profile_update_is_atomic_versioned_and_restart_safe(tmp_path: Path) -> None:
    store = ProjectCapabilityProfileStore(tmp_path)
    saved = store.update(
        "project-a",
        expected_revision=0,
        boundary_profile_id="project-boundary-project-a",
        boundary_profile_revision=1,
        enabled_sources=("core", "plugin"),
        enabled_plugin_ids=("docs-plugin",),
        denied_tool_ids=("external.search",),
        preferred_model_tier="deep",
    )
    assert saved.persisted is True and saved.store_revision == 1
    restarted = ProjectCapabilityProfileStore(tmp_path).get("project-a")
    assert restarted.profile == saved.profile
    assert restarted.profile.tool_discovery_policy == "auto_discover"
    with pytest.raises(ProjectCapabilityProfileConflict, match="revision conflict"):
        store.update(
            "project-a",
            expected_revision=0,
            boundary_profile_id="project-boundary-project-a",
            boundary_profile_revision=1,
        )


def test_v11_allowlist_loads_unbound_without_inheriting_current_tool_contract(
    tmp_path: Path,
) -> None:
    store = ProjectCapabilityProfileStore(tmp_path)
    store.update(
        "project-a", expected_revision=0,
        boundary_profile_id="project-boundary-project-a",
        boundary_profile_revision=1,
        allowed_tool_ids=("memory.recall",),
    )
    path = tmp_path / "library/projects/project-a/ai/capability-profile.json"
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy["schema_version"] = "1.1.0"
    legacy.pop("tool_discovery_policy")
    legacy.pop("tool_selection_bindings")
    legacy.pop("mcp_server_selection_bindings")
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = store.get("project-a").profile
    assert loaded.allowed_tool_ids == ("memory.recall",)
    assert loaded.tool_discovery_policy == "auto_discover"
    assert loaded.tool_selection_bindings == ()


def test_v12_profile_loads_with_empty_mcp_binding_without_writing_back(tmp_path: Path) -> None:
    store = ProjectCapabilityProfileStore(tmp_path)
    store.update(
        "project-a", expected_revision=0,
        boundary_profile_id="project-boundary-project-a", boundary_profile_revision=1,
    )
    path = tmp_path / "library/projects/project-a/ai/capability-profile.json"
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy["schema_version"] = "1.2.0"
    legacy.pop("mcp_server_selection_bindings")
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = store.get("project-a")

    assert loaded.profile.mcp_server_selection_bindings == ()
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == "1.2.0"


def test_selection_binding_round_trip_is_strict(tmp_path: Path) -> None:
    binding = ToolSelectionBinding(
        "memory.recall", f"contract-sha256:{'a' * 64}",
    )
    saved = ProjectCapabilityProfileStore(tmp_path).update(
        "project-a", expected_revision=0,
        boundary_profile_id="project-boundary-project-a",
        boundary_profile_revision=1,
        tool_discovery_policy="confirm_new",
        allowed_tool_ids=("memory.recall",),
        tool_selection_bindings=(binding,),
    )
    assert saved.profile.tool_selection_bindings == (binding,)
    assert ProjectCapabilityProfileStore(tmp_path).get("project-a").profile == saved.profile


def test_profile_rejects_identity_drift_sensitive_fields_and_path_escape(tmp_path: Path) -> None:
    path = tmp_path / "library/projects/project-a/ai/capability-profile.json"
    ProjectCapabilityProfileStore(tmp_path).update(
        "project-a",
        expected_revision=0,
        boundary_profile_id="project-boundary-project-a",
        boundary_profile_revision=1,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["project_id"] = "project-b"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ProjectCapabilityProfileStoreError, match="identity drifted"):
        ProjectCapabilityProfileStore(tmp_path).get("project-a")
    with pytest.raises(ProjectCapabilityProfileStoreError, match="identity is invalid"):
        ProjectCapabilityProfileStore(tmp_path).get("../escape")


def test_legacy_profile_requires_explicit_boundary_binding_migration(tmp_path: Path) -> None:
    store = ProjectCapabilityProfileStore(tmp_path)
    saved = store.update(
        "project-a",
        expected_revision=0,
        boundary_profile_id="project-boundary-project-a",
        boundary_profile_revision=1,
        enabled_sources=("core", "plugin"),
        enabled_plugin_ids=("docs-plugin",),
    )
    path = tmp_path / "library/projects/project-a/ai/capability-profile.json"
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy["schema_version"] = "1.0.0"
    legacy.pop("boundary_profile_revision")
    legacy.pop("tool_discovery_policy")
    legacy.pop("tool_selection_bindings")
    legacy.pop("mcp_server_selection_bindings")
    path.write_text(json.dumps(legacy), encoding="utf-8")

    with pytest.raises(ProjectCapabilityProfileStoreError, match="explicit boundary revision migration"):
        store.get("project-a")
    with pytest.raises(ProjectCapabilityProfileStoreError, match="explicit boundary revision migration"):
        store.update(
            "project-a",
            expected_revision=saved.profile.revision,
            boundary_profile_id="project-boundary-project-a",
            boundary_profile_revision=2,
        )

    migrated = store.migrate_boundary_binding(
        "project-a",
        expected_revision=saved.profile.revision,
        boundary_profile_id="project-boundary-project-a",
        boundary_profile_revision=2,
    )
    assert migrated.profile.revision == 2
    assert migrated.profile.boundary_profile_revision == 2
    assert migrated.profile.enabled_plugin_ids == ("docs-plugin",)
    assert store.get("project-a").profile == migrated.profile


def test_exclude_tool_is_narrow_cas_and_preserves_every_other_profile_field(tmp_path: Path) -> None:
    store = ProjectCapabilityProfileStore(tmp_path)
    original = store.update(
        "project-a", expected_revision=0,
        boundary_profile_id="project-boundary-project-a",
        boundary_profile_revision=1,
        enabled_sources=("core", "plugin"),
        enabled_skill_ids=("writing",), enabled_plugin_ids=("docs",),
        allowed_tool_ids=("memory.recall", "document.write"),
        preferred_model_tier="deep", max_tools=7,
    ).profile
    updated = store.exclude_tool(
        "project-a", tool_id="memory.recall", expected_revision=1,
    ).profile
    assert updated.revision == 2
    assert updated.allowed_tool_ids == ("document.write",)
    assert updated.denied_tool_ids == ("memory.recall",)
    assert updated.enabled_sources == original.enabled_sources
    assert updated.enabled_skill_ids == original.enabled_skill_ids
    assert updated.enabled_plugin_ids == original.enabled_plugin_ids
    assert updated.preferred_model_tier == "deep" and updated.max_tools == 7
    assert updated.boundary_profile_revision == original.boundary_profile_revision
    with pytest.raises(ProjectCapabilityProfileConflict):
        store.exclude_tool(
            "project-a", tool_id="document.write", expected_revision=1,
        )


def test_select_tool_binds_contract_and_preserves_auto_discovery_semantics(
    tmp_path: Path,
) -> None:
    store = ProjectCapabilityProfileStore(tmp_path)
    store.exclude_tool(
        "project-a", tool_id="memory.recall", expected_revision=1,
    )
    binding = ToolSelectionBinding(
        "memory.recall", f"contract-sha256:{'b' * 64}",
    )
    selected = store.select_tool(
        "project-a", tool_binding=binding, expected_revision=2,
        require_existing_exclusion=True,
    ).profile
    assert selected.allowed_tool_ids == ()
    assert selected.denied_tool_ids == ()
    assert selected.tool_selection_bindings == (binding,)


def test_select_tool_adds_allowlist_entry_for_confirm_new_and_requires_reset_target(
    tmp_path: Path,
) -> None:
    store = ProjectCapabilityProfileStore(tmp_path)
    store.update(
        "project-a", expected_revision=0,
        boundary_profile_id="project-boundary-project-a",
        boundary_profile_revision=1,
        tool_discovery_policy="confirm_new",
    )
    binding = ToolSelectionBinding(
        "memory.recall", f"contract-sha256:{'c' * 64}",
    )
    with pytest.raises(ProjectCapabilityProfileStoreError, match="exclusion"):
        store.select_tool(
            "project-a", tool_binding=binding, expected_revision=1,
            require_existing_exclusion=True,
        )
    selected = store.select_tool(
        "project-a", tool_binding=binding, expected_revision=1,
    ).profile
    assert selected.allowed_tool_ids == ("memory.recall",)
    assert selected.tool_selection_bindings == (binding,)


def test_mcp_rebind_is_cas_bound_and_all_narrow_updates_preserve_bindings(tmp_path: Path) -> None:
    store = ProjectCapabilityProfileStore(tmp_path)
    original = store.update(
        "project-a", expected_revision=0,
        boundary_profile_id="project-boundary-project-a", boundary_profile_revision=1,
        enabled_sources=("core", "mcp"), enabled_mcp_server_ids=("calendar",),
        mcp_server_selection_bindings=(_mcp_binding(),),
    ).profile
    excluded = store.exclude_tool(
        "project-a", tool_id="memory.recall", expected_revision=original.revision,
    ).profile
    assert excluded.mcp_server_selection_bindings == (_mcp_binding(),)
    rebound = store.rebind_mcp_server(
        "project-a", expected_revision=excluded.revision,
        server_id="calendar", protocol_profile="stateless_2026_07_28",
        manifest_revision=8, endpoint_identity="calendar-v2",
        credential_subject_id="local-user", transport_generation=3,
    ).profile
    assert rebound.mcp_server_selection_bindings == (_mcp_binding(
        manifest_revision=8, endpoint_identity="calendar-v2",
        transport_generation=3,
    ),)
    with pytest.raises(ProjectCapabilityProfileConflict):
        store.rebind_mcp_server(
            "project-a", expected_revision=excluded.revision,
            server_id="calendar", protocol_profile="stateless_2026_07_28",
            manifest_revision=8, endpoint_identity="calendar-v2",
            credential_subject_id="local-user", transport_generation=3,
        )
    updated = store.update(
        "project-a", expected_revision=rebound.revision,
        boundary_profile_id=rebound.boundary_profile_id,
        boundary_profile_revision=rebound.boundary_profile_revision,
        enabled_sources=rebound.enabled_sources,
        enabled_mcp_server_ids=rebound.enabled_mcp_server_ids,
    ).profile
    assert updated.mcp_server_selection_bindings == rebound.mcp_server_selection_bindings
    assert updated.tool_selection_bindings == rebound.tool_selection_bindings
