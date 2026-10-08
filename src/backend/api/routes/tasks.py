"""Stable task-reference read API over existing durable owners."""
from __future__ import annotations

from backend.security.device_identity import server_mode, server_authorized

import asyncio
import ipaddress
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from backend.api.bilibili_favorite_batch import BilibiliFavoriteBatchRepository
from backend.api.container import ApiContainerDep
from backend.api.job_runtime import build_rebuild_job_repository
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.task_reference_projection import (
    TaskReferenceError,
    list_task_references,
    task_reference_detail,
)
from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.product_core.workbench_content_transform_execution import read_workbench_transform_receipt
from core.storage_provider import SQLiteStructuredRecordStore


router = APIRouter(tags=["tasks"])
_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/api/rebuild/tasks")
async def list_tasks(
    request: Request,
    container: ApiContainerDep,
    project_id: str = Query(...),
    limit: int = Query(default=50),
    cursor: str | None = None,
    filter: str | None = Query(default=None),
) -> JSONResponse:
    if not _local_request(request):
        return _error(403, "task_query_local_only")
    try:
        root, repository = _repository(container)
        world_action_overview, world_action_status = _world_action_callbacks(request, container)
        transform_job_page, transform_job_reader, transform_receipt, document_reader, document_revision_reader, document_markdown_reader = _workbench_transform_callbacks(
            root, repository.object_store, repository.namespace_id, project_id,
        )
        result = await asyncio.to_thread(
            list_task_references,
            repository=repository,
            project_id=project_id,
            project_batch_projection=lambda payload: _batch_projection(root, repository.object_store, payload),
            object_store=repository.object_store,
            world_action_overview=world_action_overview,
            world_action_status=world_action_status,
            workbench_transform_job_page=transform_job_page,
            workbench_transform_job_reader=transform_job_reader,
            workbench_transform_receipt=transform_receipt,
            document_reader=document_reader,
            document_revision_reader=document_revision_reader,
            document_markdown_reader=document_markdown_reader,
            limit=limit,
            cursor=cursor,
            filter=filter,
        )
    except TaskReferenceError as error:
        return _error(400, "task_query_invalid")
    except (TypeError, ValueError):
        return _error(409, "task_query_unavailable")
    return JSONResponse({**result, "observed_at": _observed_at(result)}, headers=_NO_STORE)


@router.get("/api/rebuild/tasks/{task_ref}")
async def get_task(
    task_ref: str,
    request: Request,
    container: ApiContainerDep,
    project_id: str = Query(...),
) -> JSONResponse:
    if not _local_request(request):
        return _error(403, "task_query_local_only")
    try:
        root, repository = _repository(container)
        world_action_overview, world_action_status = _world_action_callbacks(request, container)
        _transform_job_page, transform_job_reader, transform_receipt, document_reader, document_revision_reader, document_markdown_reader = _workbench_transform_callbacks(
            root, repository.object_store, repository.namespace_id, project_id,
        )
        result = await asyncio.to_thread(
            task_reference_detail,
            repository=repository,
            task_ref=task_ref,
            project_id=project_id,
            project_batch_projection=lambda payload: _batch_projection(root, repository.object_store, payload),
            object_store=repository.object_store,
            world_action_overview=world_action_overview,
            world_action_status=world_action_status,
            workbench_transform_job_reader=transform_job_reader,
            workbench_transform_receipt=transform_receipt,
            document_reader=document_reader,
            document_revision_reader=document_revision_reader,
            document_markdown_reader=document_markdown_reader,
        )
    except TaskReferenceError:
        return _error(400, "task_reference_invalid")
    except (TypeError, ValueError):
        return _error(409, "task_query_unavailable")
    if result is None:
        return _error(404, "task_not_found")
    return JSONResponse({**_attach_workbench_reviews(result, root=root, project_id=project_id),
                         "observed_at": result.get("updated_at")}, headers=_NO_STORE)


def _attach_workbench_reviews(result: dict[str, object], *, root: Path, project_id: str) -> dict[str, object]:
    if result.get("kind") == "workbench_content_transform":
        detail = result.get("detail")
        if isinstance(detail, dict):
            source_ids = detail.get("source_ids")
            if isinstance(source_ids, list):
                records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "structured-records.sqlite3")
                review_items = []
                for source_id in source_ids:
                    if not isinstance(source_id, str):
                        continue
                    row = records.read("workspace_review_intents", f"review-{source_id}")
                    if row is None or row.payload.get("project_id") != project_id:
                        continue
                    review_items.append({
                        "source_id": source_id, "status": row.payload.get("state"),
                        "href": "#view=home&panel=review&project_id=" + quote(project_id, safe="")
                        + "&item_id=" + quote(row.object_id, safe=""),
                    })
                detail["review_items"] = review_items
                outputs = detail.get("outputs")
                if isinstance(outputs, list) and len(outputs) == len(source_ids):
                    pending = {item["source_id"]: item for item in review_items if item["status"] != "confirmed"}
                    for source_id, output in zip(source_ids, outputs):
                        if source_id in pending and isinstance(output, dict):
                            output.update({"kind": "review", "title": "待审核的整理结果",
                                           "href": pending[source_id]["href"]})
    return result


def _repository(container: object) -> tuple[Path, BilibiliFavoriteBatchRepository]:
    root = Path(getattr(container, "root_dir"))
    store, settings = build_rebuild_object_store(root)
    return root, BilibiliFavoriteBatchRepository(store, namespace_id=settings.namespace_id)


def _batch_projection(root: Path, store: object, payload: object) -> dict[str, object]:
    from backend.api.routes.bilibili_media_ingress import _favorite_batch_projection
    if not isinstance(payload, dict):
        raise ValueError("task owner invalid")
    return _favorite_batch_projection(root, store, payload)


def _world_action_callbacks(request: Request, container: object):
    """Compose the cold read port only when the durable Turn store exists."""
    turn_store = getattr(request.app.state, "ai_turn_effect_store", None)
    if turn_store is None:
        return None, None
    from backend.api.task_world_action_reader import TaskWorldActionReader

    reader = TaskWorldActionReader(root_dir=Path(getattr(container, "root_dir")), turn_store=turn_store)
    return reader.overview, reader.action_status


def _workbench_transform_callbacks(root: Path, store: object, namespace_id: str, project_id: str):
    """Compose only the existing Effect-v2 transform read models.

    The task projection receives no generic Job history and no document body;
    it can only inspect the fixed transform family plus read-back identities.
    """
    jobs = build_rebuild_job_repository(root, store)
    documents = AggregateRepositoryFactory(
        runtime_root=root, namespace_id=namespace_id, json_store=store,
    ).document_repository()
    return (
        lambda after, limit: jobs.list_effect_jobs_page(
            effect_kind="workbench_content_transform", contract_version="effect-v2",
            project_id=project_id,
            after=after, limit=limit,
        ),
        jobs.get,
        lambda job_id: read_workbench_transform_receipt(jobs.sqlite.database_path, job_id),
        documents.read,
        documents.revision,
        lambda document_id, revision: documents.markdown(document_id, revision=revision),
    )


def _observed_at(result: dict[str, object]) -> str | None:
    items = result.get("items")
    if isinstance(items, list) and items:
        value = items[0].get("updated_at") if isinstance(items[0], dict) else None
        return value if isinstance(value, str) else None
    return None


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code, headers=_NO_STORE)


def _local_request(request: Request) -> bool:
    if server_mode(request):
        return server_authorized(request)
    host = request.client.host if request.client is not None else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
