"""Model routes ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.model_route_context import list_model_route_provider_contexts

from core.product_core.developer_studio_config import GetDeveloperStudioConfig
from core.product_core.model_route_migration import (
    ModelRouteMigrationConflict,
    ModelRouteMigrationError,
    ModelRouteMigrationService,
)
from core.product_core.model_route_registry import ModelRouteRegistry
from core.product_core.model_route_runtime import (
    ModelRouteRuntimeConflict,
    ModelRouteRuntimeError,
    ModelRouteRuntimeService,
)

from . import http as product_http
from . import providers as product_providers
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.post("/api/rebuild/model-routes/migration/preview")
async def model_route_migration_preview(request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await product_http._json_body(request)
    renderer_task_map = body.get("renderer_task_map", {})
    if not isinstance(renderer_task_map, Mapping):
        return product_http._json_response(400, {"detail": "renderer_task_map must be an object"}, product_http._no_store_headers())
    store, _settings = product_repositories._object_store(container.root_dir)
    config = GetDeveloperStudioConfig(store).execute()
    try:
        result = ModelRouteMigrationService(ModelRouteRegistry(container.root_dir)).preview(
            renderer_task_map=renderer_task_map,
            developer_revision=config.revision,
            developer_task_map=config.task_model_map,
            developer_model_profiles=config.model_profiles,
            providers=list_model_route_provider_contexts(container),
        )
    except (ModelRouteMigrationError, ValueError) as error:
        return product_http._json_response(409, {"detail": str(error)}, product_http._no_store_headers())
    return product_http._json_response(200, result, product_http._no_store_headers())


@router.post("/api/rebuild/model-routes/migration/confirm")
async def model_route_migration_confirm(request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await product_http._json_body(request)
    renderer_task_map = body.get("renderer_task_map", {})
    choices = body.get("choices", {})
    if not isinstance(renderer_task_map, Mapping) or not isinstance(choices, Mapping):
        return product_http._json_response(400, {"detail": "renderer_task_map and choices must be objects"}, product_http._no_store_headers())
    store, _settings = product_repositories._object_store(container.root_dir)
    config = GetDeveloperStudioConfig(store).execute()
    try:
        result = ModelRouteMigrationService(ModelRouteRegistry(container.root_dir)).confirm(
            preview_token=str(body.get("preview_token") or ""),
            confirm=body.get("confirm") is True,
            choices=choices,
            renderer_task_map=renderer_task_map,
            developer_revision=config.revision,
            developer_task_map=config.task_model_map,
            developer_model_profiles=config.model_profiles,
            providers=list_model_route_provider_contexts(container),
        )
    except ModelRouteMigrationConflict as error:
        return product_http._json_response(409, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    except (ModelRouteMigrationError, ValueError) as error:
        return product_http._json_response(400, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    return product_http._json_response(200, result, product_http._no_store_headers())


@router.post("/api/rebuild/model-routes/migrations/{migration_id}/rollback")
async def model_route_migration_rollback(
    migration_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    body = await product_http._json_body(request)
    expected_revision = body.get("expected_registry_revision")
    if not isinstance(expected_revision, int):
        return product_http._json_response(400, {"detail": "expected_registry_revision is required"}, product_http._no_store_headers())
    try:
        result = ModelRouteMigrationService(ModelRouteRegistry(container.root_dir)).rollback(
            migration_id,
            expected_registry_revision=expected_revision,
            confirm=body.get("confirm") is True,
        )
    except ModelRouteMigrationConflict as error:
        return product_http._json_response(409, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    except ModelRouteMigrationError as error:
        return product_http._json_response(400, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    return product_http._json_response(200, result, product_http._no_store_headers())


@router.get("/api/model-route-runtime")
async def model_route_runtime_status(container: ApiContainerDep) -> JSONResponse:
    try:
        result = ModelRouteRuntimeService(container.root_dir).status()
    except ModelRouteRuntimeError as error:
        return product_http._json_response(409, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    return product_http._json_response(200, result, product_http._no_store_headers())


@router.post("/api/model-route-runtime/preview")
async def model_route_runtime_preview(request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await product_http._json_body(request)
    route_keys = body.get("route_keys", ["intake.classification"])
    if not isinstance(route_keys, list):
        return product_http._json_response(400, {"detail": "route_keys must be an array"}, product_http._no_store_headers())
    try:
        compatibility, providers = product_providers._model_route_runtime_contexts(container)
        result = ModelRouteRuntimeService(container.root_dir).preview(
            route_keys=route_keys, compatibility=compatibility, providers=providers,
        )
    except ModelRouteRuntimeError as error:
        return product_http._json_response(409, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    return product_http._json_response(200, result, product_http._no_store_headers())


@router.post("/api/model-route-runtime/activate")
async def model_route_runtime_activate(request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await product_http._json_body(request)
    try:
        compatibility, providers = product_providers._model_route_runtime_contexts(container)
        result = ModelRouteRuntimeService(container.root_dir).activate(
            shadow_token=str(body.get("shadow_token") or ""),
            route_keys=body.get("route_keys", ["intake.classification"]),
            expected_runtime_revision=int(body.get("expected_runtime_revision")),
            confirm=body.get("confirm") is True,
            compatibility=compatibility,
            providers=providers,
        )
    except ModelRouteRuntimeConflict as error:
        return product_http._json_response(409, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    except ModelRouteRuntimeError as error:
        return product_http._json_response(400, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    except (TypeError, ValueError) as error:
        return product_http._json_response(400, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    return product_http._json_response(200, result, product_http._no_store_headers())


@router.post("/api/model-route-runtime/deactivate")
async def model_route_runtime_deactivate(request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await product_http._json_body(request)
    try:
        result = ModelRouteRuntimeService(container.root_dir).deactivate(
            expected_runtime_revision=int(body.get("expected_runtime_revision")),
            confirm=body.get("confirm") is True,
        )
    except ModelRouteRuntimeConflict as error:
        return product_http._json_response(409, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    except ModelRouteRuntimeError as error:
        return product_http._json_response(400, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    except (TypeError, ValueError) as error:
        return product_http._json_response(400, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    return product_http._json_response(200, result, product_http._no_store_headers())
