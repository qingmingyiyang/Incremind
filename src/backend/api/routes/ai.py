from __future__ import annotations

from backend.security.device_identity import server_mode, server_authorized

import asyncio
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
import ipaddress
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend.api.ai_runtime import get_or_build_ai_runtime
from backend.api.external_agent_context_runtime import (
    authorize_external_agent_memory_proposal,
    authorize_external_agent_context_start,
    build_external_agent_context_bridge,
)
from backend.api.mcp_runtime import MCPConnectionManager
from backend.api.ai_turn_runner import AITurnRunnerCapacityError, get_or_build_ai_turn_runner
from backend.api.ai_turn_manual_recovery import (
    AIRecoveryReviewConflict,
    AIRecoveryReviewDenied,
    AIRecoveryReviewError,
    AIRecoveryReviewService,
)
from backend.api.ai_turn_recovery_worker import start_ai_turn_recovery_worker
from backend.api.container import ApiContainerDep
from backend.api.series_turn_scope_authority import build_series_turn_scope_authority
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.media_hands_runtime import current_media_hands_runtime
from backend.api.media_ingress_selection_authority import (
    MediaIngressSelectionAuthority,
    MediaIngressSelectionConflict,
    MediaIngressSelectionError,
    media_ingress_selection_public,
)
from backend.api.desktop_session import (
    DESKTOP_SESSION_HEADER,
    desktop_session,
    desktop_session_authorized,
)
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.model_routing_profile import (
    ModelRoutingProfileConflict,
    ModelRoutingProfileStore,
    ModelRoutingProfileStoreError,
)
from backend.model_runtime import ModelRuntimeError, validate_tiered_model_routing_profile
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from backend.security.project_capability_selection_command import (
    CapabilitySelectionCommandConflict,
    CapabilitySelectionCommandError,
    ProjectCapabilitySelectionCommandService,
    resolve_exclusion_target,
    resolve_selection_target,
)
from backend.security.project_capability_selection_confirmation import (
    ProjectCapabilitySelectionConfirmationConflict,
    ProjectCapabilitySelectionConfirmationError,
    ProjectCapabilitySelectionConfirmationStore,
)
from backend.security.project_boundary_summary import ProjectBoundarySummaryError, ProjectBoundarySummaryProjector
from backend.security.project_boundary_mode_command import (
    BoundaryModeCommandConflict,
    BoundaryModeCommandError,
    ProjectBoundaryModeCommandService,
)
from backend.security.project_boundary_grant_command import (
    BoundaryGrantCommandConflict,
    BoundaryGrantCommandError,
    ProjectBoundaryGrantCommandService,
    resolve_grant_target,
)
from backend.security.project_boundary_mutation_reservation import (
    ProjectBoundaryMutationReservationConflict,
)
from core.ai_kernel import (
    AIKernelContractError,
    AIKernelRuntimeError,
    ExternalAgentContextConflict,
    ExternalAgentCursorGap,
    ExternalAgentContextError,
    TurnEventConflict,
    TurnStateConflict,
    validate_execution_projection,
)
from core.ai_tooling import ProjectCapabilityCatalogError, ProjectCapabilityCatalogProjector
from core.media_hands import (
    MediaHandsPolicyAuthority,
    MediaHandsPolicyAuthorityConflict,
    MediaHandsPolicyAuthorityError,
    default_personal_workbench_policy_snapshot,
)
from core.storage_provider import SQLiteStructuredRecordStore
from core.source_processing import (
    SourceManifestArtifactError,
    SourceManifestArtifactRepository,
    SourcePermissionAuthority,
    SourcePermissionConflict,
    SourcePermissionError,
)


router = APIRouter(prefix="/api/ai", tags=["ai-kernel"])
_SELECTION_CONFIRMATION_CONTRACT_VERSION = 1


def _json_response(status_code: int, body: object) -> JSONResponse:
    return JSONResponse(content=body, status_code=status_code, headers={"Cache-Control": "no-store"})


@router.get("/projects/{project_id}/source-permissions/current")
async def get_current_source_permission(
    project_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    if not _local_governance_request(request):
        return _json_response(403, {"detail": "Source permission is local-only"})
    manifest_ref = request.query_params.get("manifest_ref")
    if not manifest_ref:
        return _json_response(400, {"detail": "Source permission manifest_ref is required"})
    try:
        store, settings = build_rebuild_object_store(getattr(container, "root_dir"))
        artifacts = SourceManifestArtifactRepository(store, namespace_id=settings.namespace_id)
        artifact = await asyncio.to_thread(
            artifacts.resolve_source_ref, source_ref=manifest_ref, project_id=project_id
        )
        evidence_ref = _permission_metadata_evidence(artifact.manifest)
        authority = SourcePermissionAuthority(store, namespace_id=settings.namespace_id)
        current = await asyncio.to_thread(
            authority.current_for_source,
            project_id=project_id,
            source_id=artifact.manifest.source_id,
            metadata_evidence_ref=evidence_ref,
        )
    except SourceManifestArtifactError:
        return _json_response(404, {"detail": "Source permission target was not found"})
    except (SourcePermissionError, ValueError, TypeError) as error:
        return _json_response(409, {"detail": "Source permission is unavailable", "reason": str(error)})
    return _json_response(200, {"permission": None if current is None else _source_permission_public(current)})


@router.post("/projects/{project_id}/source-permissions/grant")
async def grant_source_permission(
    project_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    if not _local_governance_request(request):
        return _json_response(403, {"detail": "Source permission is local-only"})
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    if set(body) != {"command_id", "manifest_ref", "expected_permission_revision", "confirm"} or body.get("confirm") is not True:
        return _json_response(400, {"detail": "Source permission grant body rejected"})
    expected = body.get("expected_permission_revision")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
        return _json_response(400, {"detail": "Source permission revision rejected"})
    try:
        store, settings = build_rebuild_object_store(getattr(container, "root_dir"))
        artifact = await asyncio.to_thread(
            SourceManifestArtifactRepository(store, namespace_id=settings.namespace_id).resolve_source_ref,
            source_ref=body.get("manifest_ref"), project_id=project_id,
        )
        if artifact.manifest.permission.decision != "unknown":
            raise SourcePermissionError("only an unresolved source manifest can receive its first grant")
        evidence_ref = _permission_metadata_evidence(artifact.manifest)
        permission = await asyncio.to_thread(
            SourcePermissionAuthority(store, namespace_id=settings.namespace_id).grant,
            project_id=project_id,
            permission_id=artifact.manifest.source_id,
            source_id=artifact.manifest.source_id,
            platform=artifact.manifest.platform,
            source_manifest_ref=artifact.public_ref,
            source_manifest_revision=artifact.revision,
            metadata_evidence_ref=evidence_ref,
            actor_id="local-user",
            command_id=body.get("command_id"),
            created_at=_utc_now(),
            expected_revision=expected,
        )
    except SourceManifestArtifactError:
        return _json_response(404, {"detail": "Source permission target was not found"})
    except SourcePermissionConflict as error:
        return _json_response(409, {"detail": "Source permission grant conflict", "reason": str(error)})
    except (SourcePermissionError, ValueError, TypeError) as error:
        return _json_response(400, {"detail": "Source permission grant rejected", "reason": str(error)})
    return _json_response(200, _source_permission_public(permission))


@router.post("/projects/{project_id}/source-permissions/{permission_id}/revoke")
async def revoke_source_permission(
    project_id: str, permission_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    if not _local_governance_request(request):
        return _json_response(403, {"detail": "Source permission is local-only"})
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    if set(body) != {"command_id", "expected_permission_revision", "confirm"} or body.get("confirm") is not True:
        return _json_response(400, {"detail": "Source permission revoke body rejected"})
    expected = body.get("expected_permission_revision")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
        return _json_response(400, {"detail": "Source permission revision rejected"})
    try:
        store, settings = build_rebuild_object_store(getattr(container, "root_dir"))
        permission = await asyncio.to_thread(
            SourcePermissionAuthority(store, namespace_id=settings.namespace_id).revoke,
            project_id=project_id,
            permission_id=permission_id,
            actor_id="local-user",
            command_id=body.get("command_id"),
            created_at=_utc_now(),
            expected_revision=expected,
        )
    except SourcePermissionConflict as error:
        return _json_response(409, {"detail": "Source permission revoke conflict", "reason": str(error)})
    except (SourcePermissionError, ValueError, TypeError) as error:
        return _json_response(400, {"detail": "Source permission revoke rejected", "reason": str(error)})
    return _json_response(200, _source_permission_public(permission))


def _permission_metadata_evidence(manifest) -> str:
    refs = manifest.permission.evidence_refs
    if len(refs) != 1 or "/source-resolution-evidence/projects/" not in refs[0]:
        raise SourcePermissionError("source manifest does not bind one metadata evidence artifact")
    return refs[0]


def _source_permission_public(permission) -> dict[str, object]:
    return {
        "permission_id": permission.permission_id,
        "permission_ref": permission.public_ref,
        "permission_revision": permission.revision,
        "project_id": permission.project_id,
        "source_manifest_ref": permission.source_manifest_ref,
        "source_manifest_revision": permission.source_manifest_revision,
        "state": permission.state,
        "scope": permission.scope,
        "revocation_generation": permission.revocation_generation,
        "created_at": permission.created_at,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _media_policy_authority(container: object) -> MediaHandsPolicyAuthority:
    root = Path(getattr(container, "root_dir"))
    return MediaHandsPolicyAuthority(
        SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3")
    )


def _media_ingress_selection_authority(
    container: object,
) -> MediaIngressSelectionAuthority:
    root = Path(getattr(container, "root_dir"))
    return MediaIngressSelectionAuthority(
        SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3")
    )


def _media_policy_public(current) -> dict[str, object]:
    if current is None:
        return {
            "scope": "personal-workbench",
            "persisted": False,
            "revision": 0,
            "policy_ref": None,
            "policy": default_personal_workbench_policy_snapshot(),
            "command_id": None,
            "actor": None,
            "created_at": None,
            "activation": "restart_required",
        }
    return {
        "scope": "personal-workbench",
        "persisted": True,
        "revision": current.revision,
        "policy_ref": current.public_ref,
        "policy": dict(current.snapshot),
        "command_id": current.command_id,
        "actor": current.actor,
        "created_at": current.created_at,
        "activation": "restart_required",
    }


@router.get("/media-hands-policy")
async def get_media_hands_policy(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    if not _local_governance_request(request):
        return _json_response(403, {"detail": "Media Hands policy is local-only"})
    try:
        current = await asyncio.to_thread(_media_policy_authority(container).current)
    except (MediaHandsPolicyAuthorityError, TypeError, ValueError) as error:
        return _json_response(
            409, {"detail": "Media Hands policy is unavailable", "reason": str(error)}
        )
    return _json_response(200, _media_policy_public(current))


@router.post("/media-hands-policy/revisions")
async def publish_media_hands_policy(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    if not _local_governance_request(request):
        return _json_response(403, {"detail": "Media Hands policy is local-only"})
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    if (
        set(body) != {"command_id", "expected_revision", "confirm", "policy"}
        or body.get("confirm") is not True
        or not isinstance(body.get("policy"), Mapping)
    ):
        return _json_response(400, {"detail": "Media Hands policy body rejected"})
    expected = body.get("expected_revision")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
        return _json_response(400, {"detail": "Media Hands policy revision rejected"})
    try:
        current = await asyncio.to_thread(
            _media_policy_authority(container).publish,
            dict(body["policy"]),
            expected_revision=expected,
            command_id=body.get("command_id"),
            actor="local-user",
            created_at=_utc_now(),
        )
    except MediaHandsPolicyAuthorityConflict as error:
        return _json_response(
            409, {"detail": "Media Hands policy conflict", "reason": str(error)}
        )
    except (MediaHandsPolicyAuthorityError, TypeError, ValueError) as error:
        return _json_response(
            400, {"detail": "Media Hands policy rejected", "reason": str(error)}
        )
    return _json_response(200, _media_policy_public(current))


@router.get("/media-ingress-selection")
async def get_media_ingress_selection(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    if not _local_governance_request(request):
        return _json_response(403, {"detail": "Media ingress selection is local-only"})
    try:
        selection = await asyncio.to_thread(
            _media_ingress_selection_authority(container).effective
        )
    except (MediaIngressSelectionError, TypeError, ValueError) as error:
        return _json_response(
            409, {"detail": "Media ingress selection is unavailable", "reason": str(error)}
        )
    return _json_response(200, {
        **media_ingress_selection_public(selection),
        "activation": "after_in_flight_requests_drain",
    })


@router.post("/media-ingress-selection/revisions")
async def publish_media_ingress_selection(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    if not _local_governance_request(request):
        return _json_response(403, {"detail": "Media ingress selection is local-only"})
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    if (
        set(body) != {"command_id", "expected_revision", "confirm", "mode"}
        or body.get("confirm") is not True
        or body.get("mode") not in {"legacy", "hands"}
    ):
        return _json_response(400, {"detail": "Media ingress selection body rejected"})
    expected = body.get("expected_revision")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
        return _json_response(400, {"detail": "Media ingress selection revision rejected"})
    if body["mode"] == "hands":
        try:
            await asyncio.to_thread(get_or_build_ai_runtime, request, container)
            runtime = current_media_hands_runtime(request.app)
        except (TypeError, ValueError, RuntimeError):
            runtime = None
        if runtime is None or not runtime.readiness().ready:
            return _json_response(
                409,
                {
                    "detail": "Media Hands is not ready for ingress cutover",
                    "reason": "media_hands_unavailable",
                },
            )
    try:
        selection = await asyncio.to_thread(
            _media_ingress_selection_authority(container).publish,
            body["mode"],
            expected_revision=expected,
            command_id=body.get("command_id"),
            actor="local-user",
            created_at=_utc_now(),
        )
    except MediaIngressSelectionConflict as error:
        return _json_response(
            409, {"detail": "Media ingress selection conflict", "reason": str(error)}
        )
    except (MediaIngressSelectionError, TypeError, ValueError) as error:
        return _json_response(
            400, {"detail": "Media ingress selection rejected", "reason": str(error)}
        )
    return _json_response(200, {
        **media_ingress_selection_public(selection),
        "activation": "after_in_flight_requests_drain",
    })


@router.get("/model-routing-profile")
async def get_model_routing_profile(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    if not _local_governance_request(request):
        return _json_response(403, {"detail": "AI model routing profile is local-only"})
    try:
        snapshot = await asyncio.to_thread(
            ModelRoutingProfileStore(getattr(container, "root_dir")).get,
        )
    except (ModelRoutingProfileStoreError, TypeError, ValueError) as error:
        return _json_response(409, {"detail": "AI model routing profile is unavailable", "reason": str(error)})
    return _json_response(200, _model_routing_profile_public(snapshot))


@router.put("/model-routing-profile")
async def update_model_routing_profile(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    if not _local_governance_request(request):
        return _json_response(403, {"detail": "AI model routing profile is local-only"})
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    if (
        set(body) != {
            "expected_revision", "rules_version", "text_default_tier",
            "tier_routes", "confirm",
        }
        or body.get("confirm") is not True
        or not isinstance(body.get("tier_routes"), Mapping)
    ):
        return _json_response(400, {"detail": "AI model routing profile body rejected"})
    try:
        store = ModelRoutingProfileStore(getattr(container, "root_dir"))
        preview = await asyncio.to_thread(
            store.preview_update,
            expected_revision=body["expected_revision"],
            rules_version=body["rules_version"],
            text_default_tier=body["text_default_tier"],
            tier_routes=dict(body["tier_routes"]),
        )
        authority_binding = await asyncio.to_thread(
            validate_tiered_model_routing_profile, container, preview,
        )
        snapshot = await asyncio.to_thread(
            store.update,
            expected_revision=body["expected_revision"],
            rules_version=body["rules_version"],
            text_default_tier=body["text_default_tier"],
            tier_routes=dict(body["tier_routes"]),
            authority_binding=authority_binding,
        )
    except ModelRoutingProfileConflict as error:
        return _json_response(409, {"detail": "AI model routing profile conflict", "reason": str(error)})
    except (ModelRoutingProfileStoreError, ModelRuntimeError, TypeError, ValueError) as error:
        return _json_response(400, {"detail": "AI model routing profile rejected", "reason": str(error)})
    return _json_response(200, _model_routing_profile_public(snapshot))


@router.post("/projects/{project_id}/capability-selection/confirmations")
async def confirm_project_capability_selection(
    project_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    """Issue a short-lived confirmation bound to one exact expansion payload."""
    if not _local_governance_request(request):
        return _json_response(403, {"detail": "AI Capability selection confirmation is local-only"})
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    expected = {
        "command_id", "action", "target_stable_id",
        "expected_boundary_revision", "expected_capability_revision",
        "expected_registry_generation",
    }
    if (
        set(body) != expected
        or body.get("action") not in {"select", "reset_exclusion"}
        or not isinstance(body.get("command_id"), str)
        or not isinstance(body.get("target_stable_id"), str)
    ):
        return _json_response(400, {"detail": "AI Capability selection confirmation rejected"})
    runtime = getattr(request.app.state, "ai_runtime", None)
    if runtime is None:
        return _json_response(503, {"detail": "AI Capability selection target is unavailable"})
    store: ProjectCapabilitySelectionConfirmationStore | None = None
    try:
        root_dir = getattr(container, "root_dir")
        registry = await asyncio.to_thread(runtime.capability_registry_snapshot)
        capability = await asyncio.to_thread(ProjectCapabilityProfileStore(root_dir).get, project_id)
        boundary = await asyncio.to_thread(ProjectBoundaryProfileStore(root_dir).get, project_id)
        if (
            capability.profile.revision != body["expected_capability_revision"]
            or boundary.profile.revision != body["expected_boundary_revision"]
        ):
            raise CapabilitySelectionCommandError("target_unavailable")
        await asyncio.to_thread(
            resolve_selection_target,
            action=body["action"], stable_id=body["target_stable_id"],
            profile=capability.profile, boundary=boundary.profile,
            capabilities=registry.definitions,
            registry_generation=registry.generation,
            expected_registry_generation=body["expected_registry_generation"],
        )
        store = ProjectCapabilitySelectionConfirmationStore(root_dir)
        token = await asyncio.to_thread(
            store.issue,
            project_id=project_id, action=body["action"],
            target_stable_id=body["target_stable_id"], command_id=body["command_id"],
            expected_boundary_revision=body["expected_boundary_revision"],
            expected_capability_revision=body["expected_capability_revision"],
            expected_registry_generation=body["expected_registry_generation"],
            contract_version=_SELECTION_CONFIRMATION_CONTRACT_VERSION,
        )
    except ProjectCapabilitySelectionConfirmationConflict as error:
        return _json_response(409, {"detail": "AI Capability selection confirmation conflict", "reason": str(error)})
    except (
        ProjectCapabilitySelectionConfirmationError, CapabilitySelectionCommandError,
        ProjectCapabilityCatalogError, AIKernelRuntimeError, TypeError, ValueError,
    ):
        return _json_response(400, {"detail": "AI Capability selection target is unavailable"})
    finally:
        if store is not None:
            store.close()
    return _json_response(200, {
        "confirmation_token": token.token, "expires_at": token.expires_at,
    })


@router.post("/projects/{project_id}/capability-selection")
async def mutate_project_capability(project_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    """Apply one contraction or explicitly confirmed single-Tool expansion."""
    if not _local_governance_request(request):
        return _json_response(403, {"detail": "AI Capability selection command is local-only"})
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    base = {
        "command_id", "action", "target_stable_id",
        "expected_boundary_revision", "expected_capability_revision",
        "expected_registry_generation",
    }
    action = body.get("action")
    expected = base | ({"confirm"} if action == "exclude" else {"confirmation_token"})
    if (
        set(body) != expected or action not in {"exclude", "select", "reset_exclusion"}
        or (action == "exclude" and body.get("confirm") is not True)
        or (action != "exclude" and not isinstance(body.get("confirmation_token"), str))
        or not isinstance(body.get("command_id"), str)
        or not isinstance(body.get("target_stable_id"), str)
    ):
        return _json_response(400, {"detail": "AI Capability selection command body rejected"})
    service: ProjectCapabilitySelectionCommandService | None = None
    try:
        root_dir = getattr(container, "root_dir")
        service = ProjectCapabilitySelectionCommandService(root_dir)
        replay = await asyncio.to_thread(
            service.replay,
            project_id=project_id, command_id=body["command_id"],
            target_stable_id=body["target_stable_id"],
            expected_boundary_revision=body["expected_boundary_revision"],
            expected_capability_revision=body["expected_capability_revision"],
            expected_registry_generation=body["expected_registry_generation"],
            action=action,
        )
        if replay is not None and replay.status in {"completed", "requires_repair"}:
            return _json_response(409 if replay.status == "requires_repair" else 200, replay.public())
        if replay is not None:
            recovered = await asyncio.to_thread(
                service.complete_if_profile_written, replay,
            )
            if recovered is not None:
                return _json_response(200, recovered.public())
        if action != "exclude" and replay is None:
            confirmation = ProjectCapabilitySelectionConfirmationStore(root_dir)
            try:
                await asyncio.to_thread(
                    confirmation.consume_exact,
                    token=body["confirmation_token"], project_id=project_id,
                    action=action, target_stable_id=body["target_stable_id"],
                    command_id=body["command_id"],
                    expected_boundary_revision=body["expected_boundary_revision"],
                    expected_capability_revision=body["expected_capability_revision"],
                    expected_registry_generation=body["expected_registry_generation"],
                    contract_version=_SELECTION_CONFIRMATION_CONTRACT_VERSION,
                )
            finally:
                confirmation.close()
        runtime = getattr(request.app.state, "ai_runtime", None)
        if runtime is None:
            return _json_response(503, {"detail": "AI Capability selection target is unavailable"})
        registry = await asyncio.to_thread(runtime.capability_registry_snapshot)
        capability = await asyncio.to_thread(ProjectCapabilityProfileStore(root_dir).get, project_id)
        boundary = await asyncio.to_thread(ProjectBoundaryProfileStore(root_dir).get, project_id)
        try:
            if action == "exclude":
                target = await asyncio.to_thread(
                    resolve_exclusion_target,
                    stable_id=body["target_stable_id"], profile=capability.profile,
                    boundary=boundary.profile, capabilities=registry.definitions,
                    registry_generation=registry.generation,
                    expected_registry_generation=body["expected_registry_generation"],
                )
            else:
                target = await asyncio.to_thread(
                    resolve_selection_target,
                    action=action, stable_id=body["target_stable_id"],
                    profile=capability.profile, boundary=boundary.profile,
                    capabilities=registry.definitions,
                    registry_generation=registry.generation,
                    expected_registry_generation=body["expected_registry_generation"],
                )
        except (CapabilitySelectionCommandError, ProjectCapabilityCatalogError, TypeError, ValueError):
            if replay is None or replay.status not in {"prepared", "capability_updated"}:
                raise
            repaired = await asyncio.to_thread(service.require_repair, replay)
            return _json_response(409, repaired.public())
        if action == "exclude":
            receipt = await asyncio.to_thread(
                service.exclude,
                project_id=project_id, command_id=body["command_id"],
                target_stable_id=target,
                expected_boundary_revision=body["expected_boundary_revision"],
                expected_capability_revision=body["expected_capability_revision"],
                expected_registry_generation=body["expected_registry_generation"],
            )
        else:
            receipt = await asyncio.to_thread(
                service.select,
                project_id=project_id, command_id=body["command_id"],
                action=action, target=target,
                expected_boundary_revision=body["expected_boundary_revision"],
                expected_capability_revision=body["expected_capability_revision"],
                expected_registry_generation=body["expected_registry_generation"],
            )
    except (
        CapabilitySelectionCommandConflict,
        ProjectBoundaryMutationReservationConflict,
        ProjectCapabilitySelectionConfirmationConflict,
    ) as error:
        return _json_response(409, {"detail": "AI Capability selection command conflict", "reason": str(error)})
    except (
        ProjectCapabilitySelectionConfirmationError,
        CapabilitySelectionCommandError, ProjectCapabilityCatalogError,
        AIKernelRuntimeError, TypeError, ValueError,
    ) as error:
        return _json_response(400, {"detail": "AI Capability selection target is unavailable", "reason": str(error)})
    finally:
        if service is not None:
            service.close()
    return _json_response(409 if receipt.status == "requires_repair" else 200, receipt.public())


@router.get("/projects/{project_id}/capability-selection-commands/{command_id}")
async def get_project_capability_selection_command(project_id: str, command_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    if not _local_request(request):
        return _json_response(403, {"detail": "AI Capability selection command is local-only"})
    service: ProjectCapabilitySelectionCommandService | None = None
    try:
        service = ProjectCapabilitySelectionCommandService(getattr(container, "root_dir"))
        receipt = await asyncio.to_thread(service.get, command_id)
    except (
        CapabilitySelectionCommandConflict,
        ProjectBoundaryMutationReservationConflict,
    ) as error:
        return _json_response(409, {"detail": "AI Capability selection command conflict", "reason": str(error)})
    except CapabilitySelectionCommandError as error:
        return _json_response(400, {"detail": "AI Capability selection command rejected", "reason": str(error)})
    finally:
        if service is not None:
            service.close()
    if receipt is None or receipt.project_id != project_id:
        return _json_response(404, {"detail": "AI Capability selection command not found"})
    return _json_response(409 if receipt.status == "requires_repair" else 200, receipt.public())


@router.post("/projects/{project_id}/boundary-grants")
async def create_project_boundary_grant(project_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    """Create one server-derived persistent Tool grant from a frozen Runtime snapshot."""
    if not _local_request(request):
        return _json_response(403, {"detail": "AI Boundary grant command is local-only"})
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    expected = {
        "command_id", "target_stable_id", "duration",
        "expected_boundary_revision", "expected_capability_revision",
        "expected_registry_generation", "confirm",
    }
    if set(body) != expected or body.get("confirm") is not True:
        return _json_response(400, {"detail": "AI Boundary grant command body rejected"})
    if not all(isinstance(body.get(key), str) for key in ("command_id", "target_stable_id", "duration")):
        return _json_response(400, {"detail": "AI Boundary grant command rejected"})
    service: ProjectBoundaryGrantCommandService | None = None
    try:
        root_dir = getattr(container, "root_dir")
        service = ProjectBoundaryGrantCommandService(root_dir)
        replay = await asyncio.to_thread(
            service.create_replay,
            project_id=project_id, command_id=body["command_id"],
            target_stable_id=body["target_stable_id"], duration=body["duration"],
            expected_boundary_revision=body["expected_boundary_revision"],
            expected_capability_revision=body["expected_capability_revision"],
        )
        if replay is not None and replay.status in {"completed", "requires_repair"}:
            return _json_response(409 if replay.status == "requires_repair" else 200, replay.public())
        runtime = getattr(request.app.state, "ai_runtime", None)
        if runtime is None:
            return _json_response(503, {"detail": "AI Boundary grant target is unavailable"})
        registry = await asyncio.to_thread(runtime.capability_registry_snapshot)
        capability = await asyncio.to_thread(ProjectCapabilityProfileStore(root_dir).get, project_id)
        boundary = await asyncio.to_thread(ProjectBoundaryProfileStore(root_dir).get, project_id)
        try:
            target = await asyncio.to_thread(
                resolve_grant_target,
                stable_id=body["target_stable_id"], profile=capability.profile,
                boundary=boundary.profile, capabilities=registry.definitions,
                registry_generation=registry.generation,
                expected_registry_generation=body["expected_registry_generation"],
                allow_pending_boundary_revision=replay is not None,
            )
        except (BoundaryGrantCommandError, ProjectCapabilityCatalogError, TypeError, ValueError):
            if replay is None or replay.status not in {"prepared", "boundary_updated"}:
                raise
            repaired = await asyncio.to_thread(service.require_repair_create, replay)
            return _json_response(409, repaired.public())
        receipt = await asyncio.to_thread(
            service.create,
            project_id=project_id, command_id=body["command_id"], target=target,
            duration=body["duration"],
            expected_boundary_revision=body["expected_boundary_revision"],
            expected_capability_revision=body["expected_capability_revision"],
        )
    except (BoundaryGrantCommandConflict, ProjectBoundaryMutationReservationConflict) as error:
        return _json_response(409, {"detail": "AI Boundary grant command conflict", "reason": str(error)})
    except (BoundaryGrantCommandError, ProjectCapabilityCatalogError, AIKernelRuntimeError, TypeError, ValueError) as error:
        return _json_response(400, {"detail": "AI Boundary grant target is unavailable", "reason": str(error)})
    finally:
        if service is not None:
            service.close()
    return _json_response(409 if receipt.status == "requires_repair" else 200, receipt.public())


@router.post("/projects/{project_id}/boundary-grants/{grant_id}/revoke")
async def revoke_project_boundary_grant(project_id: str, grant_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    """Revoke an existing grant without consulting or building AI Runtime."""
    if not _local_request(request):
        return _json_response(403, {"detail": "AI Boundary grant command is local-only"})
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    expected = {
        "command_id", "expected_boundary_revision",
        "expected_capability_revision", "expected_grant_revision", "confirm",
    }
    if set(body) != expected or body.get("confirm") is not True or not isinstance(body.get("command_id"), str):
        return _json_response(400, {"detail": "AI Boundary grant command body rejected"})
    service: ProjectBoundaryGrantCommandService | None = None
    try:
        service = ProjectBoundaryGrantCommandService(getattr(container, "root_dir"))
        receipt = await asyncio.to_thread(
            service.revoke,
            project_id=project_id, command_id=body["command_id"], grant_id=grant_id,
            expected_boundary_revision=body["expected_boundary_revision"],
            expected_capability_revision=body["expected_capability_revision"],
            expected_grant_revision=body["expected_grant_revision"],
        )
    except (BoundaryGrantCommandConflict, ProjectBoundaryMutationReservationConflict) as error:
        return _json_response(409, {"detail": "AI Boundary grant command conflict", "reason": str(error)})
    except (BoundaryGrantCommandError, TypeError, ValueError) as error:
        return _json_response(400, {"detail": "AI Boundary grant command rejected", "reason": str(error)})
    finally:
        if service is not None:
            service.close()
    return _json_response(409 if receipt.status == "requires_repair" else 200, receipt.public())


@router.get("/projects/{project_id}/boundary-grant-commands/{command_id}")
async def get_project_boundary_grant_command(project_id: str, command_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    if not _local_request(request):
        return _json_response(403, {"detail": "AI Boundary grant command is local-only"})
    service: ProjectBoundaryGrantCommandService | None = None
    try:
        service = ProjectBoundaryGrantCommandService(getattr(container, "root_dir"))
        receipt = await asyncio.to_thread(service.get, command_id)
    except (BoundaryGrantCommandConflict, ProjectBoundaryMutationReservationConflict) as error:
        return _json_response(409, {"detail": "AI Boundary grant command conflict", "reason": str(error)})
    except BoundaryGrantCommandError as error:
        return _json_response(400, {"detail": "AI Boundary grant command rejected", "reason": str(error)})
    finally:
        if service is not None:
            service.close()
    if receipt is None or receipt.project_id != project_id:
        return _json_response(404, {"detail": "AI Boundary grant command not found"})
    return _json_response(409 if receipt.status == "requires_repair" else 200, receipt.public())


@router.post("/projects/{project_id}/boundary-mode")
async def set_project_boundary_mode(project_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    """Apply the narrow, durable Boundary mode command without building AI runtime."""
    if not _local_request(request):
        return _json_response(403, {"detail": "AI Boundary mode command is local-only"})
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    if set(body) != {"command_id", "mode", "expected_boundary_revision", "expected_capability_revision", "confirm"}:
        return _json_response(400, {"detail": "AI Boundary mode command body rejected"})
    if body.get("confirm") is not True:
        return _json_response(400, {"detail": "AI Boundary mode command requires confirmation"})
    if not isinstance(body.get("command_id"), str) or not isinstance(body.get("mode"), str):
        return _json_response(400, {"detail": "AI Boundary mode command rejected"})
    service: ProjectBoundaryModeCommandService | None = None
    try:
        service = ProjectBoundaryModeCommandService(getattr(container, "root_dir"))
        receipt = await asyncio.to_thread(
            service.submit, project_id=project_id, command_id=body["command_id"], mode=body["mode"],
            expected_boundary_revision=body["expected_boundary_revision"],
            expected_capability_revision=body["expected_capability_revision"],
        )
    except (BoundaryModeCommandConflict, ProjectBoundaryMutationReservationConflict) as error:
        return _json_response(409, {"detail": "AI Boundary mode command conflict", "reason": str(error)})
    except (BoundaryModeCommandError, TypeError, ValueError) as error:
        return _json_response(400, {"detail": "AI Boundary mode command rejected", "reason": str(error)})
    finally:
        if service is not None:
            service.close()
    status = 409 if receipt.status == "requires_repair" else 200
    return _json_response(status, receipt.public())


@router.get("/projects/{project_id}/boundary-mode-commands/{command_id}")
async def get_project_boundary_mode_command(project_id: str, command_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    if not _local_request(request):
        return _json_response(403, {"detail": "AI Boundary mode command is local-only"})
    service: ProjectBoundaryModeCommandService | None = None
    try:
        service = ProjectBoundaryModeCommandService(getattr(container, "root_dir"))
        receipt = await asyncio.to_thread(service.get, command_id)
    except (BoundaryModeCommandConflict, ProjectBoundaryMutationReservationConflict) as error:
        return _json_response(409, {"detail": "AI Boundary mode command conflict", "reason": str(error)})
    except BoundaryModeCommandError as error:
        return _json_response(400, {"detail": "AI Boundary mode command rejected", "reason": str(error)})
    finally:
        if service is not None:
            service.close()
    if receipt is None or receipt.project_id != project_id:
        return _json_response(404, {"detail": "AI Boundary mode command not found"})
    return _json_response(409 if receipt.status == "requires_repair" else 200, receipt.public())


@router.get("/projects/{project_id}/capabilities")
async def get_project_capability_catalog(
    project_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    """Return a local, read-only projection of the existing AI Runtime."""
    if not _local_request(request):
        return _json_response(403, {"detail": "AI capability catalog is local-only"})
    runtime = getattr(request.app.state, "ai_runtime", None)
    if runtime is None:
        # Catalog reads must not build a second runtime, register MCP tools, or
        # create a provider connection as a side effect of a GET request.
        return _json_response(503, {"detail": "AI capability catalog is unavailable"})
    root_dir = getattr(container, "root_dir", None)
    if root_dir is None:
        return _json_response(503, {"detail": "AI capability catalog is unavailable"})
    try:
        registry = await asyncio.to_thread(runtime.capability_registry_snapshot)
        capability = await asyncio.to_thread(ProjectCapabilityProfileStore(root_dir).get, project_id)
        boundary = await asyncio.to_thread(ProjectBoundaryProfileStore(root_dir).get, project_id)
        catalog = await asyncio.to_thread(
            ProjectCapabilityCatalogProjector().project,
            profile=capability.profile,
            boundary=boundary.profile,
            capabilities=registry.definitions,
            registry_generation=registry.generation,
        )
    except (AIKernelRuntimeError, ProjectCapabilityCatalogError, TypeError, ValueError) as error:
        return _json_response(409, {"detail": "AI capability catalog is unavailable", "reason": str(error)})
    return _json_response(200, catalog.to_public_dict())


@router.get("/projects/{project_id}/boundary-summary")
async def get_project_boundary_summary(
    project_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    """Return the Boundary Center view from already-existing runtime state only."""
    if not _local_request(request):
        return _json_response(403, {"detail": "AI Boundary summary is local-only"})
    runtime = getattr(request.app.state, "ai_runtime", None)
    root_dir = getattr(container, "root_dir", None)
    if runtime is None or root_dir is None:
        return _json_response(503, {"detail": "AI Boundary summary is unavailable"})
    try:
        registry = await asyncio.to_thread(runtime.capability_registry_snapshot)
        capability = await asyncio.to_thread(ProjectCapabilityProfileStore(root_dir).get, project_id)
        boundary = await asyncio.to_thread(ProjectBoundaryProfileStore(root_dir).get, project_id)
        catalog = await asyncio.to_thread(
            ProjectCapabilityCatalogProjector().project,
            profile=capability.profile, boundary=boundary.profile,
            capabilities=registry.definitions, registry_generation=registry.generation,
        )
        summary = await asyncio.to_thread(
            ProjectBoundarySummaryProjector().project,
            boundary=boundary.profile, capability=capability.profile, catalog=catalog,
            capabilities=registry.definitions,
        )
    except (AIKernelRuntimeError, ProjectCapabilityCatalogError, ProjectBoundarySummaryError, TypeError, ValueError) as error:
        return _json_response(409, {"detail": "AI Boundary summary is unavailable", "reason": str(error)})
    return _json_response(200, summary.to_public_dict())


@router.get("/projects/{project_id}/mcp-status")
async def get_project_mcp_status(
    project_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    """Return only an already-running MCP manager's bounded local state."""
    if not _local_request(request):
        return _json_response(403, {"detail": "AI MCP status is local-only"})
    runtime = getattr(request.app.state, "ai_runtime", None)
    manager = getattr(request.app.state, "ai_mcp_connection_manager", None)
    root_dir = getattr(container, "root_dir", None)
    if runtime is None or not isinstance(manager, MCPConnectionManager) or root_dir is None:
        return _json_response(503, {"detail": "AI MCP status is unavailable"})
    try:
        capability = await asyncio.to_thread(
            ProjectCapabilityProfileStore(root_dir).get, project_id,
        )
        boundary = await asyncio.to_thread(
            ProjectBoundaryProfileStore(root_dir).get, project_id,
        )
        registry = await asyncio.to_thread(runtime.capability_registry_snapshot)
        catalog = await asyncio.to_thread(
            ProjectCapabilityCatalogProjector().project,
            profile=capability.profile, boundary=boundary.profile,
            capabilities=registry.definitions, registry_generation=registry.generation,
        )
        status = await asyncio.to_thread(
            manager.bounded_audit_status,
            enabled_server_ids=(
                capability.profile.enabled_mcp_server_ids
                if "mcp" in capability.profile.enabled_sources
                else ()
            ),
        )
    except (AIKernelRuntimeError, ProjectCapabilityCatalogError, TypeError, ValueError):
        return _json_response(409, {"detail": "AI MCP status is unavailable"})
    return _json_response(200, {
        "project_id": project_id,
        "capability_profile": {
            "profile_id": capability.profile.profile_id,
            "revision": capability.profile.revision,
        },
        "servers": status["servers"],
        "global": status["global"],
        "anonymous_reason_counts": status["anonymous_reason_counts"],
        # Existing catalog exclusions remain anonymous and are intentionally
        # not joined to an MCP server or Tool identity in this projection.
        "excluded_reason_counts": [
            {"reason": reason, "count": min(1024, max(0, count))}
            for reason, count in catalog.excluded_reason_counts
            if isinstance(count, int) and not isinstance(count, bool) and count > 0
        ],
    })


@router.post("/external-agents/context-sessions")
async def start_external_agent_context_session(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    if not _external_agent_context_request(request):
        return _json_response(403, {"detail": "External Agent context is local-only"})
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    fields = {
        "operation_id", "adapter_id", "adapter_revision", "template_revision", "turn_id",
        "project_id", "purpose", "requested_context_bytes", "confirm",
    }
    if set(body) != fields or body.get("confirm") is not True:
        return _json_response(400, {"detail": "External Agent context session body rejected"})
    try:
        bridge = _external_agent_context_bridge(container)
        values = {key: value for key, value in body.items() if key != "confirm"}
        admission = await asyncio.to_thread(
            authorize_external_agent_context_start,
            Path(getattr(container, "root_dir")),
            operation_id=str(body["operation_id"]),
            turn_id=str(body["turn_id"]),
            project_id=str(body["project_id"]),
            adapter_id=str(body["adapter_id"]),
        )
        result = await asyncio.to_thread(bridge.start_session, **values, admission=admission)
    except ExternalAgentContextConflict as error:
        return _json_response(409, {"detail": "External Agent context conflict", "reason": str(error)})
    except ExternalAgentContextError as error:
        return _external_agent_context_error(error)
    except (OSError, RuntimeError):
        return _json_response(503, {"detail": "External Agent context is unavailable"})
    except (TypeError, ValueError):
        return _json_response(400, {"detail": "External Agent context request rejected"})
    return _json_response(201, result)


@router.post("/external-agents/context-sessions/{session_id}/resolve")
async def resolve_external_agent_context(
    session_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    if not _external_agent_context_request(request):
        return _json_response(403, {"detail": "External Agent context is local-only"})
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    fields = {
        "operation_id", "context_refs", "expected_context_manifest_revision", "purpose",
    }
    if set(body) != fields or not isinstance(body.get("context_refs"), list):
        return _json_response(400, {"detail": "External Agent context resolve body rejected"})
    try:
        bridge = _external_agent_context_bridge(container)
        result = await asyncio.to_thread(bridge.resolve_context, session_id=session_id, **body)
    except ExternalAgentContextConflict as error:
        return _json_response(409, {"detail": "External Agent context conflict", "reason": str(error)})
    except ExternalAgentContextError as error:
        return _external_agent_context_error(error)
    except (OSError, RuntimeError):
        return _json_response(503, {"detail": "External Agent context is unavailable"})
    except (TypeError, ValueError):
        return _json_response(400, {"detail": "External Agent context request rejected"})
    return _json_response(200, result)


@router.get("/external-agents/context-sessions/{session_id}/changes")
async def get_external_agent_context_changes(
    session_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    if not _external_agent_context_request(request):
        return _json_response(403, {"detail": "External Agent context is local-only"})
    params = request.query_params
    if set(params) - {"operation_id", "after_cursor", "purpose", "limit"}:
        return _json_response(400, {"detail": "External Agent change query rejected"})
    try:
        operation_id = params["operation_id"]
        purpose = params["purpose"]
        after_cursor = int(params["after_cursor"])
        limit = int(params.get("limit", "128"))
    except (KeyError, TypeError, ValueError):
        return _json_response(400, {"detail": "External Agent change query rejected"})
    try:
        bridge = _external_agent_context_bridge(container)
        result = await asyncio.to_thread(
            bridge.get_changes,
            operation_id=operation_id,
            session_id=session_id,
            after_cursor=after_cursor,
            purpose=purpose,
            limit=limit,
        )
    except ExternalAgentCursorGap as error:
        return _json_response(409, {
            "detail": "External Agent context cursor retention gap",
            "reason": str(error),
            **error.public_payload(),
        })
    except ExternalAgentContextConflict as error:
        return _json_response(409, {"detail": "External Agent context conflict", "reason": str(error)})
    except ExternalAgentContextError as error:
        return _external_agent_context_error(error)
    except (OSError, RuntimeError):
        return _json_response(503, {"detail": "External Agent context is unavailable"})
    except (TypeError, ValueError):
        return _json_response(400, {"detail": "External Agent context request rejected"})
    return _json_response(200, result)


@router.post("/external-agents/context-sessions/{session_id}/acknowledgements")
async def acknowledge_external_agent_context_changes(
    session_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    if not _external_agent_context_request(request):
        return _json_response(403, {"detail": "External Agent context is local-only"})
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    if set(body) != {"operation_id", "acknowledged_cursor", "purpose"}:
        return _json_response(400, {"detail": "External Agent acknowledgement body rejected"})
    try:
        bridge = _external_agent_context_bridge(container)
        result = await asyncio.to_thread(
            bridge.acknowledge_changes, session_id=session_id, **body,
        )
    except ExternalAgentContextConflict as error:
        return _json_response(409, {"detail": "External Agent context conflict", "reason": str(error)})
    except ExternalAgentContextError as error:
        return _external_agent_context_error(error)
    except (OSError, RuntimeError):
        return _json_response(503, {"detail": "External Agent context is unavailable"})
    except (TypeError, ValueError):
        return _json_response(400, {"detail": "External Agent context request rejected"})
    return _json_response(200, result)


@router.post("/external-agents/context-sessions/{session_id}/memory-proposals")
async def submit_external_agent_memory_proposal(
    session_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    if not _external_agent_context_request(request):
        return _json_response(403, {"detail": "External Agent context is local-only"})
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    fields = {
        "operation_id", "expected_context_manifest_revision", "purpose", "proposal", "confirm",
    }
    if set(body) != fields or body.get("confirm") is not True or not isinstance(body.get("proposal"), Mapping):
        return _json_response(400, {"detail": "External Agent memory proposal body rejected"})
    try:
        bridge = _external_agent_context_bridge(container)
        subject = await asyncio.to_thread(
            bridge.proposal_admission_subject,
            session_id=session_id, purpose=str(body["purpose"]),
        )
        admission = await asyncio.to_thread(
            authorize_external_agent_memory_proposal,
            Path(getattr(container, "root_dir")),
            operation_id=str(body["operation_id"]),
            turn_id=subject["turn_id"], project_id=subject["project_id"],
            adapter_id=subject["adapter_id"],
        )
        result = await asyncio.to_thread(
            bridge.submit_memory_proposal,
            operation_id=str(body["operation_id"]), session_id=session_id,
            expected_context_manifest_revision=str(body["expected_context_manifest_revision"]),
            purpose=str(body["purpose"]), proposal=body["proposal"], admission=admission,
        )
    except ExternalAgentContextConflict as error:
        return _json_response(409, {"detail": "External Agent context conflict", "reason": str(error)})
    except ExternalAgentContextError as error:
        return _external_agent_context_error(error)
    except (OSError, RuntimeError):
        return _json_response(503, {"detail": "External Agent context is unavailable"})
    except (TypeError, ValueError):
        return _json_response(400, {"detail": "External Agent memory proposal rejected"})
    return _json_response(201, result)


@router.post("/turns")
async def submit_ai_turn(request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    try:
        scope = body.get("scope")
        if isinstance(scope, Mapping) and scope.get("kind") == "series":
            authority = await asyncio.to_thread(
                build_series_turn_scope_authority, getattr(container, "root_dir"),
            )
            body = await asyncio.to_thread(authority.canonicalize, body)
        runtime = get_or_build_ai_runtime(request, container)
        receipt = await asyncio.to_thread(get_or_build_ai_turn_runner(request, runtime).accept_and_submit, body)
    except AITurnRunnerCapacityError:
        return _json_response(503, {"detail": "AI turn runner unavailable", "error_code": "ai.runner_capacity"})
    except (AIKernelContractError, AIKernelRuntimeError, KeyError, TypeError, ValueError) as error:
        return _json_response(400, {"detail": "AI turn rejected", "reason": str(error)})
    return _json_response(202, asdict(receipt))


@router.get("/turns/{turn_id}/stream", response_model=None)
async def stream_ai_turn(turn_id: str, request: Request, container: ApiContainerDep):
    try:
        after = _stream_cursor(request)
    except ValueError as error:
        return _json_response(400, {"detail": "AI stream cursor rejected", "reason": str(error)})
    view = request.query_params.get("view", "simple")
    if view not in {"simple", "developer"}:
        return _json_response(400, {"detail": "AI stream view rejected"})
    runtime = get_or_build_ai_runtime(request, container)
    events = tuple(await asyncio.to_thread(runtime.events_after, turn_id))
    if not events:
        return _json_response(404, {"detail": "AI turn not found"})
    current = int(events[-1]["sequence"])
    if after > current:
        return _json_response(409, {"detail": "AI stream cursor unavailable", "error_code": "ai.cursor_ahead"})
    try:
        projection = await asyncio.to_thread(runtime.execution_projection_for, turn_id, view)
        projection = validate_execution_projection(projection)
    except (AIKernelContractError, KeyError, TypeError, ValueError):
        return _json_response(409, {
            "detail": "AI execution projection unavailable",
            "error_code": "ai.projection_integrity",
        })
    return StreamingResponse(
        _stream_ai_projections(request, runtime, turn_id=turn_id, view=view, after=after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store, no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/turns/{turn_id}/events")
def get_ai_turn_events(turn_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        after = int(request.query_params.get("after", "0"))
        if after < 0:
            raise ValueError("after must be non-negative")
        view = request.query_params.get("view")
        if view not in {None, "simple", "developer"}:
            raise ValueError("view must be simple or developer")
    except ValueError as error:
        return _json_response(400, {"detail": "AI event cursor rejected", "reason": str(error)})
    runtime = get_or_build_ai_runtime(request, container)
    events = list(runtime.events_after(turn_id, after))
    if not events and after == 0:
        return _json_response(404, {"detail": "AI turn not found"})
    if view is not None:
        if after > 0 and not tuple(runtime.events_after(turn_id)):
            return _json_response(404, {"detail": "AI turn not found"})
        try:
            projection = runtime.execution_projection_for(turn_id, view)
        except AIKernelContractError:
            return _json_response(409, {
                "detail": "AI execution projection unavailable",
                "error_code": "ai.projection_integrity",
            })
        return _json_response(200, {
            "turn_id": turn_id,
            "after": after,
            "projection": projection,
            "presentation": runtime.presentation_for(turn_id),
        })
    return _json_response(200, {
        "turn_id": turn_id,
        "after": after,
        "events": events,
        "presentation": runtime.presentation_for(turn_id),
    })


@router.post("/turns/{turn_id}/actions")
async def apply_ai_turn_action(turn_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    if body.get("turn_id") != turn_id:
        return _json_response(400, {"detail": "AI action turn identity mismatch"})
    try:
        runtime = get_or_build_ai_runtime(request, container)
        receipt = await asyncio.to_thread(
            get_or_build_ai_turn_runner(request, runtime).accept_action_and_submit,
            body,
        )
    except (TurnEventConflict, TurnStateConflict) as error:
        return _json_response(409, {"detail": "AI action conflict", "reason": str(error)})
    except (AIKernelContractError, AIKernelRuntimeError, KeyError, TypeError, ValueError) as error:
        status = 409 if "sequence conflict" in str(error) else 400
        return _json_response(status, {"detail": "AI action rejected", "reason": str(error)})
    return _json_response(202, asdict(receipt))


@router.get("/recovery-reviews")
async def list_ai_recovery_reviews(request: Request, container: ApiContainerDep) -> JSONResponse:
    if not _local_request(request):
        return _json_response(403, {"detail": "AI recovery review is local-only"})
    project_id = request.query_params.get("project_id")
    try:
        limit = int(request.query_params.get("limit", "50"))
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        service = AIRecoveryReviewService(getattr(container, "root_dir"))
        items = await asyncio.to_thread(service.list, project_id=project_id, limit=limit)
    except (TypeError, ValueError) as error:
        return _json_response(400, {"detail": "AI recovery review query rejected", "reason": str(error)})
    return _json_response(200, {"items": items})


@router.get("/recovery-reviews/{review_id}")
async def get_ai_recovery_review(review_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    if not _local_request(request):
        return _json_response(403, {"detail": "AI recovery review is local-only"})
    try:
        item = await asyncio.to_thread(AIRecoveryReviewService(getattr(container, "root_dir")).get, review_id)
    except (TypeError, ValueError) as error:
        return _json_response(400, {"detail": "AI recovery review rejected", "reason": str(error)})
    if item is None:
        return _json_response(404, {"detail": "AI recovery review not found"})
    return _json_response(200, item)


@router.post("/recovery-reviews/{review_id}/confirm-no-effect")
async def confirm_ai_recovery_no_effect(review_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    return await _apply_ai_recovery_review_action(
        review_id, request, container, action="confirm_no_effect_and_resume",
    )


@router.post("/recovery-reviews/{review_id}/keep-quarantined")
async def keep_ai_recovery_quarantined(review_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    return await _apply_ai_recovery_review_action(
        review_id, request, container, action="keep_quarantined",
    )


async def _apply_ai_recovery_review_action(
    review_id: str,
    request: Request,
    container: object,
    *,
    action: str,
) -> JSONResponse:
    if not _local_request(request):
        return _json_response(403, {"detail": "AI recovery review is local-only"})
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    if set(body) != {"expected_revision"}:
        return _json_response(400, {"detail": "AI recovery review action body rejected"})
    expected_revision = body.get("expected_revision")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
        return _json_response(400, {"detail": "AI recovery review revision rejected"})
    service = AIRecoveryReviewService(getattr(container, "root_dir"))
    try:
        resolved = await asyncio.to_thread(
            service.decide,
            review_id,
            expected_revision=expected_revision,
            action=action,
        )
    except AIRecoveryReviewDenied as error:
        return _json_response(403, {"detail": "AI recovery review denied", "reason": str(error)})
    except AIRecoveryReviewConflict as error:
        return _json_response(409, {"detail": "AI recovery review conflict", "reason": str(error)})
    except (AIRecoveryReviewError, ValueError) as error:
        return _json_response(400, {"detail": "AI recovery review rejected", "reason": str(error)})
    if action == "confirm_no_effect_and_resume":
        try:
            runtime = get_or_build_ai_runtime(request, container)
            start_ai_turn_recovery_worker(request.app, service.store, runtime)
            resolved["wake_status"] = "scheduled"
        except Exception:
            # The queue transition is already durable.  Startup recovery will
            # discover it even when this best-effort wake cannot be scheduled.
            resolved["wake_status"] = "pending_startup"
    return _json_response(202 if action == "confirm_no_effect_and_resume" else 200, resolved)


async def _json_body(request: Request) -> dict[str, Any] | JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return _json_response(400, {"detail": "request body must be JSON"})
    if not isinstance(body, Mapping):
        return _json_response(400, {"detail": "request body must be an object"})
    return dict(body)


def _local_request(request: Request) -> bool:
    if server_mode(request):
        return server_authorized(request)
    host = request.client.host if request.client is not None else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _local_governance_request(request: Request) -> bool:
    if server_mode(request):
        return server_authorized(request)
    if not _local_request(request):
        return False
    try:
        session = desktop_session()
    except RuntimeError:
        return False
    return session is None or desktop_session_authorized(
        request.headers.get(DESKTOP_SESSION_HEADER)
    )


def _external_agent_context_request(request: Request) -> bool:
    if server_mode(request):
        return server_authorized(request)
    if not _local_request(request):
        return False
    try:
        session = desktop_session()
    except RuntimeError:
        return False
    return session is not None and desktop_session_authorized(
        request.headers.get(DESKTOP_SESSION_HEADER)
    )


def _external_agent_context_error(error: ExternalAgentContextError) -> JSONResponse:
    reason = str(error)
    status = 404 if "not found" in reason else 400
    return _json_response(
        status,
        {"detail": "External Agent context request rejected", "reason": reason},
    )


def _external_agent_context_bridge(container: object):
    root_dir = getattr(container, "root_dir", None)
    if root_dir is None:
        raise RuntimeError("External Agent context root is unavailable")
    return build_external_agent_context_bridge(Path(root_dir))


def _model_routing_profile_public(snapshot) -> dict[str, object]:
    profile = snapshot.profile
    return {
        "schema_version": "1.0.0",
        "persisted": snapshot.persisted,
        "revision": profile.revision,
        "rules_version": profile.rules_version,
        "text_default_tier": profile.text_default_tier,
        "tier_routes": dict(profile.tier_routes),
        "authority_binding": (
            None if profile.authority_binding is None else {
                "registry_revision": profile.authority_binding.registry_revision,
                "runtime_revision": profile.authority_binding.runtime_revision,
                "activation_fingerprint": profile.authority_binding.activation_fingerprint,
            }
        ),
        "image_generation_available": bool(
            profile.route_key_for("image_generation")
            and profile.authority_binding is not None
        ),
    }


def _stream_cursor(request: Request) -> int:
    # Last-Event-ID is the EventSource resume authority and deliberately wins
    # over a stale query parameter.
    value = request.headers.get("last-event-id")
    if value is None:
        value = request.query_params.get("after", "0")
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ValueError("cursor must be a non-negative integer")
    cursor = int(value)
    if cursor < 0:
        raise ValueError("cursor must be a non-negative integer")
    return cursor


async def _stream_ai_projections(request: Request, runtime: object, *, turn_id: str, view: str, after: int):
    cursor = after
    while True:
        if await request.is_disconnected():
            return
        events = tuple(await asyncio.to_thread(runtime.events_after, turn_id))
        if not events:
            return
        current = int(events[-1]["sequence"])
        if current > cursor:
            try:
                projection = validate_execution_projection(
                    await asyncio.to_thread(runtime.execution_projection_for, turn_id, view)
                )
            except (AIKernelContractError, KeyError, TypeError, ValueError):
                # A corrupt public view is never substituted with raw durable
                # events or payload data.
                yield "event: stream_error\ndata: {\"error_code\":\"ai.projection_integrity\"}\n\n"
                return
            sequence = int(projection["current_sequence"])
            if sequence < current or sequence <= cursor:
                yield "event: stream_error\ndata: {\"error_code\":\"ai.projection_integrity\"}\n\n"
                return
            import json
            payload: dict[str, object] = {"turn_id": turn_id, "cursor": sequence, "projection": projection}
            yield f"id: {sequence}\nevent: projection\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
            cursor = sequence
            if projection["terminal"] is True:
                return
        elif events[-1].get("type") in {"turn.completed", "turn.failed", "turn.cancelled"}:
            return
        await asyncio.sleep(0.1)
