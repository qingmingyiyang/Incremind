from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.ai_runtime import get_or_build_ai_runtime
from backend.api.plugin_runtime import (
    build_plugin_package_intake,
    build_plugin_skill_activation,
    build_plugin_tool_activation,
    build_plugin_mcp_reference_activation,
)
from backend.api.plugin_hands_runtime import (
    PLUGIN_HANDS_CONTAINMENT_PROFILE,
    PLUGIN_HANDS_RECIPE_REVISION,
    PLUGIN_HANDS_RESOURCE_POLICY_REVISION,
    build_plugin_hands_activation,
    build_plugin_hands_artifacts,
    build_plugin_hands_upgrade_runtime,
    plugin_hands_capability,
)
from backend.api.plugin_hook_runtime import build_plugin_hook_activation
from backend.api.plugin_hands_lifecycle_projection import (
    LifecycleProjectionReadError,
    read_plugin_hands_lifecycle_attempt,
    serialize_plugin_hands_lifecycle_attempt,
)
from backend.security.project_capability_profiles import (
    ProjectCapabilityProfileConflict,
    ProjectCapabilityProfileStore,
    ProjectCapabilityProfileStoreError,
)
from backend.security.mcp_approved_servers import JsonMCPApprovedServerStore
from core.plugin_host import PluginPackageIntakeConflict, PluginPackageIntakeError
from core.plugin_host.hands_activation import PluginHandsActivationConflict, PluginHandsActivationError
from core.plugin_host.hands_artifact import PluginHandsArtifactConflict, PluginHandsArtifactError
from core.plugin_host.hands_upgrade import (
    PluginHandsUpgradeConflict,
    PluginHandsUpgradeError,
    PluginHandsUpgradeSnapshot,
)
from core.plugin_host.hook_activation import (
    PluginHookActivationConflict,
    PluginHookActivationError,
)
from core.ai_tooling import (
    MCPServerSelectionBinding,
    ToolSelectionBinding,
    tool_contract_binding_identity,
)


router = APIRouter(tags=["plugin-packages"])


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/hooks/{hook_id}/review")
async def review_plugin_hook(plugin_id: str, hook_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _body(request)
    fields = {"expected_state_revision", "expected_hand_activation_revision", "command_id", "confirm", "reason"}
    if body is None or set(body) != fields:
        return _response(400, {"status": "invalid_request", "reason": "exact Plugin Hook review fields are required"})
    try:
        payload = build_plugin_hook_activation(container.root_dir).review(
            plugin_id, hook_id=hook_id,
            expected_state_revision=body["expected_state_revision"],
            expected_hand_activation_revision=body["expected_hand_activation_revision"],
            command_id=body["command_id"], confirm=body["confirm"] is True,
            reason=body["reason"],
        )
    except PluginHookActivationConflict as error:
        return _response(409, {"status": "plugin_hook_conflict", "reason": str(error)})
    except PluginHookActivationError as error:
        return _response(400, {"status": "plugin_hook_rejected", "reason": str(error)})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/hooks/{hook_id}/activate")
async def activate_plugin_hook(plugin_id: str, hook_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _body(request)
    fields = {"expected_review_revision", "expected_activation_revision", "command_id", "confirm"}
    if body is None or set(body) != fields:
        return _response(400, {"status": "invalid_request", "reason": "exact Plugin Hook activation fields are required"})
    try:
        payload = build_plugin_hook_activation(container.root_dir).activate(
            plugin_id, hook_id=hook_id,
            expected_review_revision=body["expected_review_revision"],
            expected_activation_revision=body["expected_activation_revision"],
            command_id=body["command_id"], confirm=body["confirm"] is True,
        )
        _reconcile_plugin_hooks(request, container)
    except PluginHookActivationConflict as error:
        return _response(409, {"status": "plugin_hook_conflict", "reason": str(error)})
    except PluginHookActivationError as error:
        return _response(400, {"status": "plugin_hook_rejected", "reason": str(error)})
    except Exception:
        _fail_closed_plugin_hooks(request)
        return _response(503, {"status": "plugin_hook_runtime_unavailable"})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/hooks/{hook_id}/disable")
async def disable_plugin_hook(plugin_id: str, hook_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _body(request)
    fields = {"expected_activation_revision", "command_id", "reason"}
    if body is None or set(body) != fields:
        return _response(400, {"status": "invalid_request", "reason": "exact Plugin Hook disable fields are required"})
    try:
        payload = build_plugin_hook_activation(container.root_dir).disable(
            plugin_id, hook_id=hook_id,
            expected_activation_revision=body["expected_activation_revision"],
            command_id=body["command_id"], reason=body["reason"],
        )
        _reconcile_plugin_hooks(request, container)
    except PluginHookActivationConflict as error:
        return _response(409, {"status": "plugin_hook_conflict", "reason": str(error)})
    except PluginHookActivationError as error:
        return _response(400, {"status": "plugin_hook_rejected", "reason": str(error)})
    except Exception:
        _fail_closed_plugin_hooks(request)
        return _response(503, {"status": "plugin_hook_runtime_unavailable"})
    return _response(200, payload)


@router.get("/api/ai/governance/plugins/packages")
async def plugin_package_snapshot(container: ApiContainerDep) -> JSONResponse:
    try:
        payload = build_plugin_package_intake(container.root_dir).snapshot()
    except PluginPackageIntakeError as error:
        return _response(409, {"status": "plugin_authority_unavailable", "reason": str(error)})
    return _response(200, payload)


@router.get("/api/ai/governance/plugins/packages/{plugin_id}/hands/{hand_id}/attempts/{attempt_id}")
async def plugin_hand_lifecycle_attempt(
    plugin_id: str,
    hand_id: str,
    attempt_id: str,
    container: ApiContainerDep,
) -> JSONResponse:
    """Expose one retained attempt as a non-replaying audit projection."""

    try:
        projection = read_plugin_hands_lifecycle_attempt(container.root_dir, attempt_id)
    except LifecycleProjectionReadError as error:
        return _response(400, {"status": "plugin_hand_attempt_rejected", "reason": str(error)})
    if projection is None or (projection.plugin_id, projection.hand_id) != (plugin_id, hand_id):
        return _response(404, {"status": "plugin_hand_attempt_not_found"})
    return _response(200, serialize_plugin_hands_lifecycle_attempt(projection))


@router.post("/api/ai/governance/plugins/packages/discover")
async def discover_plugin_package(request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _body(request)
    if body is None or set(body) != {"source_path", "command_id"}:
        return _response(400, {"status": "invalid_request", "reason": "exact source_path and command_id are required"})
    if not isinstance(body["source_path"], str) or not isinstance(body["command_id"], str):
        return _response(400, {"status": "invalid_request", "reason": "source_path and command_id must be strings"})
    try:
        payload = build_plugin_package_intake(container.root_dir).discover(
            body["source_path"], command_id=body["command_id"]
        )
    except PluginPackageIntakeConflict as error:
        return _response(409, {"status": "plugin_package_conflict", "reason": str(error)})
    except PluginPackageIntakeError as error:
        return _response(400, {"status": "plugin_package_rejected", "reason": str(error)})
    return _response(201, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/install-disabled")
async def install_plugin_package_disabled(
    plugin_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    body = await _body(request)
    if body is None or set(body) != {"expected_state_revision", "command_id", "confirm"}:
        return _response(400, {"status": "invalid_request", "reason": "exact install confirmation fields are required"})
    if not isinstance(body["command_id"], str):
        return _response(400, {"status": "invalid_request", "reason": "command_id must be a string"})
    try:
        payload = build_plugin_package_intake(container.root_dir).install_disabled(
            plugin_id,
            expected_state_revision=body["expected_state_revision"],
            command_id=body["command_id"],
            confirm=body["confirm"] is True,
        )
    except PluginPackageIntakeConflict as error:
        return _response(409, {"status": "plugin_package_conflict", "reason": str(error)})
    except PluginPackageIntakeError as error:
        return _response(400, {"status": "plugin_package_rejected", "reason": str(error)})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/skills/review")
async def review_plugin_skills(plugin_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _body(request)
    fields = {"skill_ids", "expected_state_revision", "command_id", "confirm", "reason"}
    if body is None or set(body) != fields:
        return _response(400, {"status": "invalid_request", "reason": "exact Skill review fields are required"})
    try:
        payload = build_plugin_skill_activation(container.root_dir).review(
            plugin_id,
            skill_ids=body["skill_ids"],
            expected_state_revision=body["expected_state_revision"],
            command_id=body["command_id"],
            confirm=body["confirm"] is True,
            reason=body["reason"],
        )
    except PluginPackageIntakeConflict as error:
        return _response(409, {"status": "plugin_skill_conflict", "reason": str(error)})
    except PluginPackageIntakeError as error:
        return _response(400, {"status": "plugin_skill_rejected", "reason": str(error)})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/skills/activate")
async def activate_plugin_skills(plugin_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _body(request)
    if body is None or set(body) != {"expected_review_revision", "expected_activation_revision", "command_id", "confirm"}:
        return _response(400, {"status": "invalid_request", "reason": "exact Skill activation fields are required"})
    try:
        payload = build_plugin_skill_activation(container.root_dir).activate(
            plugin_id,
            expected_review_revision=body["expected_review_revision"],
            expected_activation_revision=body["expected_activation_revision"],
            command_id=body["command_id"],
            confirm=body["confirm"] is True,
        )
    except PluginPackageIntakeConflict as error:
        return _response(409, {"status": "plugin_skill_conflict", "reason": str(error)})
    except PluginPackageIntakeError as error:
        return _response(400, {"status": "plugin_skill_rejected", "reason": str(error)})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/skills/disable")
async def disable_plugin_skills(plugin_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _body(request)
    fields = {"expected_activation_revision", "command_id", "confirm", "reason"}
    if body is None or set(body) != fields:
        return _response(400, {"status": "invalid_request", "reason": "exact Skill disable fields are required"})
    try:
        payload = build_plugin_skill_activation(container.root_dir).disable(
            plugin_id,
            expected_activation_revision=body["expected_activation_revision"],
            command_id=body["command_id"],
            confirm=body["confirm"] is True,
            reason=body["reason"],
        )
    except PluginPackageIntakeConflict as error:
        return _response(409, {"status": "plugin_skill_conflict", "reason": str(error)})
    except PluginPackageIntakeError as error:
        return _response(400, {"status": "plugin_skill_rejected", "reason": str(error)})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/tools/review")
async def review_plugin_tools(plugin_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _body(request)
    fields = {"tool_ids", "expected_state_revision", "command_id", "confirm", "reason"}
    if body is None or set(body) != fields:
        return _response(400, {"status": "invalid_request", "reason": "exact Tool review fields are required"})
    try:
        payload = build_plugin_tool_activation(container.root_dir).review(
            plugin_id, tool_ids=body["tool_ids"], expected_state_revision=body["expected_state_revision"],
            command_id=body["command_id"], confirm=body["confirm"] is True, reason=body["reason"],
        )
    except PluginPackageIntakeConflict as error:
        return _response(409, {"status": "plugin_tool_conflict", "reason": str(error)})
    except PluginPackageIntakeError as error:
        return _response(400, {"status": "plugin_tool_rejected", "reason": str(error)})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/tools/activate")
async def activate_plugin_tools(plugin_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _body(request)
    fields = {"expected_review_revision", "expected_activation_revision", "command_id", "confirm"}
    if body is None or set(body) != fields:
        return _response(400, {"status": "invalid_request", "reason": "exact Tool activation fields are required"})
    try:
        payload = build_plugin_tool_activation(container.root_dir).activate(
            plugin_id, expected_review_revision=body["expected_review_revision"],
            expected_activation_revision=body["expected_activation_revision"],
            command_id=body["command_id"], confirm=body["confirm"] is True,
        )
        conflicts = _reconcile_plugin_tools(request)
        activation = payload.get("activation")
        activated_tool_ids = {
            item.get("id") for item in activation.get("tools", [])
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        } if isinstance(activation, Mapping) else set()
        own_conflicts = tuple(sorted(activated_tool_ids & set(conflicts)))
        if own_conflicts:
            return _response(409, dict(payload) | {
                "status": "plugin_tool_runtime_conflict",
                "runtime_status": "withheld",
                "reason": "Plugin Tool capability id collides with an existing runtime capability",
                "conflicting_tool_ids": list(own_conflicts),
            })
    except PluginPackageIntakeConflict as error:
        return _response(409, {"status": "plugin_tool_conflict", "reason": str(error)})
    except PluginPackageIntakeError as error:
        return _response(400, {"status": "plugin_tool_rejected", "reason": str(error)})
    except Exception:
        # Durable activation succeeded, but an already-running process cannot
        # safely expose a partially reconciled capability set. The manager is
        # fail-closed before re-raising this availability response.
        return _response(503, {"status": "plugin_tool_runtime_unavailable"})
    if conflicts:
        payload = dict(payload) | {
            "runtime_status": "active",
            "other_conflicting_tool_ids": list(conflicts),
        }
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/tools/disable")
async def disable_plugin_tools(plugin_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _body(request)
    fields = {"expected_activation_revision", "command_id", "confirm", "reason"}
    if body is None or set(body) != fields:
        return _response(400, {"status": "invalid_request", "reason": "exact Tool disable fields are required"})
    try:
        payload = build_plugin_tool_activation(container.root_dir).disable(
            plugin_id, expected_activation_revision=body["expected_activation_revision"],
            command_id=body["command_id"], confirm=body["confirm"] is True, reason=body["reason"],
        )
        _reconcile_plugin_tools(request)
    except PluginPackageIntakeConflict as error:
        return _response(409, {"status": "plugin_tool_conflict", "reason": str(error)})
    except PluginPackageIntakeError as error:
        return _response(400, {"status": "plugin_tool_rejected", "reason": str(error)})
    except Exception:
        return _response(503, {"status": "plugin_tool_runtime_unavailable"})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/tools/projects/{project_id}/enable")
async def enable_plugin_tools_for_project(
    plugin_id: str, project_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    """Authorize one active Plugin as a project source, never a Tool selection.

    A project must separately select each native Tool through the existing
    capability-selection command, keeping plugin admission and tool dispatch
    approval as two distinct durable decisions.
    """
    body = await _body(request)
    if body is None or set(body) != {"expected_profile_revision", "confirm"}:
        return _response(400, {"status": "invalid_request", "reason": "exact Plugin Tool project fields are required"})
    expected = body.get("expected_profile_revision")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0 or body.get("confirm") is not True:
        return _response(400, {"status": "invalid_request", "reason": "confirmed project profile revision is required"})
    try:
        if not build_plugin_tool_activation(container.root_dir).active_contributions((plugin_id,)):
            raise PluginPackageIntakeError("Plugin Tool is not durably active")
        profiles = ProjectCapabilityProfileStore(container.root_dir)
        snapshot = profiles.get(project_id)
        if expected != snapshot.store_revision:
            raise ProjectCapabilityProfileConflict("project capability profile revision conflict")
        profile = snapshot.profile
        updated = profiles.update(
            project_id,
            expected_revision=expected,
            boundary_profile_id=profile.boundary_profile_id,
            boundary_profile_revision=profile.boundary_profile_revision,
            enabled_sources=tuple(dict.fromkeys((*profile.enabled_sources, "plugin"))),
            enabled_skill_ids=profile.enabled_skill_ids,
            enabled_plugin_ids=tuple(dict.fromkeys((*profile.enabled_plugin_ids, plugin_id))),
            enabled_mcp_server_ids=profile.enabled_mcp_server_ids,
            allowed_tool_ids=profile.allowed_tool_ids,
            denied_tool_ids=profile.denied_tool_ids,
            preferred_model_tier=profile.preferred_model_tier,
            memory_scope=profile.memory_scope,
            cross_project_grant_ids=profile.cross_project_grant_ids,
            output_style_profile_id=profile.output_style_profile_id,
            max_tools=profile.max_tools,
            max_tool_descriptor_bytes=profile.max_tool_descriptor_bytes,
            tool_discovery_policy=profile.tool_discovery_policy,
            tool_selection_bindings=profile.tool_selection_bindings,
        )
    except ProjectCapabilityProfileConflict as error:
        return _response(409, {"status": "plugin_tool_project_conflict", "reason": str(error)})
    except (PluginPackageIntakeError, ProjectCapabilityProfileStoreError, ValueError) as error:
        return _response(400, {"status": "plugin_tool_project_rejected", "reason": str(error)})
    return _response(200, {"profile": asdict(updated.profile), "profile_revision": updated.store_revision})


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/hands/{hand_id}/review")
async def review_plugin_hand(plugin_id: str, hand_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _body(request)
    if body is None or set(body) != {"expected_state_revision", "command_id", "confirm", "reason"}:
        return _response(400, {"status": "invalid_request", "reason": "exact Plugin Hand review fields are required"})
    try:
        payload = build_plugin_hands_artifacts(container.root_dir).review(
            plugin_id, hand_id=hand_id, expected_state_revision=body["expected_state_revision"],
            command_id=body["command_id"], confirm=body["confirm"] is True,
            reason=body["reason"], containment_profile_revision=PLUGIN_HANDS_CONTAINMENT_PROFILE,
        )
    except PluginHandsArtifactConflict as error:
        return _response(409, {"status": "plugin_hand_conflict", "reason": str(error)})
    except PluginHandsArtifactError as error:
        return _response(400, {"status": "plugin_hand_rejected", "reason": str(error)})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/hands/{hand_id}/materialize")
async def materialize_plugin_hand(plugin_id: str, hand_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _body(request)
    fields = {"expected_review_revision", "expected_materialization_revision", "command_id", "confirm"}
    if body is None or set(body) != fields:
        return _response(400, {"status": "invalid_request", "reason": "exact Plugin Hand materialization fields are required"})
    try:
        artifacts = build_plugin_hands_artifacts(container.root_dir)
        payload = artifacts.materialize(
            plugin_id, hand_id=hand_id,
            expected_review_revision=body["expected_review_revision"],
            expected_materialization_revision=body["expected_materialization_revision"],
            command_id=body["command_id"], confirm=body["confirm"] is True,
        )
        artifact = artifacts.resolve(plugin_id, hand_id=hand_id)
        payload = dict(payload) | {"materialization_revision": artifact.materialization_revision}
    except PluginHandsArtifactConflict as error:
        return _response(409, {"status": "plugin_hand_conflict", "reason": str(error)})
    except PluginHandsArtifactError as error:
        return _response(400, {"status": "plugin_hand_rejected", "reason": str(error)})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/hands/{hand_id}/activate")
async def activate_plugin_hand(plugin_id: str, hand_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _body(request)
    fields = {"expected_review_revision", "expected_materialization_revision", "expected_activation_revision", "command_id", "confirm"}
    if body is None or set(body) != fields:
        return _response(400, {"status": "invalid_request", "reason": "exact Plugin Hand activation fields are required"})
    try:
        payload = build_plugin_hands_activation(container.root_dir).activate(
            plugin_id, hand_id=hand_id,
            expected_review_revision=body["expected_review_revision"],
            expected_materialization_revision=body["expected_materialization_revision"],
            expected_activation_revision=body["expected_activation_revision"],
            containment_profile_revision=PLUGIN_HANDS_CONTAINMENT_PROFILE,
            command_id=body["command_id"], confirm=body["confirm"] is True,
        )
        conflicts = _reconcile_plugin_hands(request)
        capability_id = f"plugin.hand.{plugin_id}.{hand_id}"
        if capability_id in conflicts:
            return _response(409, dict(payload) | {
                "status": "plugin_hand_runtime_conflict", "runtime_status": "withheld",
                "conflicting_capability_ids": [capability_id],
            })
    except PluginHandsActivationConflict as error:
        return _response(409, {"status": "plugin_hand_conflict", "reason": str(error)})
    except PluginHandsActivationError as error:
        return _response(400, {"status": "plugin_hand_rejected", "reason": str(error)})
    except Exception:
        return _response(503, {"status": "plugin_hand_runtime_unavailable"})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/hands/{hand_id}/upgrades/stage-candidate")
async def stage_plugin_hand_upgrade_candidate(
    plugin_id: str, hand_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    """Freeze an upgrade package without moving the executable Hand pointer."""

    body = await _body(request)
    fields = {"source_path", "expected_state_revision", "expected_package_record_id", "command_id", "confirm"}
    if body is None or set(body) != fields:
        return _response(400, {"status": "invalid_request", "reason": "exact Plugin Hand upgrade staging fields are required"})
    try:
        payload = build_plugin_package_intake(container.root_dir).stage_upgrade_candidate(
            body["source_path"], plugin_id=plugin_id,
            expected_state_revision=body["expected_state_revision"],
            expected_package_record_id=body["expected_package_record_id"],
            command_id=body["command_id"], confirm=body["confirm"] is True,
        )
    except PluginPackageIntakeConflict as error:
        return _response(409, {"status": "plugin_hand_upgrade_conflict", "reason": str(error)})
    except PluginPackageIntakeError as error:
        return _response(400, {"status": "plugin_hand_upgrade_rejected", "reason": str(error)})
    return _response(201, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/hands/{hand_id}/upgrades/begin")
async def begin_plugin_hand_upgrade(
    plugin_id: str, hand_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    """Create the cutover before reviewing its isolated candidate artifact slot.

    Candidate slots are write-once, therefore their first review is revision
    one and materialization reaches revision two (intent then ready).  The
    activation revision is the next CAS revision of the one executable
    pointer.
    """

    body = await _body(request)
    fields = {"cutover_id", "candidate_package_record_id", "expected_activation_revision", "confirm"}
    if body is None or set(body) != fields:
        return _response(400, {"status": "invalid_request", "reason": "exact Plugin Hand upgrade begin fields are required"})
    if body.get("confirm") is not True:
        return _response(400, {"status": "invalid_request", "reason": "Plugin Hand upgrade begin requires confirmation"})
    try:
        activation = build_plugin_hands_activation(container.root_dir)
        active = activation.resolve_active(plugin_id, hand_id=hand_id)
        if active is None:
            raise PluginHandsActivationError("Plugin Hand is not durably active")
        if active.activation_revision != body["expected_activation_revision"]:
            raise PluginHandsActivationConflict("Plugin Hand activation revision conflict")
        old = _upgrade_snapshot_from_active(active)
        new = PluginHandsUpgradeSnapshot(
            package_record_id=body["candidate_package_record_id"], review_revision=1,
            materialization_revision=2, activation_revision=active.activation_revision + 1,
            runtime_revision=PLUGIN_HANDS_RECIPE_REVISION,
            resource_policy_revision=PLUGIN_HANDS_RESOURCE_POLICY_REVISION,
        )
        runtime = _plugin_hands_upgrade_runtime(request, container.root_dir, activation)
        preview = runtime.authority.preview(plugin_id, hand_id=hand_id, old=old, new=new)
        payload = runtime.authority.begin(
            body["cutover_id"], plugin_id, hand_id=hand_id, old=old, new=new,
        )
        payload["preview"] = {
            "automatic_rollback_blocked_for_write_attempts": preview.automatic_rollback_blocked_for_write_attempts,
        }
    except (PluginHandsActivationConflict, PluginHandsUpgradeConflict) as error:
        return _response(409, {"status": "plugin_hand_upgrade_conflict", "reason": str(error)})
    except (PluginHandsActivationError, PluginHandsUpgradeError, ValueError) as error:
        return _response(400, {"status": "plugin_hand_upgrade_rejected", "reason": str(error)})
    except RuntimeError:
        return _response(503, {"status": "plugin_hand_upgrade_runtime_unavailable"})
    return _response(201, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/hands/{hand_id}/upgrades/{cutover_id}/candidate/review")
async def review_plugin_hand_upgrade_candidate(
    plugin_id: str, hand_id: str, cutover_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await _body(request)
    fields = {"expected_state_revision", "command_id", "confirm", "reason"}
    if body is None or set(body) != fields:
        return _response(400, {"status": "invalid_request", "reason": "exact Plugin Hand candidate review fields are required"})
    try:
        payload = build_plugin_hands_artifacts(container.root_dir).review(
            plugin_id, hand_id=hand_id, expected_state_revision=body["expected_state_revision"],
            command_id=body["command_id"], confirm=body["confirm"] is True, reason=body["reason"],
            containment_profile_revision=PLUGIN_HANDS_CONTAINMENT_PROFILE,
            candidate_cutover_id=cutover_id,
        )
    except PluginHandsArtifactConflict as error:
        return _response(409, {"status": "plugin_hand_upgrade_conflict", "reason": str(error)})
    except PluginHandsArtifactError as error:
        return _response(400, {"status": "plugin_hand_upgrade_rejected", "reason": str(error)})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/hands/{hand_id}/upgrades/{cutover_id}/candidate/materialize")
async def materialize_plugin_hand_upgrade_candidate(
    plugin_id: str, hand_id: str, cutover_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await _body(request)
    fields = {"expected_review_revision", "expected_materialization_revision", "command_id", "confirm"}
    if body is None or set(body) != fields:
        return _response(400, {"status": "invalid_request", "reason": "exact Plugin Hand candidate materialization fields are required"})
    try:
        artifacts = build_plugin_hands_artifacts(container.root_dir)
        payload = artifacts.materialize(
            plugin_id, hand_id=hand_id, expected_review_revision=body["expected_review_revision"],
            expected_materialization_revision=body["expected_materialization_revision"],
            command_id=body["command_id"], confirm=body["confirm"] is True,
            candidate_cutover_id=cutover_id,
        )
        artifact = artifacts.resolve(plugin_id, hand_id=hand_id, candidate_cutover_id=cutover_id)
        payload = dict(payload) | {"materialization_revision": artifact.materialization_revision}
    except PluginHandsArtifactConflict as error:
        return _response(409, {"status": "plugin_hand_upgrade_conflict", "reason": str(error)})
    except PluginHandsArtifactError as error:
        return _response(400, {"status": "plugin_hand_upgrade_rejected", "reason": str(error)})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/hands/{hand_id}/upgrades/{cutover_id}/resume")
async def resume_plugin_hand_upgrade(
    plugin_id: str, hand_id: str, cutover_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await _body(request)
    if body is None or set(body) != {"confirm"} or body.get("confirm") is not True:
        return _response(400, {"status": "invalid_request", "reason": "confirmed Plugin Hand upgrade resume is required"})
    try:
        runtime = _plugin_hands_upgrade_runtime(request, container.root_dir)
        _require_upgrade_identity(runtime.authority.load(cutover_id), plugin_id, hand_id)
        payload = runtime.authority.resume(cutover_id, ports=runtime.ports)
    except PluginHandsUpgradeConflict as error:
        return _response(409, {"status": "plugin_hand_upgrade_conflict", "reason": str(error)})
    except (PluginHandsUpgradeError, ValueError) as error:
        return _response(400, {"status": "plugin_hand_upgrade_rejected", "reason": str(error)})
    except RuntimeError:
        return _response(503, {"status": "plugin_hand_upgrade_runtime_unavailable"})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/hands/{hand_id}/upgrades/{cutover_id}/manual-rollback")
async def rollback_plugin_hand_upgrade(
    plugin_id: str, hand_id: str, cutover_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await _body(request)
    if body is None or set(body) != {"confirm", "rollback_reason"} or body.get("confirm") is not True:
        return _response(400, {"status": "invalid_request", "reason": "confirmed opaque Plugin Hand rollback reason is required"})
    try:
        runtime = _plugin_hands_upgrade_runtime(request, container.root_dir)
        _require_upgrade_identity(runtime.authority.load(cutover_id), plugin_id, hand_id)
        payload = runtime.authority.rollback(
            cutover_id, ports=runtime.ports, automatic=False, confirm=True, reason=body["rollback_reason"],
        )
    except PluginHandsUpgradeConflict as error:
        return _response(409, {"status": "plugin_hand_upgrade_conflict", "reason": str(error)})
    except (PluginHandsUpgradeError, ValueError) as error:
        return _response(400, {"status": "plugin_hand_upgrade_rejected", "reason": str(error)})
    except RuntimeError:
        return _response(503, {"status": "plugin_hand_upgrade_runtime_unavailable"})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/hands/{hand_id}/upgrades/{cutover_id}/finalize")
async def finalize_plugin_hand_upgrade(
    plugin_id: str, hand_id: str, cutover_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await _body(request)
    if body is None or set(body) != {"confirm"} or body.get("confirm") is not True:
        return _response(400, {"status": "invalid_request", "reason": "confirmed Plugin Hand upgrade finalize is required"})
    try:
        runtime = _plugin_hands_upgrade_runtime(request, container.root_dir)
        _require_upgrade_identity(runtime.authority.load(cutover_id), plugin_id, hand_id)
        payload = runtime.authority.finalize(cutover_id)
    except PluginHandsUpgradeConflict as error:
        return _response(409, {"status": "plugin_hand_upgrade_conflict", "reason": str(error)})
    except (PluginHandsUpgradeError, ValueError) as error:
        return _response(400, {"status": "plugin_hand_upgrade_rejected", "reason": str(error)})
    except RuntimeError:
        return _response(503, {"status": "plugin_hand_upgrade_runtime_unavailable"})
    return _response(200, payload)


@router.get("/api/ai/governance/plugins/packages/{plugin_id}/hands/{hand_id}/upgrades/{cutover_id}")
async def load_plugin_hand_upgrade(
    plugin_id: str, hand_id: str, cutover_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    try:
        payload = _plugin_hands_upgrade_runtime(request, container.root_dir).authority.load(cutover_id)
        _require_upgrade_identity(payload, plugin_id, hand_id)
    except PluginHandsUpgradeConflict as error:
        return _response(409, {"status": "plugin_hand_upgrade_conflict", "reason": str(error)})
    except (PluginHandsUpgradeError, ValueError) as error:
        return _response(400, {"status": "plugin_hand_upgrade_rejected", "reason": str(error)})
    except RuntimeError:
        return _response(503, {"status": "plugin_hand_upgrade_runtime_unavailable"})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/hands/{hand_id}/disable")
async def disable_plugin_hand(plugin_id: str, hand_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _body(request)
    if body is None or set(body) != {"expected_activation_revision", "command_id", "confirm", "reason"}:
        return _response(400, {"status": "invalid_request", "reason": "exact Plugin Hand disable fields are required"})
    try:
        payload = build_plugin_hands_activation(container.root_dir).disable(
            plugin_id, hand_id=hand_id, expected_activation_revision=body["expected_activation_revision"],
            command_id=body["command_id"], confirm=body["confirm"] is True, reason=body["reason"],
        )
        _reconcile_plugin_hands(request)
    except PluginHandsActivationConflict as error:
        return _response(409, {"status": "plugin_hand_conflict", "reason": str(error)})
    except PluginHandsActivationError as error:
        return _response(400, {"status": "plugin_hand_rejected", "reason": str(error)})
    except Exception:
        return _response(503, {"status": "plugin_hand_runtime_unavailable"})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/hands/{hand_id}/projects/{project_id}/enable")
async def enable_plugin_hand_for_project(
    plugin_id: str, hand_id: str, project_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await _body(request)
    if body is None or set(body) != {"expected_profile_revision", "confirm"}:
        return _response(400, {"status": "invalid_request", "reason": "exact Plugin Hand project fields are required"})
    expected = body.get("expected_profile_revision")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0 or body.get("confirm") is not True:
        return _response(400, {"status": "invalid_request", "reason": "confirmed project profile revision is required"})
    try:
        active = build_plugin_hands_activation(container.root_dir).resolve_active(plugin_id, hand_id=hand_id)
        if active is None:
            raise PluginHandsActivationError("Plugin Hand is not durably active")
        profiles = ProjectCapabilityProfileStore(container.root_dir)
        snapshot = profiles.get(project_id)
        if expected != snapshot.store_revision:
            raise ProjectCapabilityProfileConflict("project capability profile revision conflict")
        profile = snapshot.profile
        tool = plugin_hands_capability(active).tool_definition
        assert tool is not None
        allowed_tool_ids = tuple(dict.fromkeys((*profile.allowed_tool_ids, tool.tool_id)))
        selection_bindings = tuple(
            item for item in profile.tool_selection_bindings if item.stable_id != tool.tool_id
        ) + (ToolSelectionBinding(tool.tool_id, tool_contract_binding_identity(tool)),)
        updated = profiles.update(
            project_id, expected_revision=expected,
            boundary_profile_id=profile.boundary_profile_id,
            boundary_profile_revision=profile.boundary_profile_revision,
            enabled_sources=tuple(dict.fromkeys((*profile.enabled_sources, "plugin"))),
            enabled_skill_ids=profile.enabled_skill_ids,
            enabled_plugin_ids=tuple(dict.fromkeys((*profile.enabled_plugin_ids, plugin_id))),
            enabled_mcp_server_ids=profile.enabled_mcp_server_ids,
            allowed_tool_ids=allowed_tool_ids,
            denied_tool_ids=tuple(item for item in profile.denied_tool_ids if item != tool.tool_id),
            preferred_model_tier=profile.preferred_model_tier, memory_scope=profile.memory_scope,
            cross_project_grant_ids=profile.cross_project_grant_ids,
            output_style_profile_id=profile.output_style_profile_id,
            max_tools=profile.max_tools, max_tool_descriptor_bytes=profile.max_tool_descriptor_bytes,
            tool_discovery_policy=profile.tool_discovery_policy,
            tool_selection_bindings=selection_bindings,
        )
    except ProjectCapabilityProfileConflict as error:
        return _response(409, {"status": "plugin_hand_project_conflict", "reason": str(error)})
    except (PluginHandsActivationError, PluginHandsActivationConflict, ProjectCapabilityProfileStoreError, ValueError) as error:
        return _response(400, {"status": "plugin_hand_project_rejected", "reason": str(error)})
    return _response(200, {
        "profile": asdict(updated.profile), "profile_revision": updated.store_revision,
        "tool": asdict(tool),
    })


def _reconcile_plugin_tools(request: Request) -> tuple[str, ...]:
    manager = getattr(request.app.state, "plugin_tool_registration_manager", None)
    if manager is not None:
        manager.reconcile()
        return tuple(manager.conflicting_tool_ids)
    return ()


def _reconcile_plugin_hooks(request: Request, container: ApiContainerDep) -> None:
    get_or_build_ai_runtime(request, container)
    manager = getattr(request.app.state, "plugin_hook_projection_manager", None)
    if manager is None:
        raise RuntimeError("Plugin Hook projection manager is unavailable")
    manager.reconcile()


def _fail_closed_plugin_hooks(request: Request) -> None:
    manager = getattr(request.app.state, "plugin_hook_projection_manager", None)
    if manager is None:
        return
    try:
        manager.fail_closed()
    except Exception:
        return


def _reconcile_plugin_hands(request: Request) -> tuple[str, ...]:
    manager = getattr(request.app.state, "plugin_hands_registration_manager", None)
    if manager is not None:
        manager.reconcile()
        return tuple(manager.conflicting_capability_ids)
    return ()


def _plugin_hands_upgrade_runtime(request: Request, root_dir, activation=None):
    """Use the application's sole Hands Registry projection for cutovers."""

    manager = getattr(request.app.state, "plugin_hands_registration_manager", None)
    if manager is None:
        raise RuntimeError("Plugin Hands Registry runtime is unavailable")
    return build_plugin_hands_upgrade_runtime(
        root_dir=root_dir,
        activation=activation or build_plugin_hands_activation(root_dir),
        registration_manager=manager,
    )


def _upgrade_snapshot_from_active(active) -> PluginHandsUpgradeSnapshot:
    return PluginHandsUpgradeSnapshot(
        package_record_id=active.package_record_id,
        review_revision=active.review_revision,
        materialization_revision=active.materialization_revision,
        activation_revision=active.activation_revision,
        runtime_revision=PLUGIN_HANDS_RECIPE_REVISION,
        resource_policy_revision=PLUGIN_HANDS_RESOURCE_POLICY_REVISION,
    )


def _require_upgrade_identity(payload: Mapping[str, object] | None, plugin_id: str, hand_id: str) -> None:
    if payload is None:
        raise PluginHandsUpgradeError("Plugin Hands upgrade is missing")
    if payload.get("plugin_id") != plugin_id or payload.get("hand_id") != hand_id:
        raise PluginHandsUpgradeConflict("Plugin Hands upgrade identity does not match this route")


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/mcp/review")
async def review_plugin_mcp_reference(plugin_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _body(request)
    fields = {"expected_state_revision", "command_id", "confirm", "reason"}
    if body is None or set(body) != fields:
        return _response(400, {"status": "invalid_request", "reason": "exact MCP reference review fields are required"})
    try:
        payload = build_plugin_mcp_reference_activation(container.root_dir).review(
            plugin_id, expected_state_revision=body["expected_state_revision"],
            command_id=body["command_id"], confirm=body["confirm"] is True, reason=body["reason"],
        )
    except PluginPackageIntakeConflict as error:
        return _response(409, {"status": "plugin_mcp_reference_conflict", "reason": str(error)})
    except PluginPackageIntakeError as error:
        return _response(400, {"status": "plugin_mcp_reference_rejected", "reason": str(error)})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/mcp/activate")
async def activate_plugin_mcp_reference(plugin_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _body(request)
    fields = {"expected_review_revision", "expected_activation_revision", "command_id", "confirm"}
    if body is None or set(body) != fields:
        return _response(400, {"status": "invalid_request", "reason": "exact MCP reference activation fields are required"})
    try:
        payload = build_plugin_mcp_reference_activation(container.root_dir).activate(
            plugin_id, expected_review_revision=body["expected_review_revision"],
            expected_activation_revision=body["expected_activation_revision"],
            command_id=body["command_id"], confirm=body["confirm"] is True,
        )
    except PluginPackageIntakeConflict as error:
        return _response(409, {"status": "plugin_mcp_reference_conflict", "reason": str(error)})
    except PluginPackageIntakeError as error:
        return _response(400, {"status": "plugin_mcp_reference_rejected", "reason": str(error)})
    return _response(200, payload)


@router.post("/api/ai/governance/plugins/packages/{plugin_id}/mcp/projects/{project_id}/enable")
async def enable_plugin_mcp_for_project(
    plugin_id: str, project_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await _body(request)
    if body is None or set(body) != {"expected_profile_revision", "confirm"}:
        return _response(400, {"status": "invalid_request", "reason": "exact Plugin MCP project fields are required"})
    expected = body.get("expected_profile_revision")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0 or body.get("confirm") is not True:
        return _response(400, {"status": "invalid_request", "reason": "confirmed project profile revision is required"})
    try:
        bindings = build_plugin_mcp_reference_activation(container.root_dir).active_contributions((plugin_id,))
        if len(bindings) != 1:
            raise PluginPackageIntakeError("Plugin MCP reference is not durably active")
        server_id = bindings[0].server_id
        approved = JsonMCPApprovedServerStore(container.root_dir).snapshot()
        record = next(
            (item for item in approved.enabled_servers if item.server_id == server_id), None,
        )
        if record is None:
            raise PluginPackageIntakeError("Plugin MCP approved server is unavailable")
        host = record.host_connection
        mcp_binding = MCPServerSelectionBinding(
            server_id=server_id,
            protocol_profile=host.protocol_profile,
            manifest_revision=host.manifest_revision,
            endpoint_identity=host.endpoint_identity,
            credential_subject_id=host.credential_subject_id,
            transport_generation=host.transport_generation,
        )
        profiles = ProjectCapabilityProfileStore(container.root_dir)
        snapshot = profiles.get(project_id)
        if expected != snapshot.store_revision:
            raise ProjectCapabilityProfileConflict("project capability profile revision conflict")
        profile = snapshot.profile
        updated = profiles.update(
            project_id, expected_revision=expected,
            boundary_profile_id=profile.boundary_profile_id,
            boundary_profile_revision=profile.boundary_profile_revision,
            enabled_sources=tuple(dict.fromkeys((*profile.enabled_sources, "mcp"))),
            enabled_skill_ids=profile.enabled_skill_ids,
            enabled_plugin_ids=profile.enabled_plugin_ids,
            enabled_mcp_server_ids=tuple(dict.fromkeys((*profile.enabled_mcp_server_ids, server_id))),
            allowed_tool_ids=profile.allowed_tool_ids, denied_tool_ids=profile.denied_tool_ids,
            preferred_model_tier=profile.preferred_model_tier, memory_scope=profile.memory_scope,
            cross_project_grant_ids=profile.cross_project_grant_ids,
            output_style_profile_id=profile.output_style_profile_id,
            max_tools=profile.max_tools, max_tool_descriptor_bytes=profile.max_tool_descriptor_bytes,
            tool_discovery_policy=profile.tool_discovery_policy,
            tool_selection_bindings=profile.tool_selection_bindings,
            mcp_server_selection_bindings=tuple(
                item for item in profile.mcp_server_selection_bindings
                if item.server_id != server_id
            ) + (mcp_binding,),
        )
    except ProjectCapabilityProfileConflict as error:
        return _response(409, {"status": "plugin_mcp_project_conflict", "reason": str(error)})
    except (PluginPackageIntakeError, ProjectCapabilityProfileStoreError, ValueError) as error:
        return _response(400, {"status": "plugin_mcp_project_rejected", "reason": str(error)})
    return _response(200, {"profile": asdict(updated.profile), "profile_revision": updated.store_revision})




async def _body(request: Request) -> Mapping[str, Any] | None:
    try:
        payload = await request.json()
    except Exception:
        return None
    return payload if isinstance(payload, Mapping) else None


def _response(status_code: int, payload: Mapping[str, object]) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=dict(payload),
        headers={"Cache-Control": "no-store"},
    )
