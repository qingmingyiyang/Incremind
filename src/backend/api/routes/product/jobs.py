"""Jobs ownership for the product API."""
from __future__ import annotations

import asyncio, json, sqlite3
from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend.api.container import ApiContainerDep
from backend.api.job_runtime import build_rebuild_job_repository as _job_repository
from backend.api.sse import encode_sse_event

from core.job_runner import JOB_STATUSES, JobServiceError, JobStatusService
from core.job_runner.job_projection import load_effect_execution_projection

from . import http as product_http
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.get("/api/rebuild/jobs/{job_id}")
def job_detail(job_id: str, container: ApiContainerDep) -> JSONResponse:
    """Return the full job record for the given job_id.

    Returns 404 if the job is not found.
    """
    store, _settings = product_repositories._object_store(container.root_dir)
    repository = _job_repository(container.root_dir, store)
    service = JobStatusService(repository)
    job = service.get_job_detail(job_id)
    if job is None:
        return product_http._json_response(
            404,
            {"detail": f"job not found: {job_id}"},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    try:
        view = _job_status_view(repository=repository, job=job)
    except JobServiceError as error:
        return product_http._json_response(409, {"detail": str(error)}, {"Content-Type": "application/json", "Cache-Control": "no-store"})
    return product_http._json_response(
        200,
        view,
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


_JOB_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


_JOB_READ_STATUSES = frozenset((*JOB_STATUSES, "legacy_unknown"))


def _job_status_view(*, repository: object, job: Mapping[str, object]) -> dict[str, object]:
    view = dict(job)
    view["execution"] = {
        "schema_version": 1,
        "authority": "core_effect_log",
        "available": False,
        "nodes": [],
    }
    sqlite_repository = getattr(repository, "sqlite", None)
    database_path = getattr(sqlite_repository, "database_path", None)
    if database_path is not None:
        try:
            view["execution"] = load_effect_execution_projection(
                database_path, str(job.get("id") or job.get("job_id") or ""),
            )
        except (OSError, ValueError, sqlite3.Error):
            # The Effect tree is a recoverable read model.  Legacy/partially
            # upgraded stores must keep the base Job projection readable, and
            # no SQL or local path detail is exposed to the client.
            pass
    outputs = job.get("staged_outputs")
    child_refs = [
        output for output in outputs or ()
        if isinstance(output, Mapping) and output.get("kind") == "candidate_job"
    ]
    if not child_refs:
        return view
    parent_id = str(job.get("id") or job.get("job_id") or "")
    children: list[dict[str, object]] = []
    for ref in child_refs:
        child_id = ref.get("object_id")
        if not isinstance(child_id, str) or not child_id:
            raise JobServiceError(f"job {parent_id} has invalid candidate child reference")
        child = repository.get(child_id)
        if child is None:
            raise JobServiceError(f"job {parent_id} candidate child not found: {child_id}")
        if child.get("parent_job_id") != parent_id:
            raise JobServiceError(f"job {parent_id} candidate child identity mismatch: {child_id}")
        children.append(dict(child))
    statuses = {str(child.get("status", "")) for child in children}
    if "failed" in statuses:
        aggregate_status = "failed"
    elif "cancelled" in statuses:
        aggregate_status = "cancelled"
    elif "waiting_user" in statuses:
        aggregate_status = "waiting_user"
    elif statuses <= {"completed"}:
        aggregate_status = str(job.get("status", "completed"))
    elif "running" in statuses:
        aggregate_status = "running"
    else:
        aggregate_status = "pending"
    view["child_jobs"] = children
    view["aggregate_status"] = aggregate_status
    return view


async def _stream_job_progress(*, repository: object, job_id: str):
    """轮询 job 状态变化并推送 SSE 事件。

    每次发现 status 或 updated_at 变化时推送整个 job dict；
    到达终态后推送最终事件并关闭流。
    """
    service = JobStatusService(repository)
    last_signature: tuple[str, str, int, str, str] | None = None
    while True:
        job = service.get_job_detail(job_id)
        if job is None:
            yield encode_sse_event("not_found", {"detail": f"job not found: {job_id}"})
            break
        try:
            view = _job_status_view(repository=repository, job=job)
        except JobServiceError as error:
            yield encode_sse_event("job_invalid", {"detail": str(error)})
            break
        events = job.get("events")
        event_count = len(events) if isinstance(events, list) else 0
        child_signature = json.dumps(view.get("child_jobs", []), ensure_ascii=False, sort_keys=True)
        execution_signature = json.dumps(view.get("execution", {}), ensure_ascii=False, sort_keys=True)
        signature = (
            str(job.get("status", "")), str(job.get("updated_at", "")), event_count,
            child_signature, execution_signature,
        )
        if signature != last_signature:
            last_signature = signature
            yield encode_sse_event("job_updated", view)
        job_status = job.get("status")
        terminal_status = view.get("aggregate_status", job_status)
        if job_status == "legacy_unknown":
            legacy_status = view.get("legacy_status")
            yield encode_sse_event(
                "manual_resolution_required",
                {
                    "job_id": str(view.get("id") or view.get("job_id") or job_id),
                    "status": "legacy_unknown",
                    "legacy_status": legacy_status if isinstance(legacy_status, str) else None,
                    "reason": "legacy_execution_result_unknown",
                    "action": {
                        "kind": "review_history",
                        "mode": "read_only",
                        "enabled": True,
                    },
                },
            )
            break
        if terminal_status in _JOB_TERMINAL_STATUSES:
            break
        await asyncio.sleep(0.5)


@router.get("/api/rebuild/jobs/{job_id}/stream")
async def job_stream(job_id: str, container: ApiContainerDep):
    """SSE 端点：实时推送 job 状态变化。

    前端用 EventSource 订阅，收到 'job_updated' 事件更新 UI，
    收到终态（completed/failed/cancelled）后流自动关闭。
    """
    store, _settings = product_repositories._object_store(container.root_dir)
    repository = _job_repository(container.root_dir, store)
    # 先确认 job 存在，否则 404
    service = JobStatusService(repository)
    job = service.get_job_detail(job_id)
    if job is None:
        return product_http._json_response(
            404,
            {"detail": f"job not found: {job_id}"},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    try:
        _job_status_view(repository=repository, job=job)
    except JobServiceError as error:
        return product_http._json_response(409, {"detail": str(error)}, {"Content-Type": "application/json", "Cache-Control": "no-store"})
    return StreamingResponse(
        _stream_job_progress(repository=repository, job_id=job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/rebuild/jobs")
def job_list(request: Request, container: ApiContainerDep) -> JSONResponse:
    """List jobs with optional filters.

    Query params:
    - job_type: filter by job_type (e.g. workbench_auto_intake, capture)
    - status: filter by job status (must be in JOB_STATUSES)
    - source_id: filter by source_id
    - limit: clamp to [0, 100], default 50
    """
    store, _settings = product_repositories._object_store(container.root_dir)
    repository = _job_repository(container.root_dir, store)
    service = JobStatusService(repository)

    job_type = product_http._optional_query_str(request, "job_type")
    status = product_http._optional_query_str(request, "status")
    if status is not None and status not in _JOB_READ_STATUSES:
        return product_http._json_response(
            400,
            {"detail": f"invalid status: {status}; expected one of {sorted(_JOB_READ_STATUSES)}"},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    source_id = product_http._optional_query_str(request, "source_id")
    limit_str = request.query_params.get("limit", "50").strip()
    try:
        limit = int(limit_str)
    except ValueError:
        return product_http._json_response(
            400,
            {"detail": f"invalid limit: {limit_str}; expected integer"},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    if limit < 0:
        limit = 0
    elif limit > 100:
        limit = 100

    jobs = service.list_jobs(
        job_type=job_type,
        status=status,
        source_id=source_id,
        limit=limit,
    )
    return product_http._json_response(
        200,
        {
            "jobs": [dict(job) for job in jobs],
            "total": len(jobs),
            "filters": {
                "job_type": job_type,
                "status": status,
                "source_id": source_id,
                "limit": limit,
            },
        },
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )
