from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.api.container import ApiContainerDep
from backend.api.responses import (
    LinkedVideoDownloadResponse,
    ResolveBilibiliSeriesRequest,
    ResolveBilibiliVideoRequest,
    SeriesResponse,
    VideoCardResponse,
)
from backend.api.sse import stream_progress_events
from backend.api.media_ingress_selection_authority import (
    LegacyMediaIngressDisabled,
    MediaIngressSelectionError,
    media_ingress_selection_authority_for_root,
    media_ingress_selection_public,
)
from backend.bilibili.ytdlp_bilibili import build_video_download_task_id

router = APIRouter()


@router.post("/api/linked/bilibili/resolve/series", response_model=SeriesResponse)
async def resolve_bilibili_series(request: ResolveBilibiliSeriesRequest, container: ApiContainerDep) -> SeriesResponse:
    try:
        series = await _run_legacy_bilibili_effect(
            container, container.resolve_bilibili_series.run, url=request.url,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return SeriesResponse.from_model(series)


@router.post("/api/linked/bilibili/resolve/video", response_model=VideoCardResponse)
async def resolve_bilibili_video(request: ResolveBilibiliVideoRequest, container: ApiContainerDep) -> VideoCardResponse:
    try:
        video = await _run_legacy_bilibili_effect(
            container,
            container.resolve_bilibili_video.run,
            url=request.url,
            target_series_id=request.target_series_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return VideoCardResponse.from_model(video)


@router.post("/api/videos/{series_id}/{video_id}/download", response_model=LinkedVideoDownloadResponse)
async def start_video_download(series_id: str, video_id: str, container: ApiContainerDep) -> LinkedVideoDownloadResponse:
    try:
        # The use case identifies the concrete provider.  Its Bilibili starter is
        # also fenced in bootstrap so an already scheduled task cannot cross a
        # later Hands cutover; this route additionally avoids creating the legacy
        # task/progress record when Hands is currently selected.
        operation = container.start_linked_video_download.run
        arguments = {"series_id": series_id, "video_id": video_id}
        result = (
            _run_legacy_bilibili_effect_sync(container, operation, **arguments)
            if _linked_video_provider(container, series_id, video_id) == "bilibili"
            else operation(**arguments)
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return LinkedVideoDownloadResponse.started(result.task_id)


async def _run_legacy_bilibili_effect(container: object, operation, **kwargs):
    authority = _selection(container)
    if authority is None:
        return await operation(**kwargs)
    try:
        return await asyncio.to_thread(
            _run_legacy_bilibili_effect_in_thread, authority, operation, kwargs,
        )
    except LegacyMediaIngressDisabled as error:
        raise _legacy_ingress_disabled(error) from error
    except MediaIngressSelectionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _run_legacy_bilibili_effect_sync(container: object, operation, **kwargs):
    authority = _selection(container)
    if authority is None:
        return operation(**kwargs)
    try:
        with authority.writer("legacy"):
            return operation(**kwargs)
    except LegacyMediaIngressDisabled as error:
        raise _legacy_ingress_disabled(error) from error
    except MediaIngressSelectionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _run_legacy_bilibili_effect_in_thread(authority, operation, kwargs):
    with authority.writer("legacy"):
        return asyncio.run(operation(**kwargs))


def _selection(container: object):
    root_dir = getattr(container, "root_dir", None)
    if root_dir is None:
        # Lightweight legacy test containers predate the durable root contract.
        # Production ApiContainer always supplies root_dir; do not invent a
        # second selection store just to make those isolated fixtures pass.
        return None
    return media_ingress_selection_authority_for_root(Path(root_dir))


def _legacy_ingress_disabled(error: LegacyMediaIngressDisabled) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "status": "legacy_ingress_disabled",
            "reason": "bilibili ingress is assigned to Media Hands",
            "selection": media_ingress_selection_public(error.selection),
            "next_step": "use_analyze_source_with_explicit_source_permission",
        },
    )


def _linked_video_provider(container: object, series_id: str, video_id: str) -> str | None:
    """Inspect the existing linked-workspace authority without creating an effect."""

    workspace = getattr(container, "linked_series_workspace", None)
    get_linked_series = getattr(workspace, "get_linked_series", None)
    if not callable(get_linked_series):
        return None
    linked_series = get_linked_series(series_id)
    videos = getattr(linked_series, "videos", ()) if linked_series is not None else ()
    video = next((item for item in videos if getattr(item, "video_id", None) == video_id), None)
    provider = getattr(video, "provider", None)
    return provider if isinstance(provider, str) else None


@router.post("/api/videos/{series_id}/{video_id}/download/cancel")
async def cancel_video_download(series_id: str, video_id: str, container: ApiContainerDep) -> dict[str, str]:
    task_id = build_video_download_task_id(series_id, video_id)
    container.video_download_progress_tracker.request_cancel(task_id)
    return {"status": "cancelling"}


@router.get("/api/videos/{series_id}/{video_id}/download/progress")
async def stream_video_download_progress(series_id: str, video_id: str, container: ApiContainerDep) -> StreamingResponse:
    task_id = build_video_download_task_id(series_id, video_id)
    return StreamingResponse(
        stream_progress_events(
            tracker=container.video_download_progress_tracker,
            task_id=task_id,
            terminal_statuses={"completed", "failed", "cancelled"},
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
