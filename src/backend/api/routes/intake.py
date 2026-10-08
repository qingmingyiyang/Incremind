from __future__ import annotations

import mimetypes
import os
from collections import OrderedDict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from backend.api.container import ApiContainerDep
from backend.api.routes.series import _validate_token
from backend.replay.series_workspace import SeriesWorkspace
from backend.video_intake.bilibili import BilibiliAccessError
from backend.video_intake.models import (
    AskVideoRequest,
    AskVideoResponse,
    DeleteRecordsRequest,
    IntakeTask,
    LibraryResponse,
    RecordDetail,
    ResolveSourceRequest,
    ResolvedSource,
    StartImportRequest,
    UpdateNotesRequest,
    UpdateVisionSettingsRequest,
    VisionSettingsResponse,
)
from backend.video_intake.service import IntakeService
from backend.video_intake.vision import VisionSettings, load_vision_settings, save_vision_settings
from backend.api.contracts import ProviderEgressConsentRequest
from backend.security import ProviderEgressError, ProviderEgressPolicyStore


router = APIRouter(prefix="/api/intake", tags=["video-intake"])

_INTAKE_SERVICES_MAX = 8  # R103: prevent unbounded per-series IntakeService cache (R96 🟢#7)


def _service(request: Request, container: ApiContainerDep) -> IntakeService:
    series_id = _series_id(request, container)
    services = getattr(request.app.state, "intake_services", None)
    if services is None:
        services = OrderedDict()
        request.app.state.intake_services = services
    current = services.get(series_id)
    if current is None:
        if len(services) >= _INTAKE_SERVICES_MAX:
            services.popitem(last=False)
        current = IntakeService(container.root_dir, series_id)
        services[series_id] = current
    return current


def _series_id(request: Request, container: ApiContainerDep) -> str:
    header = (request.headers.get("X-Series-Id") or "").strip()
    query = (request.query_params.get("series_id") or "").strip()
    if header and query and header != query:
        raise HTTPException(status_code=409, detail="Header 与查询参数中的 series_id 不一致。")
    series_id = header or query
    if not series_id:
        raise HTTPException(status_code=400, detail="知识操作必须提供 X-Series-Id。")
    try:
        series = SeriesWorkspace(container.root_dir).get_series(series_id)
    except (LookupError, ValueError) as error:
        raise HTTPException(status_code=404, detail="系列不存在。") from error
    if series.status == "archived":
        raise HTTPException(status_code=409, detail="归档系列不允许继续写入或读取视频上下文。")
    return series.series_id


@router.get("/status")
def intake_status(request: Request, container: ApiContainerDep) -> dict[str, object]:
    service = _service(request, container)
    vision = load_vision_settings(container.root_dir, container.secret_store)
    return {
        "library_path": str(service.storage.ensure_root()),
        "cookie_browser": "edge",
        "cookie_snapshot_ready": service.client.has_cookie_snapshot,
        "cookie_access": service.client.cookie_access,
        "cookie_warning": service.client.cookie_warning,
        "cookie_snapshot_path": "",
        "vision_mode": vision.mode,
        "vision_provider": vision.provider,
        "vision_model": vision.model,
        "vision_real_configured": vision.real_configured,
        "record_count": len(service.list_records()),
        "active_task_count": len([task for task in service.list_tasks() if task.status in {"queued", "running"}]),
    }


@router.get("/vision-settings", response_model=VisionSettingsResponse)
def get_vision_settings(container: ApiContainerDep) -> VisionSettingsResponse:
    settings = load_vision_settings(container.root_dir, container.secret_store)
    policy = ProviderEgressPolicyStore(container.root_dir)
    manifest = _vision_egress_manifest(settings, policy)
    return VisionSettingsResponse(
        mode=settings.mode,
        provider=settings.provider,
        base_url=settings.base_url,
        model=settings.model,
        has_api_key=settings.has_api_key,
        api_key_masked="***" if settings.has_api_key else "",
        max_frames=settings.max_frames,
        timeout_seconds=settings.timeout_seconds,
        real_configured=settings.real_configured,
        privacy_notice="真实模式只发送筛选后的关键帧与附近转写，不发送完整视频。",
        egress_manifest=manifest.public_dict(consented=policy.is_consented(manifest)),
    )


@router.put("/vision-settings", response_model=VisionSettingsResponse)
def update_vision_settings(payload: UpdateVisionSettingsRequest, container: ApiContainerDep) -> VisionSettingsResponse:
    current = load_vision_settings(container.root_dir, container.secret_store)
    save_vision_settings(
        container.root_dir,
        VisionSettings(
            mode=payload.mode,
            provider=payload.provider.strip() or "openai_compatible",
            base_url=payload.base_url.strip().rstrip("/"),
            model=payload.model.strip(),
            has_api_key=current.has_api_key or bool(payload.api_key and payload.api_key.strip()),
            max_frames=payload.max_frames,
            timeout_seconds=payload.timeout_seconds,
        ),
        container.secret_store,
        api_key=payload.api_key,
    )
    return get_vision_settings(container)


@router.post("/vision-settings/egress-consent", response_model=VisionSettingsResponse)
def grant_vision_egress_consent(
    payload: ProviderEgressConsentRequest,
    container: ApiContainerDep,
) -> VisionSettingsResponse:
    settings = load_vision_settings(container.root_dir, container.secret_store)
    policy = ProviderEgressPolicyStore(container.root_dir)
    manifest = _vision_egress_manifest(settings, policy)
    try:
        policy.grant(manifest, manifest_id=payload.manifest_id, confirm=payload.confirm)
    except ProviderEgressError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return get_vision_settings(container)


@router.delete("/vision-settings/egress-consent", response_model=VisionSettingsResponse)
def revoke_vision_egress_consent(container: ApiContainerDep) -> VisionSettingsResponse:
    ProviderEgressPolicyStore(container.root_dir).revoke("vision")
    return get_vision_settings(container)


def _vision_egress_manifest(settings: VisionSettings, policy: ProviderEgressPolicyStore):
    return policy.manifest(
        provider_id="vision",
        endpoint=settings.base_url or "http://127.0.0.1/disabled",
        purposes=("vision_analysis",),
        payload_categories=("image_frame", "instructions", "source_excerpt"),
        max_payload_bytes=16 * 1024 * 1024,
    )


@router.post("/resolve", response_model=ResolvedSource)
async def resolve_source(
    payload: ResolveSourceRequest,
    request: Request,
    container: ApiContainerDep,
) -> ResolvedSource:
    try:
        return await _service(request, container).resolve(payload.url)
    except BilibiliAccessError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/tasks", response_model=IntakeTask)
async def start_import(
    payload: StartImportRequest,
    request: Request,
    container: ApiContainerDep,
) -> IntakeTask:
    del payload, request, container
    raise HTTPException(
        status_code=503,
        detail="旧版视频导入任务已停用，请通过统一 analyze_source 使用 Media Hands；未创建任务或媒体记录。",
    )


@router.get("/tasks", response_model=list[IntakeTask])
def list_tasks(request: Request, container: ApiContainerDep) -> list[IntakeTask]:
    return _service(request, container).list_tasks()


@router.get("/tasks/{task_id}", response_model=IntakeTask)
def get_task(task_id: str, request: Request, container: ApiContainerDep) -> IntakeTask:
    task = _service(request, container).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    return task


@router.get("/library", response_model=LibraryResponse)
def list_library(
    request: Request,
    container: ApiContainerDep,
    cross_series: bool = False,
) -> LibraryResponse:
    service = _service(request, container)
    if cross_series:
        workspace = SeriesWorkspace(container.root_dir)
        current = workspace.get_series(service.series_id)
        if not current.preferences.allow_cross_series_search:
            raise HTTPException(status_code=403, detail="当前系列未允许跨系列检索。")
        records = []
        for series in workspace.list_series():
            records.extend(IntakeService(container.root_dir, series.series_id).list_records())
        return LibraryResponse(root_path=str(workspace.series_root), records=records)
    return LibraryResponse(root_path=str(service.storage.ensure_root()), records=service.list_records())


@router.post("/library/batch-delete")
def batch_delete_records(
    payload: DeleteRecordsRequest,
    request: Request,
    container: ApiContainerDep,
    token: str = Query(default=""),
) -> dict[str, object]:
    _validate_token(request, token, "delete_records")
    service = _service(request, container)
    deleted = service.storage.delete_records(payload.record_ids)
    return {"deleted": deleted, "deleted_count": len(deleted)}


@router.get("/library/{record_id}", response_model=RecordDetail)
def get_record(record_id: str, request: Request, container: ApiContainerDep) -> RecordDetail:
    detail = _service(request, container).detail(record_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="视频资料不存在。")
    return detail


@router.get("/library/{record_id}/media")
def get_record_media(record_id: str, request: Request, container: ApiContainerDep) -> FileResponse:
    service = _service(request, container)
    record = service.storage.find_record(record_id)
    if record is None or not record.media_file:
        raise HTTPException(status_code=404, detail="音视频文件不存在。")
    path = service.storage.record_dir(record) / record.media_file
    if not path.exists():
        raise HTTPException(status_code=404, detail="音视频文件已被移动或删除。")
    media_type, _ = mimetypes.guess_type(path.name)
    return FileResponse(path, media_type=media_type or "application/octet-stream")


@router.get("/library/{record_id}/cover")
def get_record_cover(record_id: str, request: Request, container: ApiContainerDep) -> FileResponse:
    service = _service(request, container)
    record = service.storage.find_record(record_id)
    if record is None or not record.cover_file:
        raise HTTPException(status_code=404, detail="封面不存在。")
    path = service.storage.record_dir(record) / record.cover_file
    if not path.exists():
        raise HTTPException(status_code=404, detail="封面已被移动或删除。")
    media_type, _ = mimetypes.guess_type(path.name)
    return FileResponse(path, media_type=media_type or "image/jpeg")


@router.get("/library/{record_id}/frames/{frame_id}")
def get_record_frame(
    record_id: str,
    frame_id: str,
    request: Request,
    container: ApiContainerDep,
) -> FileResponse:
    service = _service(request, container)
    detail = service.detail(record_id)
    if detail is None or not detail.structured:
        raise HTTPException(status_code=404, detail="视频资料或画面分析不存在。")
    visual = detail.structured.get("visual_analysis")
    frames = visual.get("keyframes", []) if isinstance(visual, dict) else []
    frame = next(
        (item for item in frames if isinstance(item, dict) and str(item.get("frame_id")) == frame_id),
        None,
    )
    if frame is None:
        raise HTTPException(status_code=404, detail="关键帧不存在。")
    record_dir = service.storage.record_dir(detail.record).resolve()
    path = (record_dir / str(frame.get("file") or "")).resolve()
    try:
        path.relative_to(record_dir)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="关键帧路径无效。") from error
    if not path.exists():
        raise HTTPException(status_code=404, detail="关键帧文件已被移动或删除。")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/library/{record_id}/export.md")
def export_record_markdown(record_id: str, request: Request, container: ApiContainerDep) -> FileResponse:
    service = _service(request, container)
    record = service.storage.find_record(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="视频资料不存在。")
    path = service.storage.record_dir(record) / record.summary_file
    if not path.exists():
        raise HTTPException(status_code=404, detail="内容整理尚未生成。")
    return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=f"{record.id}-内容整理.md")


@router.put("/library/{record_id}/notes")
def update_notes(
    record_id: str,
    payload: UpdateNotesRequest,
    request: Request,
    container: ApiContainerDep,
) -> dict[str, str]:
    service = _service(request, container)
    record = service.storage.find_record(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="视频资料不存在。")
    service.storage.write_notes(record, payload.content)
    return {"status": "saved"}


@router.post("/library/{record_id}/ask", response_model=AskVideoResponse)
async def ask_video(
    record_id: str,
    payload: AskVideoRequest,
    request: Request,
    container: ApiContainerDep,
) -> AskVideoResponse:
    try:
        answer, references = await _service(request, container).ask(record_id, payload.question)
    except LookupError as error:
        raise HTTPException(status_code=404, detail="视频资料不存在。") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"视频问答失败：{error}") from error
    return AskVideoResponse(answer=answer, references=references)


@router.post("/open-library")
def open_library(request: Request, container: ApiContainerDep) -> dict[str, str]:
    path = _service(request, container).storage.ensure_root()
    _open_folder(path)
    return {"status": "opened", "path": str(path)}


@router.post("/library/{record_id}/open-folder")
def open_record_folder(record_id: str, request: Request, container: ApiContainerDep) -> dict[str, str]:
    service = _service(request, container)
    record = service.storage.find_record(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="视频资料不存在。")
    path = service.storage.record_dir(record)
    _open_folder(path)
    return {"status": "opened", "path": str(path)}


def _open_folder(path: Path) -> None:
    if os.name != "nt":
        raise HTTPException(status_code=501, detail="当前系统不支持直接打开文件夹。")
    os.startfile(str(path))  # type: ignore[attr-defined]
