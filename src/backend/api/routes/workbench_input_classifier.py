from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.ai_runtime import get_or_build_ai_runtime
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.workbench_input_classifier_ai_runtime import (
    WORKBENCH_INPUT_CLASSIFICATION_CONTEXT_CAPABILITY,
    WORKBENCH_INPUT_CLASSIFICATION_ENHANCE_CAPABILITY,
    WORKBENCH_INPUT_CLASSIFICATION_OUTCOME,
    WorkbenchClassificationInputGrantStore,
)
from backend.api.workbench_input_classifier_runtime import (
    build_workbench_input_classifier_runtime,
)
from core.product_core.model_route_runtime import ModelRouteRuntimeError
from core.product_core.processing_recipe import ProcessingRecipeError


router = APIRouter(tags=["workbench-input-classifier"])
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")


async def _json_body(request: Request) -> Mapping[str, object] | None:
    try:
        payload: Any = await request.json()
    except Exception:
        return None
    return payload if isinstance(payload, Mapping) else None


def _path_with_query(request: Request) -> str:
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


def _no_store_headers() -> dict[str, str]:
    return {"Content-Type": "application/json", "Cache-Control": "no-store"}


def _json_response(
    status_code: int,
    body: Mapping[str, Any],
    headers: Mapping[str, str],
) -> JSONResponse:
    return JSONResponse(
        content=body,
        status_code=status_code,
        headers={key: value for key, value in headers.items() if key.lower() != "content-type"},
    )


@router.post("/api/rebuild/workbench/input-classifier")
async def workbench_input_classifier(request: Request, container: ApiContainerDep) -> JSONResponse:
    store, _settings = build_rebuild_object_store(container.root_dir)
    body = await _json_body(request)
    remote_requested = isinstance(body, Mapping) and body.get("allow_provider_enhancement") is True
    if remote_requested and (not isinstance(body.get("request_id"), str) or _REQUEST_ID.fullmatch(body["request_id"]) is None):
        return _json_response(400, {"detail": "request_id is required for provider enhancement", "actionable": True}, _no_store_headers())
    # Preserve the deterministic endpoint contract and recipe preflight, while
    # ensuring this compatibility owner never invokes legacy runtime.enhance.
    local_body = dict(body) if isinstance(body, Mapping) else body
    if isinstance(local_body, dict): local_body["allow_provider_enhancement"] = False
    try:
        response = build_workbench_input_classifier_runtime(container, store).execute(
            method=request.method,
            path=_path_with_query(request),
            body=local_body,
        )
    except ProcessingRecipeError as error:
        return _json_response(409, {"detail": str(error), "actionable": True}, _no_store_headers())
    except ModelRouteRuntimeError as error:
        return _json_response(409, {"detail": str(error), "actionable": True}, _no_store_headers())
    if not remote_requested or response.status_code != 200:
        return _json_response(response.status_code, response.body, response.headers)
    assert isinstance(body, Mapping)
    try:
        project_id = _project_id(body.get("project_id"))
    except ValueError as error:
        return _json_response(400, {"detail": str(error), "actionable": True}, _no_store_headers())
    grant_store = _input_grant_store(request)
    grant_id = f"input-grant-{project_id}-{body['request_id']}"
    grant_store.issue({
        "content": body.get("content", ""), "media_type": body.get("media_type", ""),
        "file_name": body.get("file_name", ""), "urls": body.get("urls", ()),
    }, grant_id=grant_id)
    try:
        runtime = get_or_build_ai_runtime(request, container)
        metadata = getattr(runtime, "composition_metadata", {})
        remote_usable = isinstance(metadata, Mapping) and metadata.get("intake_classification_remote_usable") is True
        if not remote_usable:
            return _json_response(response.status_code, response.body, response.headers)
        turn = _turn_request(project_id=project_id, request_id=str(body["request_id"]), grant_id=grant_id, remote_usable=remote_usable)
        waiting = runtime.submit_turn(turn)
        if getattr(waiting, "status", None) == "waiting_approval":
            waiting = runtime.apply_action(_approval_action(runtime, waiting, turn))
        if getattr(waiting, "status", None) != "completed":
            return _json_response(409, {"detail": "workbench input classification enhancement failed", "actionable": True}, _no_store_headers())
        presentation = runtime.presentation_for(str(getattr(waiting, "turn_id")))
        if not isinstance(presentation, Mapping) or presentation.get("status") != "completed" or not isinstance(presentation.get("classification"), Mapping):
            return _json_response(409, {"detail": "workbench input classification enhancement is unavailable", "actionable": True}, _no_store_headers())
        result = dict(presentation["classification"])
        if "processing_recipe_trace" in response.body:
            result["processing_recipe_trace"] = response.body["processing_recipe_trace"]
        return _json_response(200, result, response.headers)
    except (ValueError, KeyError, RuntimeError):
        return _json_response(409, {"detail": "workbench input classification enhancement failed", "actionable": True}, _no_store_headers())
    finally:
        grant_store.revoke(grant_id)


def _input_grant_store(request: Request) -> WorkbenchClassificationInputGrantStore:
    store = getattr(request.app.state, "workbench_classification_input_store", None)
    if isinstance(store, WorkbenchClassificationInputGrantStore): return store
    store = WorkbenchClassificationInputGrantStore()
    request.app.state.workbench_classification_input_store = store
    return store


def _project_id(value: object) -> str:
    if value is None: return "default"
    if not isinstance(value, str) or _PROJECT_ID.fullmatch(value) is None: raise ValueError("project_id is invalid")
    return value


def _turn_request(*, project_id: str, request_id: str, grant_id: str, remote_usable: bool) -> dict[str, object]:
    identity = f"{project_id}:{request_id}"
    return {"schema_version": "1.0.0", "turn_id": f"turn-workbench-classifier-{identity}", "session_id": f"session-workbench-classifier-{identity}", "operation_id": request_id, "idempotency_key": f"workbench-classifier-{identity}", "scope": {"kind": "project", "project_id": project_id, "series_id": None}, "input": {"kind": "text", "text": "enhance selected workbench input", "refs": [{"kind": "workbench_input", "object_id": grant_id, "uri": f"crp://default/workbench/inputs/{grant_id}"}]}, "desired_outcome": WORKBENCH_INPUT_CLASSIFICATION_OUTCOME, "privacy": {"mode": "remote_allowed" if remote_usable else "local_only", "allow_remote": remote_usable, "pii": "possible", "consent_refs": ["crp://default/consent/provider-egress-policy"] if remote_usable else [], "retention": "local_durable"}, "capability_policy": {"allowed": [WORKBENCH_INPUT_CLASSIFICATION_CONTEXT_CAPABILITY, WORKBENCH_INPUT_CLASSIFICATION_ENHANCE_CAPABILITY], "denied": [], "require_approval": [WORKBENCH_INPUT_CLASSIFICATION_ENHANCE_CAPABILITY]}, "context_policy": {"include_project_skill": False, "include_memory": False, "include_session_history": False, "max_context_bytes": 4096}, "approval_policy": {"mode": "explicit", "auto_approve_read_only": True}, "created_at": "2026-08-23T00:00:00+00:00"}


def _approval_action(runtime: object, waiting: object, turn: Mapping[str, object]) -> dict[str, object]:
    events = tuple(runtime.events_after(str(getattr(waiting, "turn_id"))))
    approval = next(event for event in reversed(events) if event.get("type") == "approval.required")
    return {"schema_version": "1.0.0", "action_id": f"action-{turn['operation_id']}", "turn_id": str(getattr(waiting, "turn_id")), "type": "approve", "target_event_id": approval["event_id"], "reason": "legacy Workbench classifier enhancement mapped to AI Turn approval", "actor": "user", "expected_sequence": int(getattr(waiting, "current_sequence")), "idempotency_key": f"approve-{turn['idempotency_key']}", "created_at": "2026-08-23T00:00:00+00:00"}
