from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from backend.api.container import ApiContainerDep
from backend.api.desktop_session import desktop_session
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.workbench_original_asset_runtime import (
    build_original_asset_batch_store,
    build_original_asset_resolver,
    build_original_asset_store,
    original_assets_root,
)
from backend.security import (
    DesktopFileGrantError,
    FILE_GRANT_MAX_BYTES,
    StreamStorageBudgetError,
    require_stream_storage_budget,
    verify_desktop_file_grant,
)
from core.product_core.workbench_original_asset import (
    WorkbenchOriginalAssetError,
    serialize_workbench_original_asset,
    serialize_workbench_original_asset_availability,
    serialize_workbench_original_asset_batch,
)


router = APIRouter(tags=["workbench-original-asset"])


def _headers() -> dict[str, str]:
    return {"Content-Type": "application/json", "Cache-Control": "no-store"}


def _json_response(status_code: int, body: Mapping[str, Any]) -> JSONResponse:
    return JSONResponse(content=body, status_code=status_code, headers=_headers())


async def _json_body(request: Request) -> Mapping[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, Mapping) else {}


def _body_str(body: Mapping[str, Any], key: str) -> str | None:
    value = body.get(key)
    return value if isinstance(value, str) else None


def _body_int(body: Mapping[str, Any], key: str) -> int | None:
    value = body.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@router.post("/api/rebuild/workbench/original-asset")
async def workbench_original_asset(request: Request, container: ApiContainerDep) -> JSONResponse:
    store, settings = build_rebuild_object_store(container.root_dir)
    body = await _json_body(request)
    try:
        result = build_original_asset_store(
            container.root_dir,
            store,
            namespace_id=settings.namespace_id,
        ).execute(
            display_name=_body_str(body, "display_name") or "",
            media_type=_body_str(body, "media_type") or "application/octet-stream",
            size_bytes=_body_int(body, "size_bytes") or 0,
            content_base64=_body_str(body, "content_base64") or "",
            source_kind=_body_str(body, "source_kind") or "file",
        )
    except WorkbenchOriginalAssetError as error:
        return _json_response(
            400,
            {
                "detail": "workbench original asset rejected",
                "reason": str(error),
                "actionable": True,
            },
        )
    return _json_response(201, serialize_workbench_original_asset(result))


@router.post("/api/rebuild/workbench/original-asset-stream")
async def workbench_original_asset_stream(request: Request, container: ApiContainerDep) -> JSONResponse:
    session = desktop_session()
    if session is None:
        return _json_response(403, {"detail": "desktop file grant required"})
    try:
        grant = verify_desktop_file_grant(
            {key.lower(): value for key, value in request.headers.items()},
            session_secret=session.secret,
            session_instance_id=session.instance_id,
        )
    except DesktopFileGrantError as error:
        return _json_response(403, {"detail": str(error)})
    if grant.size_bytes > FILE_GRANT_MAX_BYTES:
        return _json_response(413, {"detail": "file grant exceeds streaming budget"})

    store, settings = build_rebuild_object_store(container.root_dir)
    assets_root = original_assets_root(container.root_dir)
    incoming = assets_root / ".incoming"
    try:
        require_stream_storage_budget(incoming, incoming_bytes=grant.size_bytes)
    except StreamStorageBudgetError as error:
        return _json_response(507, {"detail": str(error), "actionable": True})
    staged = incoming / f"{grant.grant_id}.part"
    received = 0
    digest = hashlib.sha256()
    try:
        with staged.open("wb") as output:
            async for chunk in request.stream():
                if not chunk:
                    continue
                received += len(chunk)
                if received > grant.size_bytes or received > FILE_GRANT_MAX_BYTES:
                    raise WorkbenchOriginalAssetError("streamed original asset exceeds grant size")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if received != grant.size_bytes:
            raise WorkbenchOriginalAssetError("streamed original asset size does not match grant")
        if digest.hexdigest() != grant.sha256:
            raise WorkbenchOriginalAssetError("streamed original asset sha256 does not match grant")
        result = build_original_asset_store(
            container.root_dir,
            store,
            namespace_id=settings.namespace_id,
        ).execute_staged_file(
            display_name=grant.display_name,
            media_type=grant.media_type,
            size_bytes=grant.size_bytes,
            sha256=grant.sha256,
            staged_path=staged,
            source_kind=grant.source_kind,
        )
    except (ClientDisconnect, OSError, WorkbenchOriginalAssetError) as error:
        staged.unlink(missing_ok=True)
        return _json_response(400, {"detail": str(error)})
    return _json_response(201, serialize_workbench_original_asset(result))


@router.get("/api/rebuild/library/sources/{source_id}/original-asset")
async def library_source_original_asset(source_id: str, container: ApiContainerDep) -> JSONResponse:
    store, _settings = build_rebuild_object_store(container.root_dir)
    try:
        result = build_original_asset_resolver(container.root_dir, store).for_source(source_id)
    except WorkbenchOriginalAssetError as error:
        return _json_response(400, {"detail": str(error), "actionable": True})
    if result is None:
        return _json_response(404, {"detail": "source original asset not found"})
    return _json_response(200, serialize_workbench_original_asset_availability(result))


@router.get("/api/rebuild/desktop/original-assets/{asset_id}/resolve")
async def desktop_resolve_original_asset(
    asset_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    session = desktop_session()
    signature = request.headers.get("x-chriptmas-main-signature", "")
    expected = hmac.new(
        session.secret.encode("utf-8") if session else b"",
        f"open-original-asset:{asset_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if session is None or not signature or not hmac.compare_digest(signature, expected):
        return _json_response(403, {"detail": "desktop main authorization required"})
    store, _settings = build_rebuild_object_store(container.root_dir)
    try:
        result = build_original_asset_resolver(container.root_dir, store).for_asset(asset_id)
    except WorkbenchOriginalAssetError as error:
        return _json_response(400, {"detail": str(error), "actionable": True})
    if result.status != "available" or result.path is None:
        return _json_response(409, serialize_workbench_original_asset_availability(result))
    payload = serialize_workbench_original_asset_availability(result)
    payload["resolved_path"] = str(result.path)
    return _json_response(200, payload)


@router.post("/api/rebuild/workbench/original-assets")
async def workbench_original_assets_batch(request: Request, container: ApiContainerDep) -> JSONResponse:
    store, settings = build_rebuild_object_store(container.root_dir)
    body = await _json_body(request)
    raw_assets = body.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        return _json_response(400, {"detail": "assets must be a non-empty array of asset objects"})
    try:
        result = build_original_asset_batch_store(
            container.root_dir,
            store,
            namespace_id=settings.namespace_id,
        ).execute(assets=raw_assets)
    except WorkbenchOriginalAssetError as error:
        return _json_response(
            400,
            {
                "detail": "workbench original asset batch rejected",
                "reason": str(error),
                "actionable": True,
            },
        )
    status_code = 201 if result.status == "completed" else (207 if result.status == "partial" else 400)
    return _json_response(status_code, serialize_workbench_original_asset_batch(result))
