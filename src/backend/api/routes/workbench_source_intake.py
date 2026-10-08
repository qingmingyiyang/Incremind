from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.workbench_source_intake_runtime import (
    WorkbenchSourceIntakeServices,
    build_workbench_source_intake_services,
)
from core.product_core.workbench_source_intake_endpoint import (
    ServeWorkbenchBookmarkCollectionIntakeEndpoint,
    ServeWorkbenchFileSourceIntakeEndpoint,
    ServeWorkbenchImageSourceIntakeEndpoint,
    ServeWorkbenchLinkSourceIntakeEndpoint,
    ServeWorkbenchTextSourceIntakeEndpoint,
)


router = APIRouter(tags=["workbench-source-intake"])


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


def _services(container: ApiContainerDep) -> WorkbenchSourceIntakeServices:
    store, settings = build_rebuild_object_store(container.root_dir)
    return build_workbench_source_intake_services(
        container.root_dir,
        store,
        namespace_id=settings.namespace_id,
    )


@router.post("/api/rebuild/workbench/text-source-intake")
async def workbench_text_source_intake(request: Request, container: ApiContainerDep) -> JSONResponse:
    response = ServeWorkbenchTextSourceIntakeEndpoint().execute(
        method=request.method,
        path=_path_with_query(request),
        body=await _json_body(request),
        intake=_services(container).text.execute,
    )
    return _json_response(response.status_code, response.body, response.headers)


@router.post("/api/rebuild/workbench/link-source-intake")
async def workbench_link_source_intake(request: Request, container: ApiContainerDep) -> JSONResponse:
    response = ServeWorkbenchLinkSourceIntakeEndpoint().execute(
        method=request.method,
        path=_path_with_query(request),
        body=await _json_body(request),
        intake=_services(container).link.execute,
    )
    return _json_response(response.status_code, response.body, response.headers)


@router.post("/api/rebuild/workbench/bookmark-collection-intake")
async def workbench_bookmark_collection_intake(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    response = ServeWorkbenchBookmarkCollectionIntakeEndpoint().execute(
        method=request.method,
        path=_path_with_query(request),
        body=await _json_body(request),
        intake=_services(container).bookmark_collection.execute,
    )
    return _json_response(response.status_code, response.body, response.headers)


@router.post("/api/rebuild/workbench/file-source-intake")
async def workbench_file_source_intake(request: Request, container: ApiContainerDep) -> JSONResponse:
    response = ServeWorkbenchFileSourceIntakeEndpoint().execute(
        method=request.method,
        path=_path_with_query(request),
        body=await _json_body(request),
        intake=_services(container).file.execute,
    )
    return _json_response(response.status_code, response.body, response.headers)


@router.post("/api/rebuild/workbench/image-source-intake")
async def workbench_image_source_intake(request: Request, container: ApiContainerDep) -> JSONResponse:
    response = ServeWorkbenchImageSourceIntakeEndpoint().execute(
        method=request.method,
        path=_path_with_query(request),
        body=await _json_body(request),
        intake=_services(container).image.execute,
    )
    return _json_response(response.status_code, response.body, response.headers)
