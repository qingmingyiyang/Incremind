from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.workbench_audio_intake_runtime import build_workbench_audio_intake_runtime
from core.product_core.workbench_source_intake_endpoint import ServeWorkbenchAudioSourceIntakeEndpoint


router = APIRouter(tags=["workbench-audio-intake"])


async def _json_body(request: Request) -> Mapping[str, object] | None:
    try:
        payload: Any = await request.json()
    except Exception:
        return None
    return payload if isinstance(payload, Mapping) else None


def _path_with_query(request: Request) -> str:
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


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


@router.post("/api/rebuild/workbench/audio-source-intake")
async def workbench_audio_source_intake(request: Request, container: ApiContainerDep) -> JSONResponse:
    store, settings = build_rebuild_object_store(container.root_dir)
    runtime = build_workbench_audio_intake_runtime(
        container.root_dir,
        store,
        namespace_id=settings.namespace_id,
        effect_runner=request.app.state.effect_runtime.runner,
    )
    response = ServeWorkbenchAudioSourceIntakeEndpoint().execute(
        method=request.method,
        path=_path_with_query(request),
        body=await _json_body(request),
        intake_flow=runtime.flow.execute,
    )
    return _json_response(response.status_code, response.body, response.headers)
