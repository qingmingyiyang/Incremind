from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.external_agent_publication_change_startup import (
    dispatch_memory_invalidation_changes,
)
from core.product_core.library_bulk_action import (
    BulkLibraryItemAction,
    serialize_bulk_library_item_action_result,
)
from core.product_core.library_item_deletion import (
    DeleteLibraryItem,
    UndoLibraryItemDeletion,
    serialize_library_item_deletion_result,
)
from core.product_core.library_source_activity import (
    QuerySourceActivity,
    serialize_source_activity_result,
)
from core.product_core.library_source_edit import (
    UpdateLibrarySourceMetadata,
    serialize_library_source_edit_result,
)
from core.product_core.memory_lifecycle import (
    GovernedMemoryLifecycle,
    MemoryBatchItem,
    MemoryBatchPreview,
    MemoryLifecycleConflict,
    MemoryLifecycleError,
    MemoryLifecycleResult,
)


router = APIRouter(tags=["rebuild-library-lifecycle"])


def _json_response(status_code: int, body: Mapping[str, Any]) -> JSONResponse:
    return JSONResponse(
        content=body,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _memory_lifecycle_payload(result: MemoryLifecycleResult) -> dict[str, object]:
    return {
        "action": result.action, "layer": result.layer, "object_id": result.object_id,
        "previous_revision": result.previous_revision, "revision": result.revision,
        "status": result.status, "invalidation_id": result.invalidation_id,
        "invalidated_refs": list(result.invalidated_refs),
        "affected_consumers": list(result.affected_consumers),
    }


def _deliver_memory_invalidation(root_dir) -> None:
    # The invalidation record is durable. Core recovery owns publication
    # backfill, lease expiry, replay classification, and delivery dispatch.
    dispatch_memory_invalidation_changes(root_dir, limit=16)


def _lifecycle_error(error: MemoryLifecycleError) -> JSONResponse:
    return _json_response(
        409 if isinstance(error, MemoryLifecycleConflict) else 400,
        {"detail": str(error)},
    )


async def _json_body(request: Request) -> Mapping[str, object] | None:
    try:
        payload: Any = await request.json()
    except Exception:
        return None
    return payload if isinstance(payload, Mapping) else None


@router.post("/api/rebuild/memory/{layer}/{object_id}/supersede")
async def supersede_memory(
    layer: str, object_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    payload = await _json_body(request)
    if payload is None or not isinstance(payload.get("changes"), Mapping):
        return _json_response(400, {"detail": "changes object is required"})
    store, settings = build_rebuild_object_store(container.root_dir)
    now = datetime.now(timezone.utc).isoformat()
    try:
        result = GovernedMemoryLifecycle(store, namespace_id=settings.namespace_id).supersede(
            layer=layer, object_id=object_id, project_id=str(payload.get("project_id") or ""),
            expected_revision=_integer(payload, "expected_revision"),
            expected_storage_revision=_integer(payload, "expected_storage_revision"),
            changes=payload["changes"], reason=str(payload.get("reason") or ""),
            occurred_at=str(payload.get("occurred_at") or now), recorded_at=now,
            confirm=payload.get("confirm") is True,
        )
    except MemoryLifecycleError as error:
        return _lifecycle_error(error)
    _deliver_memory_invalidation(container.root_dir)
    return _json_response(200, _memory_lifecycle_payload(result))


@router.post("/api/rebuild/memory/{layer}/{object_id}/redact")
async def redact_memory(
    layer: str, object_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    payload = await _json_body(request)
    if payload is None:
        return _json_response(400, {"detail": "request body is required"})
    store, settings = build_rebuild_object_store(container.root_dir)
    now = datetime.now(timezone.utc).isoformat()
    try:
        result = GovernedMemoryLifecycle(store, namespace_id=settings.namespace_id).redact(
            layer=layer, object_id=object_id, project_id=str(payload.get("project_id") or ""),
            expected_revision=_integer(payload, "expected_revision"),
            expected_storage_revision=_integer(payload, "expected_storage_revision"),
            reason=str(payload.get("reason") or ""), mode=str(payload.get("mode") or ""),
            occurred_at=now, confirm=payload.get("confirm") is True,
            hard_confirmation=(str(payload["hard_confirmation"]) if payload.get("hard_confirmation") is not None else None),
        )
    except MemoryLifecycleError as error:
        return _lifecycle_error(error)
    _deliver_memory_invalidation(container.root_dir)
    return _json_response(200, _memory_lifecycle_payload(result))


@router.post("/api/rebuild/memory/{layer}/{object_id}/restore")
async def restore_memory(
    layer: str, object_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    payload = await _json_body(request)
    if payload is None:
        return _json_response(400, {"detail": "request body is required"})
    store, settings = build_rebuild_object_store(container.root_dir)
    now = datetime.now(timezone.utc).isoformat()
    try:
        result = GovernedMemoryLifecycle(store, namespace_id=settings.namespace_id).restore_soft_redaction(
            layer=layer, object_id=object_id, project_id=str(payload.get("project_id") or ""),
            expected_revision=_integer(payload, "expected_revision"),
            expected_storage_revision=_integer(payload, "expected_storage_revision"),
            occurred_at=now, confirm=payload.get("confirm") is True,
        )
    except MemoryLifecycleError as error:
        return _lifecycle_error(error)
    _deliver_memory_invalidation(container.root_dir)
    return _json_response(200, _memory_lifecycle_payload(result))


def _integer(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MemoryLifecycleError(f"{key} must be a positive integer")
    return value


@router.get("/api/rebuild/memory/lifecycle")
def list_memory_lifecycle_heads(
    container: ApiContainerDep, project_id: str,
) -> JSONResponse:
    if not project_id.strip():
        return _json_response(400, {"detail": "project_id is required"})
    store, _settings = build_rebuild_object_store(container.root_dir)
    items: list[dict[str, object]] = []
    for layer, collection in (
        ("atom", "memory_atoms"),
        ("scenario", "memory_scenarios"),
        ("series_memory", "memory_series_memory"),
    ):
        for record in store.list(collection):
            if record.get("project_id") != project_id:
                continue
            object_id = str(record.get("id") or "")
            revision = record.get("revision")
            if not object_id or not isinstance(revision, int) or isinstance(revision, bool):
                continue
            items.append({
                "layer": layer,
                "object_id": object_id,
                "revision": revision,
                "storage_revision": store.revision(collection, object_id),
                "status": str(record.get("lifecycle_status") or "active"),
                "label": str(
                    record.get("summary") or record.get("title")
                    or record.get("content") or object_id
                )[:160],
            })
    return _json_response(200, {
        "project_id": project_id,
        "items": sorted(items, key=lambda item: (str(item["layer"]), str(item["object_id"]))),
    })


@router.post("/api/rebuild/memory/lineage")
async def register_memory_lineage(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    payload = await _json_body(request)
    if payload is None:
        return _json_response(400, {"detail": "request body is required"})
    store, settings = build_rebuild_object_store(container.root_dir)
    try:
        lineage_id = GovernedMemoryLifecycle(
            store, namespace_id=settings.namespace_id,
        ).register_lineage(
            project_id=str(payload.get("project_id") or ""),
            memory_ref=str(payload.get("memory_ref") or ""),
            consumer_kind=str(payload.get("consumer_kind") or ""),
            consumer_ref=str(payload.get("consumer_ref") or ""),
            recorded_at=datetime.now(timezone.utc).isoformat(),
        )
    except MemoryLifecycleError as error:
        return _lifecycle_error(error)
    return _json_response(201, {"lineage_id": lineage_id, "status": "active"})


@router.post("/api/rebuild/memory/batches/soft-redact/preview")
async def preview_memory_batch_soft_redact(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    payload = await _json_body(request)
    raw_items = payload.get("items") if payload is not None else None
    if not isinstance(raw_items, list):
        return _json_response(400, {"detail": "items list is required"})
    store, settings = build_rebuild_object_store(container.root_dir)
    now = datetime.now(timezone.utc).isoformat()
    try:
        items = tuple(
            MemoryBatchItem(
                str(item.get("layer") or ""), str(item.get("object_id") or ""),
                _integer(item, "expected_revision"),
                _integer(item, "expected_storage_revision"),
            )
            for item in raw_items if isinstance(item, Mapping)
        )
        if len(items) != len(raw_items):
            raise MemoryLifecycleError("memory batch items are invalid")
        preview = GovernedMemoryLifecycle(
            store, namespace_id=settings.namespace_id,
        ).preview_batch_soft_redact(
            project_id=str(payload.get("project_id") or ""), items=items,
            reason=str(payload.get("reason") or ""), occurred_at=now,
        )
        record = _memory_batch_preview_payload(preview)
        preview_id = f"memory-batch-preview-{preview.preview_token[:20]}"
        existing = store.read("memory_lifecycle_batch_previews", preview_id)
        if existing is None:
            store.write("memory_lifecycle_batch_previews", preview_id, record, expected_revision=0)
        elif dict(existing) != record:
            raise MemoryLifecycleConflict("memory batch preview identity drifted")
    except MemoryLifecycleError as error:
        return _lifecycle_error(error)
    return _json_response(200, record)


@router.post("/api/rebuild/memory/batches/soft-redact/confirm")
async def confirm_memory_batch_soft_redact(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    payload = await _json_body(request)
    if payload is None:
        return _json_response(400, {"detail": "request body is required"})
    store, settings = build_rebuild_object_store(container.root_dir)
    try:
        preview = _load_memory_batch_preview(store, str(payload.get("preview_token") or ""))
        operation = GovernedMemoryLifecycle(
            store, namespace_id=settings.namespace_id,
        ).confirm_batch_soft_redact(
            preview=preview, expected_preview_token=str(payload.get("preview_token") or ""),
            confirm=payload.get("confirm") is True,
        )
    except MemoryLifecycleError as error:
        return _lifecycle_error(error)
    _deliver_memory_invalidation(container.root_dir)
    return _json_response(200, dict(operation))


@router.post("/api/rebuild/memory/batches/{operation_id}/resume")
async def resume_memory_batch_soft_redact(
    operation_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    payload = await _json_body(request) or {}
    store, settings = build_rebuild_object_store(container.root_dir)
    try:
        operation = GovernedMemoryLifecycle(
            store, namespace_id=settings.namespace_id,
        ).resume_batch_soft_redact(
            operation_id=operation_id, confirm=payload.get("confirm") is True,
        )
    except MemoryLifecycleError as error:
        return _lifecycle_error(error)
    _deliver_memory_invalidation(container.root_dir)
    return _json_response(200, dict(operation))


@router.post("/api/rebuild/memory/batches/{operation_id}/undo")
async def undo_memory_batch_soft_redact(
    operation_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    payload = await _json_body(request) or {}
    store, settings = build_rebuild_object_store(container.root_dir)
    try:
        operation = GovernedMemoryLifecycle(
            store, namespace_id=settings.namespace_id,
        ).undo_batch_soft_redact(
            operation_id=operation_id,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            confirm=payload.get("confirm") is True,
        )
    except MemoryLifecycleError as error:
        return _lifecycle_error(error)
    _deliver_memory_invalidation(container.root_dir)
    return _json_response(200, dict(operation))


def _memory_batch_preview_payload(preview: MemoryBatchPreview) -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "project_id": preview.project_id,
        "items": [
            {
                "layer": item.layer, "object_id": item.object_id,
                "expected_revision": item.expected_revision,
                "expected_storage_revision": item.expected_storage_revision,
            }
            for item in preview.items
        ],
        "reason": preview.reason, "occurred_at": preview.occurred_at,
        "preview_token": preview.preview_token,
    }


def _load_memory_batch_preview(store: object, token: str) -> MemoryBatchPreview:
    if len(token) != 64 or any(character not in "0123456789abcdef" for character in token):
        raise MemoryLifecycleError("memory batch preview token is invalid")
    record = store.read(
        "memory_lifecycle_batch_previews", f"memory-batch-preview-{token[:20]}",
    )
    if not isinstance(record, Mapping) or record.get("preview_token") != token:
        raise MemoryLifecycleError("memory batch preview was not found")
    raw_items = record.get("items")
    if not isinstance(raw_items, list):
        raise MemoryLifecycleConflict("memory batch preview drifted")
    try:
        items = tuple(
            MemoryBatchItem(
                str(item["layer"]), str(item["object_id"]),
                int(item["expected_revision"]), int(item["expected_storage_revision"]),
            )
            for item in raw_items if isinstance(item, Mapping)
        )
        preview = MemoryBatchPreview(
            str(record["project_id"]), items, str(record["reason"]),
            str(record["occurred_at"]), token,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise MemoryLifecycleConflict("memory batch preview drifted") from error
    if len(items) != len(raw_items) or _memory_batch_preview_payload(preview) != dict(record):
        raise MemoryLifecycleConflict("memory batch preview drifted")
    return preview


@router.delete("/api/rebuild/library/items/{item_id}")
def delete_library_item(
    item_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    item_type = request.query_params.get("item_type", "").strip().lower()
    if not item_type:
        return _json_response(400, {"detail": "item_type query parameter is required"})
    store, _settings = build_rebuild_object_store(container.root_dir)
    result = DeleteLibraryItem(store).execute(item_type=item_type, item_id=item_id)
    status_code = 200 if result.status == "deleted" else (
        404 if result.status == "not_found" else 400
    )
    return _json_response(status_code, serialize_library_item_deletion_result(result))


@router.post("/api/rebuild/library/items/{item_id}/undo-delete")
async def undo_delete_library_item(
    item_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    payload = await _json_body(request) or {}
    item_type = str(payload.get("item_type") or "").strip().lower()
    operation_id = str(payload.get("operation_id") or "").strip()
    expected_revision = payload.get("expected_revision")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
        return _json_response(400, {"detail": "expected_revision must be an integer"})
    store, _settings = build_rebuild_object_store(container.root_dir)
    result = UndoLibraryItemDeletion(store).execute(
        item_type=item_type,
        item_id=item_id,
        operation_id=operation_id,
        expected_revision=expected_revision,
    )
    status_code = 200 if result.status == "restored" else (
        404 if result.status == "not_found" else
        409 if result.status in {"conflict", "expired"} else 400
    )
    return _json_response(status_code, serialize_library_item_deletion_result(result))


@router.post("/api/rebuild/library/items/bulk-action")
async def bulk_library_item_action(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return _json_response(400, {"detail": "invalid JSON body"})
    if not isinstance(payload, dict):
        return _json_response(400, {"detail": "request body must be a JSON object"})

    action = str(payload.get("action") or "").strip().lower()
    raw_ids = payload.get("item_ids") or []
    if not isinstance(raw_ids, list):
        return _json_response(400, {"detail": "item_ids must be a list of strings"})
    item_ids = [str(item_id) for item_id in raw_ids if item_id]

    series_name = payload.get("series_name")
    project_id = payload.get("project_id")
    raw_tags = payload.get("tags") or []
    tags = [str(tag) for tag in raw_tags if tag] if isinstance(raw_tags, list) else []

    store, _settings = build_rebuild_object_store(container.root_dir)
    result = BulkLibraryItemAction(store).execute(
        action=action,
        item_ids=item_ids,
        series_name=series_name if isinstance(series_name, str) else None,
        project_id=project_id if isinstance(project_id, str) else None,
        tags=tags,
    )
    status_code = 200 if result.status in {"completed", "partial"} else (
        404 if result.status == "failed" else 400
    )
    return _json_response(status_code, serialize_bulk_library_item_action_result(result))


@router.put("/api/rebuild/library/sources/{source_id}/metadata")
async def update_library_source_metadata(
    source_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    payload = await _json_body(request) or {}
    expected_revision = payload.get("expected_revision")
    raw_tags = payload.get("tags") or []
    if (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or not isinstance(raw_tags, list)
    ):
        return _json_response(
            400,
            {"detail": "expected_revision integer and tags list are required"},
        )
    store, _settings = build_rebuild_object_store(container.root_dir)
    result = UpdateLibrarySourceMetadata(store).execute(
        source_id=source_id,
        expected_revision=expected_revision,
        title=str(payload.get("title") or ""),
        series_name=str(payload.get("series_name") or ""),
        tags=[str(tag) for tag in raw_tags],
    )
    status_code = 200 if result.status == "updated" else (
        404 if result.status == "not_found" else
        409 if result.status == "conflict" else 400
    )
    return _json_response(status_code, serialize_library_source_edit_result(result))


@router.get("/api/rebuild/library/sources/{source_id}/activity")
def library_source_activity(
    request: Request,
    source_id: str,
    container: ApiContainerDep,
) -> JSONResponse:
    limit_text = request.query_params.get("limit", "20")
    try:
        limit = max(1, min(100, int(limit_text)))
    except ValueError:
        limit = 20
    store, _settings = build_rebuild_object_store(container.root_dir)
    result = QuerySourceActivity(store).execute(source_id=source_id, limit=limit)
    status_code = 200 if result.status == "completed" else (
        404 if result.status == "not_found" else 400
    )
    return _json_response(status_code, serialize_source_activity_result(result))
