from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.job_runtime import build_rebuild_job_repository
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.media_ingress_selection_authority import (
    LegacyMediaIngressDisabled,
    MediaIngressSelectionAuthority,
    MediaIngressSelectionError,
    media_ingress_selection_public,
)
from backend.api.workbench_auto_intake_runtime import build_workbench_auto_intake_runtime
from backend.api.task_reference_projection import task_ref_for_workbench_content_transform
from backend.api.workbench_content_transform_runtime import readmit_workbench_content_transform
from core.effect_log import InvalidEffectTransition
from core.job_runner.effect_commands import SQLiteJobEffectCommandAuthority
from core.product_core.model_route_runtime import ModelRouteRuntimeError
from core.product_core.processing_recipe import ProcessingRecipeError
from core.product_core.workbench_auto_intake import select_workbench_link_target
from core.storage_provider import SQLiteStructuredRecordStore


router = APIRouter(tags=["workbench-auto-intake"])
_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")


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


@router.post("/api/rebuild/workbench/auto-intake")
async def workbench_auto_intake(request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _json_body(request)
    try:
        if _single_bilibili_video_url(body) is not None:
            response = await asyncio.to_thread(_execute_legacy_auto_intake, request, container, body)
        else:
            response = await asyncio.to_thread(_execute_auto_intake, request, container, body)
    except LegacyMediaIngressDisabled as error:
        return _json_response(
            409,
            {
                "status": "legacy_ingress_disabled",
                "reason": "bilibili ingress is assigned to Media Hands",
                "selection": media_ingress_selection_public(error.selection),
                "next_step": "use_bilibili_media_ingress_resolve",
            },
            _no_store_headers(),
        )
    except MediaIngressSelectionError as error:
        return _json_response(
            409,
            {"status": "media_ingress_selection_unavailable", "reason": str(error)},
            _no_store_headers(),
        )
    except (ModelRouteRuntimeError, ProcessingRecipeError) as error:
        return _json_response(409, {"detail": str(error), "actionable": True}, _no_store_headers())
    return _json_response(response.status_code, response.body, response.headers)


@router.post("/api/rebuild/workbench/transforms/{job_id:path}/cancel")
async def cancel_workbench_content_transform(
    request: Request,
    container: ApiContainerDep,
    job_id: str,
) -> JSONResponse:
    body = await _json_body(request)
    try:
        command_id = _command_id(body)
    except (TypeError, ValueError):
        return _json_response(400, {"status": "rejected", "reason": "command_invalid"}, _no_store_headers())
    try:
        store, _settings = build_rebuild_object_store(getattr(container, "root_dir"))
        repository = build_rebuild_job_repository(Path(getattr(container, "root_dir")), store)
        job = repository.get(job_id)
        if job is None:
            return _json_response(404, {"status": "not_found"}, _no_store_headers())
        if (
            job.get("job_type") != "workbench_content_transform"
            or job.get("execution_version") != "effect-v2"
            or job.get("status") not in {"pending", "running"}
        ):
            return _json_response(409, {"status": "not_cancellable"}, _no_store_headers())
        result = SQLiteJobEffectCommandAuthority(
            repository.sqlite.database_path,
        ).request_cancellation(
            job_id=job_id,
            request_ref=f"api://workbench/transforms/{job_id}/cancel/{command_id}",
            requested_at=int(time.time()),
        )
    except KeyError:
        return _json_response(404, {"status": "not_found"}, _no_store_headers())
    except InvalidEffectTransition:
        return _json_response(409, {"status": "already_terminal"}, _no_store_headers())
    return _json_response(
        202,
        {
            "status": "cancel_requested",
            "job_id": job_id,
            "effect_operation_id": result.effect.operation_id,
        },
        _no_store_headers(),
    )


@router.post("/api/rebuild/workbench/transforms/{job_id:path}/retry")
async def retry_workbench_content_transform(
    request: Request,
    container: ApiContainerDep,
    job_id: str,
) -> JSONResponse:
    body = await _json_body(request)
    try:
        command_id = _command_id(body)
    except (TypeError, ValueError):
        return _json_response(400, {"status": "rejected", "reason": "command_invalid"}, _no_store_headers())
    try:
        root = Path(getattr(container, "root_dir"))
        store, _settings = build_rebuild_object_store(root)
        repository = build_rebuild_job_repository(root, store)
        previous_job = repository.get(job_id)
        if previous_job is None:
            return _json_response(404, {"status": "not_found"}, _no_store_headers())
        rebuilt = readmit_workbench_content_transform(
            database_path=repository.sqlite.database_path,
            runtime_root=root,
            object_store=store,
            previous_job=previous_job,
            command_id=command_id,
        )
    except (TypeError, ValueError) as error:
        return _json_response(
            409,
            {"status": "not_rebuildable", "reason": str(error)},
            _no_store_headers(),
        )
    return _json_response(
        202,
        {
            "status": "admitted",
            "job_id": str(rebuilt["id"]),
            "rebuilt_from_job_id": job_id,
        },
        _no_store_headers(),
    )


def _execute_auto_intake(
    request: Request,
    container: object,
    body: Mapping[str, object] | None,
):
    root = Path(getattr(container, "root_dir"))
    store, settings = build_rebuild_object_store(root)
    response = build_workbench_auto_intake_runtime(
        container,
        store,
        namespace_id=settings.namespace_id,
        application=request.app,
    ).execute(
        method=request.method,
        path=_path_with_query(request),
        body=body,
    )
    response_body = getattr(response, "body", None)
    if (
        not isinstance(getattr(response, "status_code", None), int)
        or not 200 <= response.status_code < 300
        or not isinstance(response_body, Mapping)
        or not isinstance(response_body.get("job_id"), str)
        or not response_body["job_id"]
    ):
        return response
    return _with_workbench_transform_task_reference(
        response,
        repository=build_rebuild_job_repository(root, store),
        object_store=store,
    )


def _with_workbench_transform_task_reference(
    response: object,
    *,
    repository: object,
    object_store: object,
):
    """Expose one stable task link only for the admitted Effect-v2 owner.

    The auto-intake response's ``job_id`` is otherwise only an implementation
    detail.  A task reference is added after reading the durable Job and its
    Source-owned project scope, mirroring the task reader's owner boundary.
    Ordinary intake jobs and legacy/media-shaped payloads cannot satisfy this
    check and retain their existing response contract unchanged.
    """
    response_body = getattr(response, "body", None)
    if not isinstance(response_body, Mapping):
        return response
    body = dict(response_body)
    job_id = body.get("job_id")
    get_job = getattr(repository, "get", None)
    read_source = getattr(object_store, "read", None)
    if not isinstance(job_id, str) or not job_id or not callable(get_job) or not callable(read_source):
        return response
    job = get_job(job_id)
    if (
        not isinstance(job, Mapping)
        or job.get("id") != job_id
        or job.get("job_type") != "workbench_content_transform"
        or job.get("execution_version") != "effect-v2"
    ):
        return response
    raw_items = job.get("transform_items")
    if not isinstance(raw_items, (list, tuple)) or not raw_items:
        return response
    project_ids: set[str] = set()
    for item in raw_items:
        if not isinstance(item, Mapping):
            return response
        source_id = item.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            return response
        source = read_source("sources", source_id)
        project_id = source.get("project_id") if isinstance(source, Mapping) else None
        if not isinstance(source, Mapping) or source.get("id") != source_id or not isinstance(project_id, str) or not project_id:
            return response
        project_ids.add(project_id)
    if len(project_ids) != 1:
        return response
    project_id = next(iter(project_ids))
    body["project_id"] = project_id
    body["task_ref"] = task_ref_for_workbench_content_transform(
        project_id=project_id,
        job_id=job_id,
    )
    return type(response)(
        status_code=response.status_code,
        body=body,
        headers=response.headers,
    )


def _execute_legacy_auto_intake(
    request: Request,
    container: object,
    body: Mapping[str, object] | None,
):
    with _media_ingress_selection(container).writer("legacy"):
        return _execute_auto_intake(request, container, body)


def _media_ingress_selection(container: object) -> MediaIngressSelectionAuthority:
    root = Path(getattr(container, "root_dir"))
    return MediaIngressSelectionAuthority(
        SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3")
    )


def _single_bilibili_video_url(body: Mapping[str, object] | None) -> str | None:
    if body is None:
        return None
    content = body.get("content")
    urls = body.get("urls")
    value = select_workbench_link_target(
        content=content if isinstance(content, str) else "",
        urls=urls if isinstance(urls, list) else (),
    )
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or not (host == "bilibili.com" or host.endswith(".bilibili.com"))
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not parsed.path.startswith("/video/")
        or parsed.fragment
    ):
        return None
    return value


def _command_id(body: Mapping[str, object] | None) -> str:
    if not isinstance(body, Mapping) or set(body) != {"command_id"}:
        raise ValueError("command body is invalid")
    value = body.get("command_id")
    if not isinstance(value, str) or _COMMAND_ID.fullmatch(value) is None:
        raise ValueError("command_id is invalid")
    return value
