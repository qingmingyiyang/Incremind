from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.ai_runtime import get_or_build_ai_runtime
from backend.api.bilibili_favorite_collection import (
    BilibiliFavoriteCollectionError,
    build_bilibili_favorite_collection_service,
)
from backend.api.bilibili_favorite_batch import (
    BilibiliFavoriteBatchError,
    BilibiliFavoriteBatchRepository,
    batch_public,
)
from backend.api.bilibili_favorite_batch_admission import (
    admit_bilibili_favorite_batch_command,
)
from backend.api.bilibili_media_postprocess_runtime import readmit_bilibili_postprocess
from backend.api.container import ApiContainerDep
from backend.api.job_runtime import build_rebuild_job_repository
from backend.api.media_hands_runtime import (
    MediaHandsRuntimeUnavailable,
    current_media_hands_runtime,
)
from backend.api.media_ingress_selection_authority import (
    HandsMediaIngressDisabled,
    MediaIngressRequestConflict,
    MediaIngressSelectionAuthority,
    MediaIngressSelectionError,
    media_ingress_selection_public,
)
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.storage_provider import SQLiteStructuredRecordStore
from core.source_processing import SourcePermissionAuthority


router = APIRouter(tags=["media-ingress"])
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")
_PROJECT_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_FAVORITE_BATCH_CHUNK = 10


@router.post("/api/rebuild/media-ingress/bilibili/favorites/resolve")
async def resolve_bilibili_favorite_collection(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await _json_body(request)
    if not isinstance(body, Mapping) or set(body) != {
        "collection_request_id", "project_id", "url"
    }:
        return _response(400, {"status": "rejected", "reason": "collection_body_invalid"})
    try:
        collection_request_id = _identifier(
            body.get("collection_request_id"), "collection_request_id"
        )
        project_id = _project_identifier(body.get("project_id"))
        store, settings = build_rebuild_object_store(Path(getattr(container, "root_dir")))
        service = build_bilibili_favorite_collection_service(
            store, namespace_id=settings.namespace_id
        )
        snapshot = await asyncio.to_thread(
            service.resolve,
            source_url=body.get("url"),
            project_id=project_id,
            snapshot_id=collection_request_id,
            resolved_at=_utc_now(),
        )
    except BilibiliFavoriteCollectionError as error:
        if error.code == "private_favorite_requires_login":
            return _response(403, {
                "status": "credential_required",
                "reason": error.code,
                "credential_boundary": "controlled_bilibili_credentials_not_configured",
            })
        status_code = 400 if error.code.endswith("_invalid") else 409
        return _response(status_code, {"status": "unavailable", "reason": error.code})
    payload = snapshot.payload
    return _response(200, {
        "status": "snapshot_ready",
        "replayed": snapshot.replayed,
        "collection_request_id": collection_request_id,
        "project_id": project_id,
        "snapshot_ref": snapshot.public_ref,
        "snapshot_revision": snapshot.revision,
        "favorite_id": payload["favorite_id"],
        "title": payload["title"],
        "owner": payload["owner"],
        "page_count": payload["page_count"],
        "raw_item_count": payload["raw_item_count"],
        "video_item_count": payload["video_item_count"],
        "skipped_counts": payload["skipped_counts"],
        "items": payload["items"],
    })


@router.post("/api/rebuild/media-ingress/bilibili/favorites/admit")
async def admit_bilibili_favorite_collection(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await _json_body(request)
    if not isinstance(body, Mapping) or set(body) != {
        "batch_request_id", "project_id", "snapshot_ref", "snapshot_revision", "confirm"
    } or body.get("confirm") is not True:
        return _response(400, {"status": "rejected", "reason": "batch_body_invalid"})
    try:
        batch_id = _identifier(body.get("batch_request_id"), "batch_request_id")
        project_id = _project_identifier(body.get("project_id"))
        if body.get("snapshot_revision") != "r1":
            raise ValueError("snapshot_revision_invalid")
        root = Path(getattr(container, "root_dir"))
        store, settings = build_rebuild_object_store(root)
        snapshot_repository = build_bilibili_favorite_collection_service(
            store, namespace_id=settings.namespace_id
        ).snapshots
        snapshot = snapshot_repository.resolve_ref(
            project_id=project_id, snapshot_ref=body.get("snapshot_ref")
        )
        repository = BilibiliFavoriteBatchRepository(
            store, namespace_id=settings.namespace_id
        )
        batch = repository.create(
            batch_id=batch_id,
            project_id=project_id,
            snapshot_ref=snapshot.public_ref,
            snapshot_revision=snapshot.revision,
            items=list(snapshot.payload["items"]),
            created_at=_utc_now(),
        )
        accepted, replayed = await asyncio.to_thread(
            admit_bilibili_favorite_batch_command,
            application=request.app,
            runtime_root=root,
            repository=repository,
            batch_payload=batch.payload,
            command_id=batch_id,
            kind="admit",
            retry_failed=False,
        )
    except BilibiliFavoriteCollectionError as error:
        return _response(409, {"status": "unavailable", "reason": error.code})
    except (BilibiliFavoriteBatchError, MediaIngressSelectionError, MediaHandsRuntimeUnavailable, TypeError, ValueError) as error:
        return _response(409, {"status": "unavailable", "reason": str(error)})
    projection = batch_public(accepted)
    return _response(200 if replayed else 202, {
        "status": "batch_admission_queued",
        "replayed": replayed,
        **projection,
    })


@router.post("/api/rebuild/media-ingress/bilibili/favorites/batches/{batch_id}/continue")
async def continue_bilibili_favorite_batch(
    batch_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await _json_body(request)
    if not isinstance(body, Mapping) or set(body) != {"command_id"}:
        return _response(400, {"status": "rejected", "reason": "batch_continue_body_invalid"})
    try:
        command_id = _identifier(body.get("command_id"), "command_id")
        root = Path(getattr(container, "root_dir"))
        store, settings = build_rebuild_object_store(root)
        repository = BilibiliFavoriteBatchRepository(store, namespace_id=settings.namespace_id)
        batch = repository.get(_identifier(batch_id, "batch_id"))
        if batch is None:
            return _response(404, {"status": "not_found"})
        accepted, replayed = await asyncio.to_thread(
            admit_bilibili_favorite_batch_command,
            application=request.app,
            runtime_root=root,
            repository=repository,
            batch_payload=batch.payload,
            command_id=command_id,
            kind="continue",
            retry_failed=False,
        )
    except (BilibiliFavoriteBatchError, MediaIngressSelectionError, MediaHandsRuntimeUnavailable, TypeError, ValueError) as error:
        return _response(409, {"status": "unavailable", "reason": str(error)})
    return _response(200 if replayed else 202, {
        "status": "batch_admission_queued", "replayed": replayed,
        **batch_public(accepted),
    })


@router.post("/api/rebuild/media-ingress/bilibili/favorites/batches/{batch_id}/retry-failed")
async def retry_failed_bilibili_favorite_batch(
    batch_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await _json_body(request)
    if not isinstance(body, Mapping) or set(body) != {"command_id", "confirm"} or body.get("confirm") is not True:
        return _response(400, {"status": "rejected", "reason": "batch_retry_body_invalid"})
    try:
        command_id = _identifier(body.get("command_id"), "command_id")
        root = Path(getattr(container, "root_dir"))
        store, settings = build_rebuild_object_store(root)
        repository = BilibiliFavoriteBatchRepository(store, namespace_id=settings.namespace_id)
        batch = repository.get(_identifier(batch_id, "batch_id"))
        if batch is None:
            return _response(404, {"status": "not_found"})
        accepted, replayed = await asyncio.to_thread(
            admit_bilibili_favorite_batch_command,
            application=request.app,
            runtime_root=root,
            repository=repository,
            batch_payload=batch.payload,
            command_id=command_id,
            kind="retry_failed",
            retry_failed=True,
        )
    except (BilibiliFavoriteBatchError, MediaIngressSelectionError, MediaHandsRuntimeUnavailable, TypeError, ValueError) as error:
        return _response(409, {"status": "unavailable", "reason": str(error)})
    return _response(200 if replayed else 202, {
        "status": "batch_admission_queued", "replayed": replayed,
        **batch_public(accepted),
    })


@router.get("/api/rebuild/media-ingress/bilibili/favorites/batches/{batch_id}")
async def get_bilibili_favorite_batch(
    batch_id: str, container: ApiContainerDep,
) -> JSONResponse:
    try:
        root = Path(getattr(container, "root_dir"))
        store, settings = build_rebuild_object_store(root)
        batch = BilibiliFavoriteBatchRepository(
            store, namespace_id=settings.namespace_id
        ).get(_identifier(batch_id, "batch_id"))
        if batch is None:
            return _response(404, {"status": "not_found"})
        projection = _favorite_batch_projection(root, store, batch.payload)
    except (BilibiliFavoriteBatchError, TypeError, ValueError) as error:
        return _response(409, {"status": "unavailable", "reason": str(error)})
    return _response(200, projection)


@router.get("/api/rebuild/media-ingress/bilibili/favorites/batches")
async def list_bilibili_favorite_batches(container: ApiContainerDep, project_id: str | None = None) -> JSONResponse:
    """Durable discovery endpoint used after a renderer or sidecar restart."""
    try:
        root = Path(getattr(container, "root_dir"))
        store, settings = build_rebuild_object_store(root)
        repository = BilibiliFavoriteBatchRepository(store, namespace_id=settings.namespace_id)
        if project_id is not None:
            project_id = _project_identifier(project_id)
        batches = [_favorite_batch_projection(root, store, item.payload) for item in repository.list(project_id=project_id)]
    except (BilibiliFavoriteBatchError, TypeError, ValueError) as error:
        return _response(409, {"status": "unavailable", "reason": str(error)})
    return _response(200, {"status": "ok", "batches": batches})


@router.post("/api/rebuild/media-ingress/resolve")
@router.post("/api/rebuild/media-ingress/bilibili/resolve")
async def resolve_bilibili_ingress(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await _json_body(request)
    if not isinstance(body, Mapping) or set(body) != {
        "ingress_request_id", "project_id", "url"
    }:
        return _response(400, {"status": "rejected", "reason": "ingress_body_invalid"})
    try:
        ingress_request_id = _identifier(body.get("ingress_request_id"), "ingress_request_id")
        project_id = _project_identifier(body.get("project_id"))
        expected_platform = _expected_platform(request)
        url = _media_url(body.get("url"), expected_platform=expected_platform)
        selection, resolved = await asyncio.to_thread(
            _resolve_with_selection,
            request,
            container,
            ingress_request_id,
            project_id,
            url,
        )
    except HandsMediaIngressDisabled as error:
        return _response(409, {
            "status": "hands_ingress_disabled",
            "reason": "media ingress is assigned to legacy",
            "selection": media_ingress_selection_public(error.selection),
        })
    except MediaIngressRequestConflict as error:
        return _response(409, {"status": "request_conflict", "reason": str(error)})
    except (MediaIngressSelectionError, MediaHandsRuntimeUnavailable) as error:
        return _response(409, {"status": "unavailable", "reason": str(error)})
    except (TypeError, ValueError) as error:
        return _response(400, {"status": "rejected", "reason": str(error)})
    if (
        resolved.manifest is None
        or resolved.manifest.platform not in {"bilibili", "xiaohongshu"}
        or (expected_platform is not None and resolved.manifest.platform != expected_platform)
        or resolved.manifest_ref is None
        or resolved.manifest_revision is None
    ):
        return _response(422, {
            "status": "unresolved",
            "reason": resolved.terminal_reason or "media_manifest_unavailable",
            "selection": media_ingress_selection_public(selection),
        })
    permission_ready = resolved.permission_snapshot is not None
    return _response(200, {
        "status": "ready_to_admit" if permission_ready else "permission_required",
        "ingress_request_id": ingress_request_id,
        "project_id": project_id,
        "source_id": resolved.manifest.source_id,
        "platform": resolved.manifest.platform,
        "manifest_ref": resolved.manifest_ref,
        "manifest_revision": resolved.manifest_revision,
        "content_kind": resolved.manifest.content_kind,
        "permission": "granted" if permission_ready else "unknown",
        "permission_grant_endpoint": f"/api/ai/projects/{project_id}/source-permissions/grant",
        "selection": media_ingress_selection_public(selection),
    })


@router.post("/api/rebuild/media-ingress/admit")
@router.post("/api/rebuild/media-ingress/bilibili/admit")
async def admit_bilibili_ingress(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await _json_body(request)
    if not isinstance(body, Mapping) or set(body) != {
        "ingress_request_id", "project_id", "manifest_ref"
    }:
        return _response(400, {"status": "rejected", "reason": "ingress_body_invalid"})
    try:
        ingress_request_id = _identifier(body.get("ingress_request_id"), "ingress_request_id")
        project_id = _project_identifier(body.get("project_id"))
        manifest_ref = body.get("manifest_ref")
        if not isinstance(manifest_ref, str) or not manifest_ref.startswith("crp://"):
            raise ValueError("manifest_ref_invalid")
        expected_platform = _expected_platform(request)
        selection, runtime, resolved, admission = await asyncio.to_thread(
            _admit_with_selection,
            request,
            container,
            ingress_request_id,
            project_id,
            manifest_ref,
            expected_platform,
        )
        if (
            resolved.manifest is None
            or resolved.manifest.platform not in {"bilibili", "xiaohongshu"}
            or (expected_platform is not None and resolved.manifest.platform != expected_platform)
            or resolved.manifest_ref is None
            or resolved.manifest_revision is None
        ):
            raise ValueError("media_manifest_unavailable")
        if resolved.permission_snapshot is None:
            return _response(409, {
                "status": "permission_required",
                "reason": "explicit_source_permission_required",
                "manifest_ref": resolved.manifest_ref,
                "permission_grant_endpoint": (
                    f"/api/ai/projects/{project_id}/source-permissions/grant"
                ),
                "selection": media_ingress_selection_public(selection),
            })
    except HandsMediaIngressDisabled as error:
        return _response(409, {
            "status": "hands_ingress_disabled",
            "reason": "media ingress is assigned to legacy",
            "selection": media_ingress_selection_public(error.selection),
        })
    except MediaIngressRequestConflict as error:
        return _response(409, {"status": "request_conflict", "reason": str(error)})
    except (MediaIngressSelectionError, MediaHandsRuntimeUnavailable) as error:
        return _response(409, {"status": "unavailable", "reason": str(error)})
    except (TypeError, ValueError) as error:
        return _response(400, {"status": "rejected", "reason": str(error)})
    job_id = str(admission.record.payload["id"])
    return _response(200 if admission.replayed else 202, {
        "status": "admitted",
        "replayed": admission.replayed,
        "ingress_request_id": ingress_request_id,
        "manifest_ref": resolved.manifest_ref,
        "manifest_revision": resolved.manifest_revision,
        "platform": resolved.manifest.platform,
        "job_id": job_id,
        "job_ref": f"crp://{runtime.namespace_id}/jobs/{job_id.replace(':', '/')}",
        "selection": media_ingress_selection_public(selection),
    })


@router.post("/api/rebuild/media-ingress/jobs/{job_id:path}/requeue")
async def requeue_legacy_media_ingress(
    request: Request,
    container: ApiContainerDep,
    job_id: str,
) -> JSONResponse:
    """Create a fresh Media Hands v2 Job without mutating legacy history."""

    body = await _json_body(request)
    try:
        if not isinstance(body, Mapping) or set(body) != {"command_id"}:
            raise ValueError("command_body_invalid")
        command_id = _identifier(body.get("command_id"), "command_id")
        root = Path(getattr(container, "root_dir"))
        store, _settings = build_rebuild_object_store(root)
        previous_job = build_rebuild_job_repository(root, store).get(job_id)
        if previous_job is None:
            return _response(404, {"status": "not_found"})
        if (
            previous_job.get("job_type") != "media_hands"
            or previous_job.get("execution_version") != "legacy-v1-readonly"
            or previous_job.get("status") != "legacy_unknown"
        ):
            return _response(409, {
                "status": "not_requeueable",
                "reason": "only frozen legacy Media Hands unknown Jobs can be requeued",
            })
        media = previous_job.get("media_hands")
        manifest = media.get("manifest") if isinstance(media, Mapping) else None
        permission = media.get("permission_snapshot") if isinstance(media, Mapping) else None
        manifest_ref = manifest.get("ref") if isinstance(manifest, Mapping) else None
        project_id = permission.get("project_id") if isinstance(permission, Mapping) else None
        if not isinstance(manifest_ref, str) or not manifest_ref.startswith("crp://"):
            raise ValueError("legacy_manifest_ref_invalid")
        project_id = _project_identifier(project_id)
        selection, runtime, resolved, admission = await asyncio.to_thread(
            _admit_with_selection,
            request,
            container,
            command_id,
            project_id,
            manifest_ref,
            "bilibili",
        )
        if admission is None:
            return _response(409, {
                "status": "permission_required",
                "reason": "explicit_source_permission_required",
                "manifest_ref": resolved.manifest_ref,
                "permission_grant_endpoint": (
                    f"/api/ai/projects/{project_id}/source-permissions/grant"
                ),
                "selection": media_ingress_selection_public(selection),
            })
    except HandsMediaIngressDisabled as error:
        return _response(409, {
            "status": "hands_ingress_disabled",
            "reason": "media ingress is assigned to legacy",
            "selection": media_ingress_selection_public(error.selection),
        })
    except MediaIngressRequestConflict as error:
        return _response(409, {"status": "request_conflict", "reason": str(error)})
    except (MediaIngressSelectionError, MediaHandsRuntimeUnavailable) as error:
        return _response(409, {"status": "unavailable", "reason": str(error)})
    except (TypeError, ValueError) as error:
        return _response(400, {"status": "rejected", "reason": str(error)})
    new_job_id = str(admission.record.payload["id"])
    return _response(200 if admission.replayed else 202, {
        "status": "admitted",
        "replayed": admission.replayed,
        "job_id": new_job_id,
        "requeued_from_job_id": job_id,
        "job_ref": f"crp://{runtime.namespace_id}/jobs/{new_job_id.replace(':', '/')}",
        "selection": media_ingress_selection_public(selection),
    })


@router.post("/api/rebuild/media-ingress/postprocess/{job_id:path}/retry")
async def retry_bilibili_postprocess(
    request: Request,
    container: ApiContainerDep,
    job_id: str,
) -> JSONResponse:
    """Create a fresh local post-process child from an immutable failed child."""

    body = await _json_body(request)
    try:
        if not isinstance(body, Mapping) or set(body) != {"command_id"}:
            raise ValueError("command_body_invalid")
        command_id = _identifier(body.get("command_id"), "command_id")
        root = Path(getattr(container, "root_dir"))
        store, settings = build_rebuild_object_store(root)
        repository = build_rebuild_job_repository(root, store)
        previous_job = repository.get(job_id)
        if previous_job is None:
            return _response(404, {"status": "not_found"})
        rebuilt = await asyncio.to_thread(
            readmit_bilibili_postprocess,
            database_path=repository.sqlite.database_path,
            runtime_root=root,
            object_store=store,
            namespace_id=settings.namespace_id,
            previous_job=previous_job,
            command_id=command_id,
        )
    except (TypeError, ValueError) as error:
        return _response(409, {
            "status": "not_rebuildable",
            "reason": str(error),
        })
    return _response(202, {
        "status": "admitted",
        "job_id": str(rebuilt["id"]),
        "rebuilt_from_job_id": job_id,
    })


def _selection(container: object) -> MediaIngressSelectionAuthority:
    root = Path(getattr(container, "root_dir"))
    return MediaIngressSelectionAuthority(
        SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3")
    )


def _runtime(request: Request, container: object):
    runtime = current_media_hands_runtime(request.app)
    if runtime is None:
        # This route is the explicit Media Hands execution boundary.  Compose
        # the governed runtime and its sole Job lifecycle before the AI runtime
        # freezes the analyze_source capability snapshot.
        from backend.api.media_hands_composition import compose_application_media_hands

        compose_application_media_hands(request.app, container)
    get_or_build_ai_runtime(request, container)
    runtime = current_media_hands_runtime(request.app)
    if runtime is None or not runtime.readiness().ready:
        raise MediaHandsRuntimeUnavailable("media_hands_unavailable")
    return runtime


def _resolve_with_selection(
    request: Request,
    container: object,
    ingress_request_id: str,
    project_id: str,
    url: str,
):
    authority = _selection(container)
    with authority.writer("hands") as selection:
        authority.bind_request(
            operation="resolve",
            request_id=ingress_request_id,
            project_id=project_id,
            input_ref=url,
            selection=selection,
        )
        runtime = _runtime(request, container)
        resolved = runtime.resolver.resolve(
            {"input": {"kind": "text", "text": url, "source_ref": None}},
            {"kind": "project", "project_id": project_id, "series_id": None},
        )
        return selection, resolved


def _admit_with_selection(
    request: Request,
    container: object,
    ingress_request_id: str,
    project_id: str,
    manifest_ref: str,
    expected_platform: str | None = None,
):
    authority = _selection(container)
    with authority.writer("hands") as selection:
        authority.bind_request(
            operation="admit",
            request_id=ingress_request_id,
            project_id=project_id,
            input_ref=manifest_ref,
            selection=selection,
        )
        runtime = _runtime(request, container)
        resolved = runtime.resolver.resolve(
            {"input": {"kind": "source_ref", "text": None, "source_ref": manifest_ref}},
            {"kind": "project", "project_id": project_id, "series_id": None},
        )
        if (
            resolved.manifest is None
            or resolved.manifest.platform not in {"bilibili", "xiaohongshu"}
            or (expected_platform is not None and resolved.manifest.platform != expected_platform)
            or resolved.manifest_ref is None
            or resolved.manifest_revision is None
        ):
            raise ValueError("media_manifest_unavailable")
        if resolved.permission_snapshot is None:
            return selection, runtime, resolved, None
        admission = runtime.provision(
            manifest=resolved.manifest,
            manifest_ref=resolved.manifest_ref,
            manifest_revision=resolved.manifest_revision,
            operation="analyze_source",
            idempotency_key=f"{resolved.manifest.platform}-ingress-{ingress_request_id}",
            created_at=_utc_now(),
            permission_snapshot=resolved.permission_snapshot,
        )
        return selection, runtime, resolved, admission


def _process_favorite_batch(
    request: Request,
    container: object,
    store: object,
    namespace_id: str,
    repository: BilibiliFavoriteBatchRepository,
    payload: Mapping[str, object],
    retry_failed: bool,
) -> dict[str, object]:
    working = dict(payload)
    items = [dict(item) for item in payload["items"]]
    if retry_failed:
        for item in items:
            if item["state"] == "failed":
                item["state"] = "pending"
                item["error"] = ""
    targets = [item for item in items if item["state"] == "pending"][:_FAVORITE_BATCH_CHUNK]
    if not targets:
        return batch_public(dict(working, items=items))

    working["status"] = "running"
    for item in targets:
        item["state"] = "processing"
        item["attempts"] = int(item["attempts"]) + 1
        ordinal = int(item["ordinal"])
        try:
            resolve_request_id = _favorite_operation_id(
                str(working["batch_id"]), ordinal, "resolve"
            )
            _selection_value, resolved = _resolve_with_selection(
                request,
                container,
                resolve_request_id,
                str(working["project_id"]),
                str(item["url"]),
            )
            if (
                resolved.manifest is None
                or resolved.manifest.platform != "bilibili"
                or resolved.manifest_ref is None
                or resolved.manifest_revision is None
            ):
                raise ValueError("media_manifest_unavailable")
            _ensure_favorite_source_permission(
                store=store,
                namespace_id=namespace_id,
                project_id=str(working["project_id"]),
                batch_id=str(working["batch_id"]),
                ordinal=ordinal,
                resolved=resolved,
            )
            _selection_value, _runtime_value, admitted_resolved, admission = _admit_with_selection(
                request,
                container,
                _favorite_operation_id(str(working["batch_id"]), ordinal, "admit"),
                str(working["project_id"]),
                str(resolved.manifest_ref),
                "bilibili",
            )
            if admission is None:
                raise ValueError("explicit_source_permission_required")
            item["state"] = "admitted"
            item["manifest_ref"] = str(admitted_resolved.manifest_ref)
            item["job_id"] = str(admission.record.payload["id"])
            item["error"] = ""
        except Exception as error:  # one source must not roll back successful siblings
            item["state"] = "failed"
            item["error"] = _favorite_item_error(error)

    pending = any(item["state"] == "pending" for item in items)
    failed = any(item["state"] == "failed" for item in items)
    working["items"] = items
    working["status"] = (
        "running" if pending else "partial_failure" if failed else "completed"
    )
    working["updated_at"] = _utc_now()
    saved = repository.save(working)
    return batch_public(saved.payload)


def _favorite_batch_projection(
    root: Path, store: object, payload: Mapping[str, object],
) -> dict[str, object]:
    """Join durable child Jobs as a read-only UI projection.

    A child output is exposed only after its referenced object can be read
    back from the owning store.  Missing evidence remains ``unknown`` and is
    never treated as a reason to re-admit the external media operation.
    """
    projection = batch_public(payload)
    jobs = build_rebuild_job_repository(root, store)
    statuses: dict[str, str] = {}
    child_outputs: list[dict[str, object]] = []
    for item in projection["items"]:
        if not isinstance(item, Mapping):
            continue
        job_id = item.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            continue
        job = jobs.get(job_id)
        status = job.get("status") if isinstance(job, Mapping) and isinstance(job.get("status"), str) else "missing"
        statuses[job_id] = status
        outputs = _read_back_favorite_outputs(store, job)
        child_outputs.append({
            "ordinal": item.get("ordinal"), "job_id": job_id, "status": status,
            "outputs": outputs,
            "openable": bool(outputs) and all(output["read_back"] for output in outputs),
        })
    projection["job_statuses"] = statuses
    projection["child_outputs"] = child_outputs
    if projection["total"] == 0:
        # A resolved empty collection is a meaningful, terminal discovery
        # result, but it is never a 0/0 successful media delivery.
        projection["processing_status"] = "empty"
        return projection
    terminal = {"completed", "failed", "cancelled", "waiting_user", "legacy_unknown", "missing"}
    values = set(statuses.values())
    admitted_without_job = any(
        item.get("state") == "admitted" and not item.get("job_id")
        for item in projection["items"] if isinstance(item, Mapping)
    )
    if admitted_without_job:
        projection["processing_status"] = "unknown"
    elif not values:
        projection["processing_status"] = "not_started"
    elif any(value not in terminal for value in values):
        projection["processing_status"] = "running"
    elif any(value in {"failed", "cancelled", "legacy_unknown", "missing"} for value in values):
        projection["processing_status"] = "partial_failure"
    elif any(value == "waiting_user" for value in values):
        projection["processing_status"] = "waiting_user"
    elif any(not child["openable"] for child in child_outputs):
        projection["processing_status"] = "unknown"
    else:
        projection["processing_status"] = "complete"
    return projection


def _read_back_favorite_outputs(store: object, job: object) -> list[dict[str, object]]:
    if not isinstance(job, Mapping):
        return []
    published = job.get("published_outputs")
    if not isinstance(published, list):
        return []
    results: list[dict[str, object]] = []
    for item in published:
        if not isinstance(item, Mapping):
            continue
        object_id = item.get("object_id")
        kind = item.get("kind")
        if not isinstance(object_id, str) or not object_id or not isinstance(kind, str):
            continue
        collection = "memory_candidates" if kind == "memory_candidate" else "media_processing_outputs"
        try:
            record = store.read(collection, object_id)
        except (AttributeError, TypeError, ValueError):
            record = None
        read_back = isinstance(record, Mapping)
        results.append({
            "kind": kind, "object_id": object_id,
            "status": record.get("status") if read_back and isinstance(record.get("status"), str) else "unknown",
            "read_back": read_back,
        })
    return results


def _ensure_favorite_source_permission(
    *,
    store: object,
    namespace_id: str,
    project_id: str,
    batch_id: str,
    ordinal: int,
    resolved: object,
) -> None:
    if resolved.permission_snapshot is not None:
        return
    manifest = resolved.manifest
    evidence_refs = manifest.permission.evidence_refs
    if len(evidence_refs) != 1:
        raise ValueError("metadata_evidence_unavailable")
    evidence_ref = str(evidence_refs[0])
    authority = SourcePermissionAuthority(store, namespace_id=namespace_id)
    current = authority.current_for_source(
        project_id=project_id,
        source_id=str(manifest.source_id),
        metadata_evidence_ref=evidence_ref,
    )
    if current is not None and current.state == "granted":
        return
    authority.grant(
        project_id=project_id,
        permission_id=str(manifest.source_id),
        source_id=str(manifest.source_id),
        platform="bilibili",
        source_manifest_ref=str(resolved.manifest_ref),
        source_manifest_revision=str(resolved.manifest_revision),
        metadata_evidence_ref=evidence_ref,
        actor_id="local-user",
        command_id=_favorite_operation_id(batch_id, ordinal, "grant"),
        created_at=_utc_now(),
        expected_revision=0 if current is None else current.revision,
    )


def _favorite_operation_id(batch_id: str, ordinal: int, operation: str) -> str:
    suffix = batch_id[-80:]
    return f"fav-{ordinal}-{operation}-{suffix}"


def _favorite_item_error(error: BaseException) -> str:
    value = str(error).strip()
    if isinstance(error, (
        BilibiliFavoriteCollectionError,
        BilibiliFavoriteBatchError,
        HandsMediaIngressDisabled,
        MediaIngressRequestConflict,
        MediaIngressSelectionError,
        MediaHandsRuntimeUnavailable,
        TypeError,
        ValueError,
    )) and value:
        return value[:200]
    return "favorite_item_processing_failed"


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label}_invalid")
    return value


def _project_identifier(value: object) -> str:
    if not isinstance(value, str) or _PROJECT_IDENTIFIER.fullmatch(value) is None:
        raise ValueError("project_id_invalid")
    return value


def _expected_platform(request: Request) -> str | None:
    return "bilibili" if "/media-ingress/bilibili/" in request.url.path else None


def _media_url(value: object, *, expected_platform: str | None) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("media_url_invalid")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (ValueError, UnicodeError) as error:
        raise ValueError("media_url_invalid") from error
    common_invalid = (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    )
    bilibili = (
        (host == "bilibili.com" or host.endswith(".bilibili.com"))
        and parsed.path.startswith("/video/")
    )
    xiaohongshu = (
        (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"))
        and parsed.path.startswith("/explore/")
    )
    if expected_platform == "bilibili" and (common_invalid or not bilibili):
        raise ValueError("bilibili_url_invalid")
    if common_invalid or not (bilibili or xiaohongshu):
        raise ValueError("media_url_invalid")
    return value


async def _json_body(request: Request) -> Mapping[str, object] | None:
    try:
        value = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _response(status_code: int, body: Mapping[str, object]) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=dict(body),
        headers={"Cache-Control": "no-store"},
    )
