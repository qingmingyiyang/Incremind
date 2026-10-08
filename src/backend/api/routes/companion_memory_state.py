from __future__ import annotations

from collections.abc import Mapping
import sqlite3
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.library_overview_runtime import build_library_overview_reader
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.aggregate_repository_factory import AggregateRepositoryFactoryError
from core.companion_core.memory_mood import infer_companion_memory_mood
from core.job_runner.job_projection import load_companion_execution_projection
from core.product_core.library_activity_overview import GetLibraryActivityOverview


router = APIRouter(tags=["companion-memory-state"])


def _json_response(status_code: int, body: Mapping[str, Any]) -> JSONResponse:
    return JSONResponse(
        content=body,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/rebuild/pet/mood")
def pet_mood(request: Request, container: ApiContainerDep) -> JSONResponse:
    """Return an anonymous activity projection for the Companion renderer."""

    store, settings = build_rebuild_object_store(container.root_dir)
    try:
        activity = GetLibraryActivityOverview(
            build_library_overview_reader(container.root_dir, store, settings),
            namespace_id=settings.namespace_id,
        ).execute(
            project_id=(request.query_params.get("project_id") or "").strip() or None,
        )
    except AggregateRepositoryFactoryError as error:
        return _json_response(
            409,
            {"detail": "pet mood rejected", "reason": str(error), "actionable": True},
        )

    recent_days = list(activity.recent_days)
    today_count = recent_days[-1].get("count", 0) if recent_days else 0
    recent_7d_count = sum(day.get("count", 0) for day in recent_days[-7:])
    pending_count = int(activity.counts.get("pending_memory_candidates", 0))
    published_count = int(activity.counts.get("published_memories", 0))
    today_memory_count = int(activity.counts.get("today_published_memories", 0))
    recent_7d_memory_count = int(activity.counts.get("recent_7d_published_memories", 0))

    try:
        execution = load_companion_execution_projection(
            container.root_dir / ".rebuild-data" / "jobs.sqlite3",
            now=int(time.time()),
        )
    except (OSError, ValueError, sqlite3.Error):
        # The pet must fail closed when the Effect projection is unavailable.
        execution = {"effect": "unavailable", "lease": "unavailable"}
    return _json_response(
        200,
        {
            "mood": infer_companion_memory_mood(
                today_activity_count=today_count,
                recent_7d_activity_count=recent_7d_count,
                pending_memory_candidate_count=pending_count,
            ),
            "today_activity_count": today_count,
            "recent_7d_activity_count": recent_7d_count,
            "execution": execution,
            "pending_memory_candidate_count": pending_count,
            "published_memory_count": published_count,
            "today_memory_count": today_memory_count,
            "recent_7d_memory_count": recent_7d_memory_count,
        },
    )
