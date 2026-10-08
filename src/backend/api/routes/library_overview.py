from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.library_overview_runtime import build_library_overview_reader
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.aggregate_repository_factory import AggregateRepositoryFactoryError
from core.product_core.library_activity_overview import (
    GetLibraryActivityOverview,
    serialize_library_activity_overview,
)
from core.product_core.library_overview import GetLibraryOverview
from core.product_core.library_overview_endpoint import ServeLibraryOverviewEndpoint


router = APIRouter(tags=["rebuild-library-overview"])


def _json_response(
    status_code: int,
    body: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    response_headers = dict(headers or {"Cache-Control": "no-store"})
    response_headers.pop("Content-Type", None)
    return JSONResponse(content=body, status_code=status_code, headers=response_headers)


def _path_with_query(request: Request) -> str:
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


@router.get("/api/rebuild/library/overview")
def library_overview(request: Request, container: ApiContainerDep) -> JSONResponse:
    store, settings = build_rebuild_object_store(container.root_dir)
    try:
        reader = build_library_overview_reader(container.root_dir, store, settings)
    except AggregateRepositoryFactoryError as error:
        return _json_response(
            409,
            {
                "detail": "library overview rejected",
                "reason": str(error),
                "actionable": True,
            },
        )
    response = ServeLibraryOverviewEndpoint().execute(
        method=request.method,
        path=_path_with_query(request),
        get_library_overview=GetLibraryOverview(
            reader,
            namespace_id=settings.namespace_id,
        ).execute,
    )
    return _json_response(response.status_code, response.body, response.headers)


@router.get("/api/rebuild/library/activity-overview")
def library_activity_overview(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    store, settings = build_rebuild_object_store(container.root_dir)
    year_text = request.query_params.get("year", "").strip()
    try:
        year = int(year_text) if year_text else None
    except ValueError:
        year = None
    try:
        result = GetLibraryActivityOverview(
            build_library_overview_reader(container.root_dir, store, settings),
            namespace_id=settings.namespace_id,
        ).execute(
            project_id=(request.query_params.get("project_id") or "").strip() or None,
            year=year,
        )
    except AggregateRepositoryFactoryError as error:
        return _json_response(
            409,
            {
                "detail": "library activity overview rejected",
                "reason": str(error),
                "actionable": True,
            },
        )
    return _json_response(200, serialize_library_activity_overview(result))
