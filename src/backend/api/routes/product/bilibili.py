"""Bilibili ownership for the product API."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.media_ingress_selection_authority import (
    LegacyMediaIngressDisabled,
    MediaIngressSelectionAuthority,
    MediaIngressSelectionError,
    media_ingress_selection_authority_for_root,
    media_ingress_selection_public,
)
from backend.api.video_auto_effect_runtime import VideoAutoEffectRuntime

from core.effect_log import EffectClass, EffectWorkflowHandler
from core.product_core.downloaded_video_source import RegisterDownloadedBilibiliVideoSource
from core.product_core.video_auto_workflow import serialize_video_auto_workflow_result
from core.product_core.video_link_adapter import (
    AuthorizedBilibiliDownloadResult,
    AuthorizedBilibiliDownloader,
    BilibiliVideoLinkResolver,
    GetBilibiliDownloaderSettings,
    LinkedVideoDownloadPlanner,
    serialize_authorized_bilibili_download_result,
    serialize_bilibili_downloader_settings,
)
from core.product_core.video_workflow_endpoint import (
    ServeAuthorizedBilibiliDownloadEndpoint,
    ServeBilibiliVideoDownloadPlanEndpoint,
)
from core.product_core.workflow_progression import (
    WorkflowDecisionBoundary,
    decide_workflow_progression,
)

from . import document_delivery_services as product_document_delivery_services
from . import http as product_http
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.get("/api/rebuild/settings/bilibili-downloader")
async def bilibili_downloader_settings(request: Request, container: ApiContainerDep) -> JSONResponse:
    """读取 bilibili 下载器配置（cookie mode / output root / 启用状态等）。

    前端 MediaWorkflowPanel 通过此端点展示当前下载器配置。
    """
    store, _settings = product_repositories._object_store(container.root_dir)
    settings = GetBilibiliDownloaderSettings(store).execute()
    return product_http._json_response(
        200,
        serialize_bilibili_downloader_settings(settings),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.post("/api/rebuild/video-links/bilibili/download-plan")
async def bilibili_video_download_plan(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await product_http._json_body(request)
    return await asyncio.to_thread(
        _legacy_bilibili_video_download_plan,
        container,
        request.method,
        product_document_delivery_services._path_with_query(request),
        body,
    )


@router.post("/api/rebuild/video-links/bilibili/authorized-download")
async def bilibili_authorized_download(request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await product_http._json_body(request)
    return await asyncio.to_thread(
        _legacy_bilibili_authorized_download,
        container,
        request.app.state.effect_runtime.runner,
        request.method,
        product_document_delivery_services._path_with_query(request),
        body,
    )


@router.post("/api/rebuild/video-links/bilibili/auto-download")
async def bilibili_auto_download(request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await product_http._json_body(request)
    return await asyncio.to_thread(
        _legacy_bilibili_auto_download,
        container,
        request.app.state.effect_runtime.runner,
        request.method,
        body,
    )


def _legacy_bilibili_video_download_plan(
    container: object,
    method: str,
    path: str,
    body: Mapping[str, object] | None,
) -> JSONResponse:
    authority = _media_ingress_selection_authority_for(container)
    try:
        with authority.writer("legacy"):
            response = ServeBilibiliVideoDownloadPlanEndpoint().execute(
                method=method,
                path=path,
                body=body,
                resolve_link=BilibiliVideoLinkResolver().resolve,
                create_plan=LinkedVideoDownloadPlanner().create_dry_run_plan,
            )
            return product_http._json_response(response.status_code, response.body, response.headers)
    except (LegacyMediaIngressDisabled, MediaIngressSelectionError) as error:
        return _legacy_bilibili_ingress_error(error)


def _legacy_bilibili_authorized_download(
    container: object,
    effect_runner,
    method: str,
    path: str,
    body: Mapping[str, object] | None,
) -> JSONResponse:
    authority = _media_ingress_selection_authority_for(container)
    try:
        with authority.writer("legacy"):
            store, settings = product_repositories._object_store(getattr(container, "root_dir"))
            downloader_settings = GetBilibiliDownloaderSettings(store).execute()
            response = ServeAuthorizedBilibiliDownloadEndpoint().execute(
                method=method,
                path=path,
                body=body,
                download_video=_effect_bilibili_downloader(
                    store, settings.namespace_id, effect_runner,
                ),
                trusted_settings=downloader_settings,
                register_downloaded_video=RegisterDownloadedBilibiliVideoSource(
                    store,
                    namespace_id=settings.namespace_id,
                ).execute,
            )
            return _finish_legacy_bilibili_download(
                response, body, store, settings, effect_runner,
            )
    except (LegacyMediaIngressDisabled, MediaIngressSelectionError) as error:
        return _legacy_bilibili_ingress_error(error)


def _legacy_bilibili_auto_download(
    container: object,
    _effect_runner,
    method: str,
    body: Mapping[str, object] | None,
) -> JSONResponse:
    authority = _media_ingress_selection_authority_for(container)
    try:
        with authority.writer("legacy"):
            store, _settings = product_repositories._object_store(getattr(container, "root_dir"))
            downloader_settings = GetBilibiliDownloaderSettings(store).execute()
            progression = decide_workflow_progression(
                WorkflowDecisionBoundary.EXTERNAL_DOWNLOAD_WRITES_FILE,
            )
            return product_http._json_response(
                409,
                {
                    "status": "needs_confirmation",
                    "reason": progression.reason.value,
                    "progression_mode": progression.mode.value,
                    "progression_reason": progression.reason.value,
                    "next_step": "confirm_authorized_download",
                    "downloader": {
                        "status": downloader_settings.status,
                        "enabled": downloader_settings.enabled,
                        "cookie_mode": downloader_settings.cookie_mode,
                        "output_root_configured": bool(str(downloader_settings.output_root).strip()),
                        "remote_processing": downloader_settings.remote_processing,
                        "memory_publication": downloader_settings.memory_publication,
                    },
                },
                {"Content-Type": "application/json", "Cache-Control": "no-store"},
            )
    except (LegacyMediaIngressDisabled, MediaIngressSelectionError) as error:
        return _legacy_bilibili_ingress_error(error)


def _finish_legacy_bilibili_download(
    response,
    body: Mapping[str, object] | None,
    store,
    settings,
    effect_runner,
) -> JSONResponse:
    response_body = dict(response.body)
    source_id = response_body.get("source_id")
    if response.status_code == 200 and response_body.get("status") == "completed" and isinstance(source_id, str):
        workflow = VideoAutoEffectRuntime(
            store,
            namespace_id=settings.namespace_id,
            effect_runner=effect_runner,
            gate_decision_id="legacy-bilibili-download:v1",
        ).execute(
            source_id=source_id,
            project_id=product_http._optional_body_str(body, "project_id"),
        )
        response_body["auto_workflow"] = serialize_video_auto_workflow_result(workflow)
        return product_http._json_response(response.status_code, response_body, response.headers)
    return product_http._json_response(response.status_code, response.body, response.headers)


def _effect_bilibili_downloader(store, namespace_id: str, effect_runner):
    effects = EffectWorkflowHandler(
        effect_runner, store, namespace_id=namespace_id,
    )
    downloader = AuthorizedBilibiliDownloader()

    def execute(*, plan, settings):
        operation_id = f"bilibili-download:{plan.bvid}:{plan.page}"
        return effects.execute(
            operation_id=operation_id,
            session_id=f"bilibili-download:{plan.series_id}",
            root_id=f"bilibili-download:{plan.series_id}",
            step_key=f"download:{plan.bvid}:{plan.page}",
            kind="bilibili_authorized_download",
            intent_ref=(
                f"crp://{namespace_id}/workflow-intents/bilibili/"
                f"{plan.bvid}/{plan.page}"
            ),
            gate_decision_id="legacy-bilibili-download:v1",
            rev_set={"workflow_revision": "2", "provider_revision": "yt-dlp-v1"},
            payload={
                "series_id": plan.series_id,
                "bvid": plan.bvid,
                "page": plan.page,
                "cookie_mode": settings.cookie_mode,
            },
            effect_class=EffectClass.AT_MOST_ONCE,
            invoke=lambda: downloader.execute(plan=plan, settings=settings),
            encode=serialize_authorized_bilibili_download_result,
            decode=lambda value: AuthorizedBilibiliDownloadResult(
                **{
                    **dict(value),
                    "command": tuple(value.get("command", ())),
                    "blocked_operations": tuple(value.get("blocked_operations", ())),
                }
            ),
        )

    return execute


def _media_ingress_selection_authority_for(
    container: object,
) -> MediaIngressSelectionAuthority:
    root = Path(getattr(container, "root_dir"))
    return media_ingress_selection_authority_for_root(root)


def _legacy_bilibili_ingress_error(error: Exception) -> JSONResponse:
    if isinstance(error, LegacyMediaIngressDisabled):
        return product_http._json_response(
            409,
            {
                "status": "legacy_ingress_disabled",
                "reason": "bilibili ingress is assigned to Media Hands",
                "selection": media_ingress_selection_public(error.selection),
                "next_step": "use_analyze_source_with_explicit_source_permission",
            },
            product_http._no_store_headers(),
        )
    return product_http._json_response(
        409,
        {
            "status": "media_ingress_selection_unavailable",
            "reason": str(error),
        },
        product_http._no_store_headers(),
    )
