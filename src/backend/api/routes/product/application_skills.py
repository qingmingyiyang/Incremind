"""Application skills ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.plugin_runtime import build_plugin_skill_activation
from backend.security.project_capability_profiles import (
    ProjectCapabilityProfileConflict,
    ProjectCapabilityProfileStore,
    ProjectCapabilityProfileStoreError,
)

from core.aggregate_repository_factory import AggregateRepositoryFactoryError
from core.application_skill import (
    ApplicationSkillBindingConflict,
    ApplicationSkillBindingError,
    ApplicationSkillBindingRegistry,
    ApplicationSkillCatalog,
    ApplicationSkillError,
    ApplicationSkillImportService,
    ApplicationSkillManagementConflict,
    ApplicationSkillManagementError,
    ApplicationSkillManagementService,
    ApplicationSkillProposalRegistry,
    ApplicationSkillResolutionError,
    ApplicationSkillResolver,
    ApplicationSkillSource,
)
from core.project_skill_core import ProjectSkillRepositoryError
from core.storage_provider import JsonObjectStore, RebuildStorageSettings

from . import http as product_http
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


def _application_skill_management(
    container: ApiContainerDep,
    store: JsonObjectStore,
) -> ApplicationSkillManagementService:
    catalog = ApplicationSkillCatalog()
    sources = (
        ApplicationSkillSource(
            "bundled",
            product_repositories.REPOSITORY_ROOT / "config" / "application-skills",
            "bundled",
        ),
        ApplicationSkillSource("user", container.root_dir / "skills", "user"),
        *build_plugin_skill_activation(container.root_dir).all_active_sources(),
    )
    bindings = ApplicationSkillBindingRegistry(store)
    return ApplicationSkillManagementService(
        catalog=catalog,
        sources=sources,
        bindings=bindings,
        resolver=ApplicationSkillResolver(bindings),
        store=store,
    )


def _application_skill_project_summary(
    container: ApiContainerDep,
    store: JsonObjectStore,
    settings: RebuildStorageSettings,
    project_id: str,
) -> str:
    skill = product_repositories._project_skill_repository(container.root_dir, store, settings).load(project_id)
    if not isinstance(skill, Mapping):
        return ""
    parts = [str(skill.get("purpose") or "")]
    for rule in skill.get("output_rules", []):
        if isinstance(rule, Mapping) and isinstance(rule.get("rule"), str):
            parts.append(str(rule["rule"]))
    return "\n".join(part for part in parts if part)[:8000]


def _application_skill_error(error: Exception) -> JSONResponse:
    conflict = isinstance(
        error,
        (
            AggregateRepositoryFactoryError,
            ApplicationSkillBindingConflict,
            ApplicationSkillManagementConflict,
            ProjectSkillRepositoryError,
        ),
    )
    return product_http._json_response(
        409 if conflict else 400,
        {"detail": str(error), "actionable": True},
        product_http._no_store_headers(),
    )


@router.get("/api/rebuild/developer-studio/application-skills")
async def developer_studio_application_skill_status(
    container: ApiContainerDep,
) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    try:
        payload = _application_skill_management(container, store).status()
    except (ApplicationSkillError, ApplicationSkillBindingError) as error:
        return _application_skill_error(error)
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.post("/api/rebuild/developer-studio/application-skills/imports/preview")
async def developer_studio_application_skill_import_preview(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    body = await product_http._json_body(request) or {}
    store, _settings = product_repositories._object_store(container.root_dir)
    try:
        payload = ApplicationSkillImportService(
            ApplicationSkillCatalog(),
            container.root_dir / "skills",
        ).preview(str(body.get("source_path") or ""))
        proposal_payload = {
            "source_path": str(body.get("source_path") or ""),
            "expected_fingerprint": str(payload["package"]["fingerprint"]),
            "preview_token": str(payload["preview_token"]),
        }
        proposal = ApplicationSkillProposalRegistry(store).propose(
            "package.import", proposal_payload
        )
        payload = {
            **payload,
            "proposal_id": proposal["proposal_id"],
            "proposal_status": proposal["status"],
        }
    except (ApplicationSkillManagementError, ApplicationSkillError) as error:
        return _application_skill_error(error)
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.post("/api/rebuild/developer-studio/application-skills/imports/confirm")
async def developer_studio_application_skill_import_confirm(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    body = await product_http._json_body(request) or {}
    store, _settings = product_repositories._object_store(container.root_dir)
    try:
        proposal_payload = {
            "source_path": str(body.get("source_path") or ""),
            "expected_fingerprint": str(body.get("expected_fingerprint") or ""),
            "preview_token": str(body.get("preview_token") or ""),
        }
        proposals = ApplicationSkillProposalRegistry(store)
        approved = proposals.require_approved(
            str(body.get("proposal_id") or ""),
            action="package.import",
            expected_payload=proposal_payload,
            confirm=body.get("confirm") is True,
            reason=str(body.get("reason") or ""),
        )
        if approved.get("status") == "applied":
            payload = {
                "status": "already_imported",
                "replayed": True,
                "proposal_id": approved["proposal_id"],
                "proposal_status": "applied",
            }
        else:
            payload = ApplicationSkillImportService(
            ApplicationSkillCatalog(),
            container.root_dir / "skills",
            ).confirm(
                proposal_payload["source_path"],
                expected_fingerprint=proposal_payload["expected_fingerprint"],
                preview_token=proposal_payload["preview_token"],
                confirm=True,
                reason=str(body.get("reason") or ""),
            )
            proposals.mark_applied(str(approved["proposal_id"]))
            payload = {
                **payload,
                "proposal_id": approved["proposal_id"],
                "proposal_status": "applied",
            }
    except (ApplicationSkillManagementError, ApplicationSkillError) as error:
        return _application_skill_error(error)
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.post("/api/rebuild/developer-studio/application-skills/bindings/preview")
async def developer_studio_application_skill_binding_preview(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    body = await product_http._json_body(request) or {}
    store, _settings = product_repositories._object_store(container.root_dir)
    try:
        payload = _application_skill_management(container, store).preview_binding(
            skill_id=str(body.get("skill_id") or ""),
            project_id=str(body.get("project_id") or ""),
            allowed_consumers=product_http._optional_body_str_list(body, "allowed_consumers"),
            priority=product_http._required_body_int(body, "priority"),
            trigger_terms=product_http._optional_body_str_list(body, "trigger_terms"),
        )
    except (
        ApplicationSkillManagementError,
        ApplicationSkillBindingError,
        ApplicationSkillResolutionError,
        ValueError,
    ) as error:
        return _application_skill_error(error)
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.post("/api/rebuild/developer-studio/application-skills/bindings/activate")
async def developer_studio_application_skill_binding_activate(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    body = await product_http._json_body(request) or {}
    store, _settings = product_repositories._object_store(container.root_dir)
    try:
        payload = _application_skill_management(container, store).activate_binding(**body)
    except (ApplicationSkillManagementError, ApplicationSkillBindingError, ValueError) as error:
        return _application_skill_error(error)
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.post("/api/rebuild/developer-studio/application-skills/bindings/deactivate")
async def developer_studio_application_skill_binding_deactivate(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    body = await product_http._json_body(request) or {}
    store, _settings = product_repositories._object_store(container.root_dir)
    try:
        payload = _application_skill_management(container, store).deactivate_binding(**body)
    except (ApplicationSkillManagementError, ApplicationSkillBindingError, ValueError) as error:
        return _application_skill_error(error)
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.post("/api/rebuild/developer-studio/application-skills/bindings/deactivate-preview")
async def developer_studio_application_skill_binding_deactivate_preview(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    body = await product_http._json_body(request) or {}
    store, _settings = product_repositories._object_store(container.root_dir)
    try:
        payload = _application_skill_management(container, store).preview_deactivation(
            project_id=str(body.get("project_id") or ""),
            skill_id=str(body.get("skill_id") or ""),
            expected_registry_revision=product_http._required_body_int(
                body, "expected_registry_revision"
            ),
        )
    except (ApplicationSkillManagementError, ApplicationSkillBindingError, ValueError) as error:
        return _application_skill_error(error)
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.post("/api/rebuild/developer-studio/application-skills/plugins/{plugin_id}/projects/{project_id}/enable")
async def developer_studio_plugin_skill_project_enable(
    plugin_id: str,
    project_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    body = await product_http._json_body(request) or {}
    if set(body) != {"skill_ids", "expected_profile_revision", "confirm"} or body.get("confirm") is not True:
        return product_http._json_response(400, {"detail": "exact confirmed Plugin Skill project fields are required"}, product_http._no_store_headers())
    raw_skill_ids = body.get("skill_ids")
    if (
        not isinstance(raw_skill_ids, list)
        or not raw_skill_ids
        or any(
            not isinstance(item, str)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", item)
            for item in raw_skill_ids
        )
        or len(set(raw_skill_ids)) != len(raw_skill_ids)
    ):
        return product_http._json_response(400, {"detail": "at least one Plugin Skill is required"}, product_http._no_store_headers())
    skill_ids = tuple(raw_skill_ids)
    store, _settings = product_repositories._object_store(container.root_dir)
    activation = build_plugin_skill_activation(container.root_dir)
    try:
        active_catalog = ApplicationSkillCatalog().discover_selected(
            activation.active_sources((plugin_id,)), skill_ids,
        )
        if {item.skill_id for item in active_catalog.packages} != set(skill_ids):
            raise ApplicationSkillManagementError("selected Plugin Skill is not active")
        status = _application_skill_management(container, store).status()
        bindings = status.get("registry", {}).get("bindings", [])
        active_bound = {
            item.get("skill_id") for item in bindings
            if isinstance(item, Mapping)
            and item.get("project_id") == project_id
            and item.get("effective_status") == "active"
            and "turn.workbench-question" in (item.get("allowed_consumers") or [])
        }
        if not set(skill_ids).issubset(active_bound):
            raise ApplicationSkillManagementError("Plugin Skill requires an active project Turn binding")
        profiles = ProjectCapabilityProfileStore(container.root_dir)
        snapshot = profiles.get(project_id)
        profile = snapshot.profile
        expected = product_http._required_body_int(body, "expected_profile_revision")
        if expected != snapshot.store_revision:
            raise ProjectCapabilityProfileConflict("project capability profile revision conflict")
        updated = profiles.update(
            project_id,
            expected_revision=expected,
            boundary_profile_id=profile.boundary_profile_id,
            boundary_profile_revision=profile.boundary_profile_revision,
            enabled_sources=tuple(dict.fromkeys((*profile.enabled_sources, "plugin"))),
            enabled_skill_ids=tuple(dict.fromkeys((*profile.enabled_skill_ids, *skill_ids))),
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
    except (
        ApplicationSkillError,
        ApplicationSkillManagementError,
        ProjectCapabilityProfileConflict,
        ProjectCapabilityProfileStoreError,
        ValueError,
    ) as error:
        return _application_skill_error(error)
    return product_http._json_response(200, {"profile": asdict(updated.profile), "profile_revision": updated.store_revision}, product_http._no_store_headers())


@router.post("/api/rebuild/developer-studio/application-skills/resolver-preview")
async def developer_studio_application_skill_resolver_preview(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    body = await product_http._json_body(request) or {}
    store, settings = product_repositories._object_store(container.root_dir)
    project_id = str(body.get("project_id") or "")
    try:
        payload = _application_skill_management(container, store).preview_resolution(
            project_id=project_id,
            consumer=str(body.get("consumer") or ""),
            task_kind=str(body.get("task_kind") or ""),
            task_text=str(body.get("task_text") or ""),
            project_summary=_application_skill_project_summary(
                container,
                store,
                settings,
                project_id,
            ),
        )
    except (
        ApplicationSkillManagementError,
        ApplicationSkillBindingError,
        ApplicationSkillResolutionError,
        ProjectSkillRepositoryError,
        AggregateRepositoryFactoryError,
        ValueError,
    ) as error:
        return _application_skill_error(error)
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.get("/api/rebuild/developer-studio/application-skills/invocations")
async def developer_studio_application_skill_invocations(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    try:
        payload = _application_skill_management(container, store).invocations(
            str(request.query_params.get("project_id") or "")
        )
    except ApplicationSkillManagementError as error:
        return _application_skill_error(error)
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.get("/api/rebuild/projects/{project_id}/application-skills")
async def project_application_skill_summary(
    project_id: str,
    container: ApiContainerDep,
) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    try:
        payload = _application_skill_management(container, store).project_summary(project_id)
    except (ApplicationSkillManagementError, ApplicationSkillBindingError) as error:
        return _application_skill_error(error)
    return product_http._json_response(200, payload, product_http._no_store_headers())
