"""Developer prompts ownership for the product API."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep

from core.product_core.developer_studio_config import (
    DeveloperStudioConfigError,
    GetDeveloperStudioConfig,
    SaveDeveloperStudioConfig,
    serialize_developer_studio_config,
)
from core.product_core.prompt_activation import (
    PromptActivationConflict,
    PromptActivationError,
    PromptActivationService,
    serialize_prompt_activation,
    serialize_prompt_activation_result,
)

from . import http as product_http
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.get("/api/rebuild/developer-studio/config")
@router.put("/api/rebuild/developer-studio/config")
async def developer_studio_config(request: Request, container: ApiContainerDep) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    if request.method.upper() == "GET":
        try:
            payload = serialize_developer_studio_config(GetDeveloperStudioConfig(store).execute())
        except PromptActivationError as error:
            return product_http._json_response(
                409,
                {"status": "rejected", "detail": "prompt activation authority drifted", "error": str(error)},
                product_http._no_store_headers(),
            )
        return product_http._json_response(200, payload, product_http._no_store_headers())
    body = await product_http._json_body(request)
    try:
        expected_revision = body.get("expected_revision")
        config = SaveDeveloperStudioConfig(store).execute(
            model_profiles=product_http._list_body(body, "model_profiles"),
            task_model_map=(
                product_http._mapping_body(body, "task_model_map")
                if "task_model_map" in body
                else None
            ),
            prompts=product_http._list_body(body, "prompts"),
            skills=product_http._list_body(body, "skills"),
            workflow_steps=product_http._list_body(body, "workflow_steps"),
            snapshots=product_http._list_body(body, "snapshots", default=[]),
            expected_revision=expected_revision if isinstance(expected_revision, int) else None,
        )
    except PromptActivationError as error:
        return product_http._json_response(
            409,
            {"status": "rejected", "detail": "prompt activation authority drifted", "error": str(error)},
            product_http._no_store_headers(),
        )
    except DeveloperStudioConfigError as error:
        is_conflict = str(error) == "developer studio config revision conflict"
        return product_http._json_response(
            409 if is_conflict else 400,
            {
                "status": "rejected",
                "detail": "developer studio config rejected",
                "error": str(error),
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        serialize_developer_studio_config(config),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.get("/api/rebuild/developer-studio/prompt-activation")
async def developer_studio_prompt_activation_status(container: ApiContainerDep) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    try:
        payload = serialize_prompt_activation(GetDeveloperStudioConfig(store).execute())
    except (DeveloperStudioConfigError, PromptActivationError) as error:
        return product_http._json_response(409, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.post("/api/rebuild/developer-studio/prompt-activation/preview")
async def developer_studio_prompt_activation_preview(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request) or {}
    try:
        result = PromptActivationService(store).preview(
            unit_id=str(body.get("unit_id") or ""),
            expected_config_revision=product_http._required_body_int(body, "expected_config_revision"),
            expected_activation_revision=product_http._required_body_int(body, "expected_activation_revision"),
        )
    except PromptActivationConflict as error:
        return product_http._json_response(409, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    except (PromptActivationError, ValueError) as error:
        return product_http._json_response(400, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    return product_http._json_response(200, result, product_http._no_store_headers())


@router.post("/api/rebuild/developer-studio/prompt-activation/activate")
async def developer_studio_prompt_activation_activate(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request) or {}
    try:
        result = PromptActivationService(store).activate(
            unit_id=str(body.get("unit_id") or ""),
            expected_config_revision=product_http._required_body_int(body, "expected_config_revision"),
            expected_activation_revision=product_http._required_body_int(body, "expected_activation_revision"),
            preview_token=str(body.get("preview_token") or ""),
            confirm=body.get("confirm") is True,
            reason=str(body.get("reason") or ""),
        )
    except PromptActivationConflict as error:
        return product_http._json_response(409, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    except (PromptActivationError, ValueError) as error:
        return product_http._json_response(400, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    payload = serialize_prompt_activation_result(result)
    payload["prompt_activation_status"] = serialize_prompt_activation(GetDeveloperStudioConfig(store).execute())
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.post("/api/rebuild/developer-studio/prompt-activation/rollback")
async def developer_studio_prompt_activation_rollback(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request) or {}
    try:
        result = PromptActivationService(store).rollback(
            unit_id=str(body.get("unit_id") or ""),
            expected_config_revision=product_http._required_body_int(body, "expected_config_revision"),
            expected_activation_revision=product_http._required_body_int(body, "expected_activation_revision"),
            confirm=body.get("confirm") is True,
            reason=str(body.get("reason") or ""),
        )
    except PromptActivationConflict as error:
        return product_http._json_response(409, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    except (PromptActivationError, ValueError) as error:
        return product_http._json_response(400, {"detail": str(error), "actionable": True}, product_http._no_store_headers())
    payload = serialize_prompt_activation_result(result)
    payload["prompt_activation_status"] = serialize_prompt_activation(GetDeveloperStudioConfig(store).execute())
    return product_http._json_response(200, payload, product_http._no_store_headers())
