"""Inspirations ownership for the product API."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep

from core.product_core.daily_reminders import (
    GenerateDailyReminders,
    serialize_daily_reminder_digest,
)
from core.product_core.inspiration_system import (
    CreateInspirationCollision,
    GetInspirationOverview,
    serialize_inspiration_collision_result,
    serialize_inspiration_overview_result,
)

from . import http as product_http
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.post("/api/rebuild/inspirations/collision")
async def inspiration_collision(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    result = CreateInspirationCollision(store, namespace_id=settings.namespace_id).execute(
        query=product_http._optional_body_str(body, "query"),
        themes=product_http._optional_body_str_list(body, "themes"),
        project_id=product_http._optional_body_str(body, "project_id"),
        limit=product_http._optional_body_int(body, "limit") or 6,
    )
    return product_http._json_response(
        200,
        serialize_inspiration_collision_result(result),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.get("/api/rebuild/inspirations/overview")
def inspiration_overview(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    result = GetInspirationOverview(store).execute(project_id=product_http._optional_query_str(request, "project_id"))
    return product_http._json_response(
        200,
        serialize_inspiration_overview_result(result),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.get("/api/rebuild/reminders/today")
def daily_reminders_today(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    result = GenerateDailyReminders(store).execute(
        project_id=product_http._optional_query_str(request, "project_id"),
        limit=product_http._optional_query_int(request, "limit") or 8,
    )
    return product_http._json_response(
        200,
        serialize_daily_reminder_digest(result),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )
