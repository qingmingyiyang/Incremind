"""Developer test lab ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json, uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.ai_runtime import get_or_build_ai_runtime
from backend.api.ai_turn_runner import get_or_build_ai_turn_runner
from backend.api.container import ApiContainerDep
from backend.api.developer_studio_test_lab_ai_runtime import (
    DEVELOPER_STUDIO_TEST_LAB_CAPABILITY,
    DEVELOPER_STUDIO_TEST_LAB_OUTCOME,
    DEVELOPER_STUDIO_TEST_LAB_SNAPSHOT_KIND,
)

from core.ai_kernel import validate_execution_projection
from core.product_core.developer_studio_config import GetDeveloperStudioConfig
from core.product_core.model_route_runtime import (
    ModelRouteRuntimeConflict,
    ModelRouteRuntimeError,
    ModelRouteRuntimeService,
)
from core.product_core.processing_recipe import ProcessingRecipeConflict, ProcessingRecipeError
from core.product_core.prompt_activation import resolve_active_prompt, serialize_prompt_activation

from . import developer_logs as product_developer_logs
from . import http as product_http
from . import processing_recipes as product_processing_recipes
from . import providers as product_providers
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


_DEV_TEST_TYPES = frozenset({"prompt", "recipe", "pipeline", "video", "search"})


_DEV_TEST_MAX_INPUT_CHARS = 8000


def _default_test_lab_system_prompt(test_type: str) -> str:
    """未指定 prompt 或 prompt 为空时的回退系统提示词。"""
    if test_type == "pipeline":
        return (
            "你是 Chrip_OS 的输入理解与结构化测试模块。"
            "请把用户输入识别为 text/link/note/file/image/audio/video 之一，"
            "并输出 JSON：{ input_type, intent, confidence, route }。"
        )
    if test_type == "search":
        return (
            "你是 Chrip_OS 的搜索测试模块。"
            "请基于用户输入给出一段简短回答，并附 3 条相关关键词。"
            "输出 JSON：{ summary, keywords }。"
        )
    return (
        "你是 Chrip_OS 的提示词测试模块。请对用户输入做出简短响应。"
        "输出 JSON：{ response, summary }。"
    )


class _TestLabFidelityError(ValueError):
    pass


class _TestLabFidelityConflict(_TestLabFidelityError):
    pass


def _test_lab_route_resolution(
    container: ApiContainerDep,
    *,
    route_key: str,
    expected_runtime_revision: int,
) -> tuple[dict[str, object], dict[str, object]]:
    if route_key != "intake.classification":
        raise _TestLabFidelityError("unsupported Test Lab model route")
    runtime = ModelRouteRuntimeService(container.root_dir)
    status = runtime.status()
    if status["runtime_revision"] != expected_runtime_revision:
        raise _TestLabFidelityConflict("Test Lab model route runtime revision conflict")
    compatibility, providers = product_providers._model_route_runtime_contexts(container)
    try:
        resolution = runtime.resolve(
            route_key,
            compatibility=compatibility[route_key],
            providers=providers,
        )
    except ModelRouteRuntimeConflict as error:
        raise _TestLabFidelityConflict(str(error)) from error
    except ModelRouteRuntimeError as error:
        raise _TestLabFidelityError(str(error)) from error
    evidence = {
        key: resolution[key]
        for key in (
            "route_key", "source", "provider_id", "provider_revision", "model_name",
            "registry_revision", "route_revision", "runtime_revision",
        )
    }
    return dict(resolution), evidence


def _test_lab_prompt_snapshot(
    store: object,
    *,
    prompt_id: str,
    source: str,
    expected_config_revision: int,
    expected_activation_revision: int | None,
    expected_unit_revision: int | None,
) -> tuple[str, dict[str, object]]:
    try:
        config = GetDeveloperStudioConfig(store).execute()  # type: ignore[arg-type]
    except ValueError as error:
        raise _TestLabFidelityConflict("Test Lab Prompt authority is unreadable") from error
    if config.revision != expected_config_revision:
        raise _TestLabFidelityConflict("Test Lab Prompt config revision conflict")
    if source == "draft":
        prompt = next((item for item in config.prompts if item.get("id") == prompt_id), None)
        if prompt is None:
            raise _TestLabFidelityError("Test Lab draft Prompt not found")
        content = prompt.get("content")
        if not isinstance(content, str) or not content.strip():
            raise _TestLabFidelityError("Test Lab draft Prompt content is empty")
        return content, {
            "prompt_id": prompt_id,
            "source": "draft",
            "config_revision": config.revision,
            "prompt_version": int(prompt.get("version") or 0),
            "activation_revision": None,
            "unit_id": None,
            "unit_revision": None,
        }
    if source != "active":
        raise _TestLabFidelityError("Test Lab Prompt source must be draft or active")
    activation = serialize_prompt_activation(config)
    if expected_activation_revision is None or activation["activation_revision"] != expected_activation_revision:
        raise _TestLabFidelityConflict("Test Lab Prompt activation revision conflict")
    unit = next((item for item in activation["units"] if prompt_id in item["prompt_ids"]), None)
    if unit is None or expected_unit_revision is None or unit["unit_revision"] != expected_unit_revision:
        raise _TestLabFidelityConflict("Test Lab Prompt unit revision conflict")
    prompt = resolve_active_prompt(config, prompt_id)
    if prompt is None:
        raise _TestLabFidelityError("Test Lab active Prompt not found")
    content = prompt.get("content")
    if not isinstance(content, str) or not content.strip():
        raise _TestLabFidelityError("Test Lab active Prompt content is empty")
    return content, {
        "prompt_id": prompt_id,
        "source": "active",
        "config_revision": config.revision,
        "prompt_version": int(prompt.get("version") or 0),
        "activation_revision": activation["activation_revision"],
        "unit_id": unit["unit_id"],
        "unit_revision": unit["unit_revision"],
    }


@router.post("/api/rebuild/developer-studio/test-lab")
async def developer_studio_test_lab(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    """Resolve the exact selected authorities, then run a side-effect-free Provider test."""
    body = await product_http._json_body(request) or {}
    test_type = str(body.get("test_type") or "").strip().lower()
    if test_type not in _DEV_TEST_TYPES:
        return product_http._json_response(
            400,
            {"detail": f"invalid test_type: {test_type}; expected one of {sorted(_DEV_TEST_TYPES)}"},
            product_http._no_store_headers(),
        )

    user_input = str(body.get("input") or "").strip()
    if not user_input:
        return product_http._json_response(
            400,
            {"detail": "input cannot be empty"},
            product_http._no_store_headers(),
        )
    if len(user_input) > _DEV_TEST_MAX_INPUT_CHARS:
        user_input = user_input[:_DEV_TEST_MAX_INPUT_CHARS]

    if test_type == "video":
        return product_http._json_response(
            200,
            {
                "test_type": test_type,
                "raw": "",
                "parsed": None,
                "valid": False,
                "elapsed_ms": 0,
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                "error": "视频测试需要本地 ASR 模型，暂不支持远程调用。请在普通模式中导入视频后测试。",
                "skipped": True,
                "provider_call_performed": False,
                "resolved": None,
            },
            product_http._no_store_headers(),
        )
    if body.get("provider_call_confirmed") is not True:
        return product_http._json_response(
            409,
            {
                "detail": "Test Lab Provider call requires explicit confirmation",
                "actionable": True,
                "provider_call_performed": False,
            },
            product_http._no_store_headers(),
        )
    store, _settings = product_repositories._object_store(container.root_dir)
    try:
        route_key = str(body.get("route_key") or "")
        expected_runtime_revision = product_http._required_body_int(body, "expected_runtime_revision")
        recipe_evidence: dict[str, object] | None = None
        prompt_evidence: dict[str, object]
        if test_type == "recipe":
            recipe_evidence = product_processing_recipes._processing_recipe_registry(container, store).evaluate_for_test(
                str(body.get("recipe_id") or ""),
                source=str(body.get("recipe_source") or ""),
                expected_registry_revision=product_http._required_body_int(body, "expected_recipe_registry_revision"),
                expected_recipe_revision=product_http._required_body_int(body, "expected_recipe_revision"),
                content_type=str(body.get("content_type") or "text"),
                text=user_input,
                trigger=str(body.get("trigger") or ""),
            )
            if recipe_evidence["status"] != "matched":
                return product_http._json_response(
                    200,
                    {
                        "test_type": "recipe",
                        "raw": "",
                        "parsed": None,
                        "valid": False,
                        "elapsed_ms": 0,
                        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                        "error": "Processing Recipe matcher did not match the sample input.",
                        "skipped": True,
                        "provider_call_performed": False,
                        "resolved": {"recipe": recipe_evidence},
                    },
                    product_http._no_store_headers(),
                )
            recipe = recipe_evidence["recipe"]
            route_key = str(recipe["model_route_key"])
            prompt_ref = recipe["prompt_ref"]
            prompt_content, prompt_evidence = _test_lab_prompt_snapshot(
                store,
                prompt_id=str(prompt_ref["prompt_id"]),
                source="active",
                expected_config_revision=product_http._required_body_int(body, "expected_config_revision"),
                expected_activation_revision=product_http._required_body_int(body, "expected_activation_revision"),
                expected_unit_revision=int(prompt_ref["unit_revision"]),
            )
        elif test_type == "prompt":
            prompt_content, prompt_evidence = _test_lab_prompt_snapshot(
                store,
                prompt_id=str(body.get("prompt_id") or ""),
                source=str(body.get("prompt_source") or ""),
                expected_config_revision=product_http._required_body_int(body, "expected_config_revision"),
                expected_activation_revision=product_http._optional_body_int(body, "expected_activation_revision"),
                expected_unit_revision=product_http._optional_body_int(body, "expected_prompt_unit_revision"),
            )
        else:
            prompt_content = _default_test_lab_system_prompt(test_type)
            prompt_evidence = {
                "prompt_id": f"built-in:test-lab-{test_type}",
                "source": "built_in",
                "config_revision": None,
                "prompt_version": 1,
                "activation_revision": None,
                "unit_id": None,
                "unit_revision": None,
            }
        resolution, route_evidence = _test_lab_route_resolution(
            container,
            route_key=route_key,
            expected_runtime_revision=expected_runtime_revision,
        )
    except (ProcessingRecipeConflict, _TestLabFidelityConflict) as error:
        return product_http._json_response(409, {"detail": str(error), "actionable": True, "provider_call_performed": False}, product_http._no_store_headers())
    except (ProcessingRecipeError, _TestLabFidelityError, ValueError) as error:
        return product_http._json_response(400, {"detail": str(error), "actionable": True, "provider_call_performed": False}, product_http._no_store_headers())

    project_id = str(body.get("project_id") or "default").strip() or "default"
    token = uuid.uuid4().hex
    recipe_snapshot = None
    if isinstance(recipe_evidence, Mapping):
        recipe_record = recipe_evidence.get("recipe")
        if isinstance(recipe_record, Mapping):
            recipe_snapshot = {
                "recipe_id": str(recipe_record.get("recipe_id") or recipe_record.get("id") or "recipe"),
                "recipe_revision": int(recipe_record.get("revision") or 1),
                "registry_revision": int(recipe_evidence.get("registry_revision") or 1),
                "evidence_ref": f"crp://default/developer-studio/recipes/{str(recipe_record.get('recipe_id') or recipe_record.get('id') or 'recipe')}",
            }
    snapshot = {
        "schema_version": "1.0.0",
        "kind": DEVELOPER_STUDIO_TEST_LAB_SNAPSHOT_KIND,
        "project_id": project_id,
        "test_type": test_type,
        "input": user_input,
        "system_prompt": prompt_content,
        "model_capability": "structured",
        "route": {
            "route_key": str(route_evidence["route_key"]),
            "route_revision": int(route_evidence["route_revision"]),
            "provider_id": str(route_evidence["provider_id"]),
            "provider_revision": str(route_evidence["provider_revision"]),
            "model_name": str(route_evidence["model_name"]),
            "runtime_revision": int(route_evidence["runtime_revision"]),
            "evidence_ref": f"crp://default/model-routes/{str(route_evidence['route_key'])}",
        },
        "prompt": {
            "prompt_id": str(prompt_evidence["prompt_id"]),
            "source": str(prompt_evidence["source"]),
            "config_revision": int(prompt_evidence.get("config_revision") or 1),
            "evidence_ref": f"crp://default/developer-studio/prompts/{str(prompt_evidence['prompt_id']).replace(':', '/')}",
        },
        "recipe": recipe_snapshot,
    }
    turn_request = {
        "schema_version": "1.0.0",
        "turn_id": f"turn-{token}",
        "session_id": f"developer-studio.test-lab.{project_id}",
        "operation_id": f"op-test-lab-{token[:20]}",
        "idempotency_key": f"developer-studio-test-lab-{token}",
        "scope": {"kind": "project", "project_id": project_id, "series_id": None},
        "input": {"kind": "text", "text": json.dumps(snapshot, ensure_ascii=False), "refs": []},
        "desired_outcome": DEVELOPER_STUDIO_TEST_LAB_OUTCOME,
        "privacy": {
            "mode": "remote_allowed", "allow_remote": True, "pii": "possible",
            "consent_refs": ["crp://default/consent/provider-egress-policy"],
            "retention": "local_durable",
        },
        "capability_policy": {
            "allowed": [DEVELOPER_STUDIO_TEST_LAB_CAPABILITY], "denied": [],
            "require_approval": [DEVELOPER_STUDIO_TEST_LAB_CAPABILITY],
        },
        "context_policy": {
            "include_project_skill": False, "include_memory": False,
            "include_session_history": False, "max_context_bytes": 32768,
        },
        "approval_policy": {"mode": "explicit", "auto_approve_read_only": True},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        runtime = get_or_build_ai_runtime(request, container)
        waiting = runtime.submit_turn(turn_request)
        if waiting.status != "waiting_approval":
            raise ValueError("Developer Studio Test Lab AI Turn did not reach approval")
        approval = next(
            event for event in reversed(tuple(runtime.events_after(waiting.turn_id)))
            if event.get("type") == "approval.required"
        )
        completed = get_or_build_ai_turn_runner(request, runtime).apply_action_and_wait({
            "schema_version": "1.0.0", "action_id": f"action-{uuid.uuid4().hex}",
            "turn_id": waiting.turn_id, "type": "approve",
            "target_event_id": approval["event_id"],
            "reason": "provider_call_confirmed mapped to governed Test Lab approval",
            "actor": "user", "expected_sequence": waiting.current_sequence,
            "idempotency_key": f"approve-test-lab-{token}",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        if completed.status != "completed":
            failed_events = tuple(runtime.events_after(completed.turn_id))
            wire_dispatched = any(
                event.get("type") == "model.attempt.dispatched"
                for event in failed_events
            )
            unknown_effect = any(
                event.get("type") == "tool.failed"
                and isinstance(event.get("data"), Mapping)
                and event["data"].get("error_code") == "ai.tool_outcome_unknown"
                for event in failed_events
            )
            return product_http._json_response(409, {
                "detail": "Developer Studio Test Lab AI Turn failed",
                "reason": "model_effect_unknown" if unknown_effect else "governed_turn_failed",
                "provider_call_performed": wire_dispatched,
                "effect_certainty": "unknown" if unknown_effect else "confirmed_none",
                "retry_safe": not wire_dispatched and not unknown_effect,
                "turn_id": completed.turn_id,
            }, product_http._no_store_headers())
        result_content = runtime.presentation_for(completed.turn_id)
        if not isinstance(result_content, Mapping):
            raise ValueError("Developer Studio Test Lab result is unavailable")
        execution_projection = validate_execution_projection(
            runtime.execution_projection_for(completed.turn_id, view="developer")
        )
    except Exception as error:  # noqa: BLE001 - Turn/runtime boundary is sanitized below
        return product_http._json_response(409, {
            "detail": "Developer Studio Test Lab AI Turn failed",
            "reason": product_developer_logs._sanitize_dev_log_text(str(error)),
            "provider_call_performed": False,
            "turn_id": turn_request["turn_id"],
        }, product_http._no_store_headers())
    return product_http._json_response(
        200,
        {
            "test_type": test_type,
            "stage": test_type,
            "steps": result_content.get("steps"),
            "raw": "",
            "parsed": None,
            "valid": True,
            "elapsed_ms": 0,
            "usage": result_content.get("usage", {}),
            "error": "",
            "provider_call_performed": True,
            "model": result_content.get("model_name", route_evidence["model_name"]),
            "turn_id": completed.turn_id,
            "operation_id": turn_request["operation_id"],
            "execution": execution_projection,
            "resolved": {
                "route": route_evidence,
                "prompt": prompt_evidence,
                "recipe": recipe_evidence,
                "output_schema": None,
                "result_metadata": result_content.get("output"),
            },
        },
        product_http._no_store_headers(),
    )
