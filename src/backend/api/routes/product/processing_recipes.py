"""Processing recipes ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep

from core.product_core.developer_studio_config import (
    DeveloperStudioConfigError,
    GetDeveloperStudioConfig,
)
from core.product_core.model_route_registry import ModelRouteRegistry
from core.product_core.processing_recipe import (
    ProcessingRecipeConflict,
    ProcessingRecipeError,
    ProcessingRecipeRegistry,
    ProcessingRecipeRuntime,
)
from core.product_core.prompt_activation import serialize_prompt_activation

from . import http as product_http
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


def _processing_recipe_registry(container: ApiContainerDep, store: object) -> ProcessingRecipeRegistry:
    def validate_authorities(recipe: Mapping[str, object]) -> None:
        prompt_ref = recipe.get("prompt_ref")
        if not isinstance(prompt_ref, Mapping):
            raise ProcessingRecipeError("processing recipe prompt authority is missing")
        try:
            config = GetDeveloperStudioConfig(store).execute()  # type: ignore[arg-type]
            activation = serialize_prompt_activation(config)
        except ValueError as error:
            raise ProcessingRecipeError("processing recipe prompt authority is unreadable") from error
        unit = next(
            (item for item in activation["units"] if item["unit_id"] == prompt_ref.get("unit_id")),
            None,
        )
        if unit is None or unit["unit_revision"] != prompt_ref.get("unit_revision"):
            raise ProcessingRecipeError("processing recipe prompt authority revision drifted")
        if prompt_ref.get("prompt_id") not in unit["active_prompt_ids"]:
            raise ProcessingRecipeError("processing recipe active prompt is missing")
        try:
            route = ModelRouteRegistry(container.root_dir).get(str(recipe.get("model_route_key") or ""))["route"]
        except ValueError as error:
            raise ProcessingRecipeError("processing recipe model route authority is missing") from error
        if route.get("enabled") is not True:
            raise ProcessingRecipeError("processing recipe model route authority is disabled")
        if route.get("revision") != recipe.get("model_route_revision"):
            raise ProcessingRecipeError("processing recipe model route revision drifted")

    return ProcessingRecipeRegistry(store, authority_validator=validate_authorities)  # type: ignore[arg-type]


def _processing_recipe_preflight(
    container: ApiContainerDep,
    store: object,
    body: object,
    *,
    consumer: str,
) -> dict[str, object] | None:
    """Resolve the side-effect-free Recipe before the existing Workbench use case."""
    if not isinstance(body, Mapping):
        return None
    content = body.get("content", "")
    if not isinstance(content, str):
        return None
    content_type = _processing_recipe_content_type(body)
    return ProcessingRecipeRuntime(_processing_recipe_registry(container, store)).preflight(
        content_type=content_type,
        text=content,
        trigger=consumer,
    )


def _processing_recipe_content_type(body: Mapping[str, object]) -> str:
    media_type = body.get("media_type")
    if isinstance(media_type, str) and media_type.strip():
        normalized = media_type.casefold()
        for content_type in ("video", "audio", "image"):
            if content_type in normalized:
                return content_type
        return "file"
    urls = body.get("urls")
    if isinstance(urls, Sequence) and not isinstance(urls, (str, bytes)) and urls:
        return "link"
    if body.get("child_inputs") or body.get("file_name") or body.get("original_asset_ref"):
        return "file"
    return "text"


def _workbench_response_with_recipe_trace(
    response: Any,
    trace: Mapping[str, object] | None,
) -> JSONResponse:
    body = dict(response.body)
    if trace is not None:
        body["processing_recipe_trace"] = dict(trace)
    return product_http._json_response(response.status_code, body, response.headers)


@router.get("/api/rebuild/developer-studio/processing-recipes")
async def developer_studio_processing_recipe_status(container: ApiContainerDep) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    try:
        payload = _processing_recipe_registry(container, store).status()
    except ProcessingRecipeError as error:
        return product_http._json_response(409, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.post("/api/rebuild/developer-studio/processing-recipes/drafts/preview")
async def developer_studio_processing_recipe_draft_preview(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request) or {}
    recipe = body.get("recipe")
    if not isinstance(recipe, Mapping):
        return product_http._json_response(400, {"detail": "recipe must be an object", "actionable": True}, product_http._no_store_headers())
    try:
        payload = _processing_recipe_registry(container, store).preview_draft(recipe)
    except ProcessingRecipeError as error:
        return product_http._json_response(400, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.put("/api/rebuild/developer-studio/processing-recipes/drafts")
async def developer_studio_processing_recipe_draft_save(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request) or {}
    recipe = body.get("recipe")
    if not isinstance(recipe, Mapping):
        return product_http._json_response(400, {"detail": "recipe must be an object", "actionable": True}, product_http._no_store_headers())
    try:
        payload = _processing_recipe_registry(container, store).save_draft(
            recipe,
            expected_registry_revision=product_http._required_body_int(body, "expected_registry_revision"),
            validation_token=str(body.get("validation_token") or ""),
        )
    except ProcessingRecipeConflict as error:
        return product_http._json_response(409, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    except (ProcessingRecipeError, ValueError) as error:
        return product_http._json_response(400, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.post("/api/rebuild/developer-studio/processing-recipes/activation/preview")
async def developer_studio_processing_recipe_activation_preview(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request) or {}
    try:
        payload = _processing_recipe_registry(container, store).preview_activation(
            str(body.get("recipe_id") or ""),
            expected_registry_revision=product_http._required_body_int(body, "expected_registry_revision"),
            expected_recipe_revision=product_http._required_body_int(body, "expected_recipe_revision"),
        )
    except ProcessingRecipeConflict as error:
        return product_http._json_response(409, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    except (ProcessingRecipeError, ValueError) as error:
        return product_http._json_response(400, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.post("/api/rebuild/developer-studio/processing-recipes/activate")
async def developer_studio_processing_recipe_activate(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request) or {}
    try:
        payload = _processing_recipe_registry(container, store).activate(
            str(body.get("recipe_id") or ""),
            expected_registry_revision=product_http._required_body_int(body, "expected_registry_revision"),
            expected_recipe_revision=product_http._required_body_int(body, "expected_recipe_revision"),
            activation_token=str(body.get("activation_token") or ""),
            confirm=body.get("confirm") is True,
            reason=str(body.get("reason") or ""),
        )
    except ProcessingRecipeConflict as error:
        return product_http._json_response(409, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    except (ProcessingRecipeError, ValueError) as error:
        return product_http._json_response(400, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.post("/api/rebuild/developer-studio/processing-recipes/deactivate")
async def developer_studio_processing_recipe_deactivate(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request) or {}
    try:
        payload = _processing_recipe_registry(container, store).deactivate(
            str(body.get("recipe_id") or ""),
            expected_registry_revision=product_http._required_body_int(body, "expected_registry_revision"),
            confirm=body.get("confirm") is True,
            reason=str(body.get("reason") or ""),
        )
    except ProcessingRecipeConflict as error:
        return product_http._json_response(409, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    except (ProcessingRecipeError, ValueError) as error:
        return product_http._json_response(400, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.post("/api/rebuild/developer-studio/processing-recipes/rollback")
async def developer_studio_processing_recipe_rollback(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request) or {}
    try:
        payload = _processing_recipe_registry(container, store).rollback(
            str(body.get("recipe_id") or ""),
            expected_registry_revision=product_http._required_body_int(body, "expected_registry_revision"),
            confirm=body.get("confirm") is True,
            reason=str(body.get("reason") or ""),
        )
    except ProcessingRecipeConflict as error:
        return product_http._json_response(409, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    except (ProcessingRecipeError, ValueError) as error:
        return product_http._json_response(400, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.get("/api/rebuild/developer-studio/processing-recipes/legacy-preview")
async def developer_studio_processing_recipe_legacy_preview(container: ApiContainerDep) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    try:
        config = GetDeveloperStudioConfig(store).execute()
        payload = _processing_recipe_registry(container, store).legacy_preview(config.skills)
    except (DeveloperStudioConfigError, ProcessingRecipeError) as error:
        return product_http._json_response(409, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    return product_http._json_response(200, payload, product_http._no_store_headers())
