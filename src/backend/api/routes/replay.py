from __future__ import annotations

from datetime import datetime
import os
from collections import OrderedDict
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse

from backend.api.container import ApiContainerDep
from backend.replay.contracts import (
    KnowledgeItem,
    MemoryAnswer,
    MemoryQuestionRequest,
    QuickCaptureRequest,
    ReplayTask,
    ReplayDashboard,
    Report,
    VideoKnowledgeRequest,
)
from backend.replay.items import KnowledgeItemService
from backend.replay.dashboard import ReplayDashboardService
from backend.replay.memory_qa import MemoryQAService
from backend.replay.reports import ReportService
from backend.replay.series_workspace import SeriesWorkspace
from backend.video_intake.service import IntakeService


router = APIRouter(prefix="/api/replay", tags=["knowledge-replay"])

_INTAKE_SERVICES_MAX = 8  # R103: prevent unbounded per-series IntakeService cache (R96 🟢#7)


def _series_workspace(request: Request, container: ApiContainerDep) -> SeriesWorkspace:
    current = getattr(request.app.state, "series_workspace", None)
    if current is None:
        current = SeriesWorkspace(container.root_dir)
        request.app.state.series_workspace = current
    return current


def _series_id(request: Request, container: ApiContainerDep) -> str:
    value = request.headers.get("X-Series-Id", "").strip()
    if not value:
        raise HTTPException(status_code=422, detail="知识 API 必须提供 X-Series-Id。")
    try:
        return _series_workspace(request, container).get_series(value).series_id
    except (LookupError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def _item_service(request: Request, container: ApiContainerDep) -> KnowledgeItemService:
    series_id = _series_id(request, container)
    return KnowledgeItemService(_series_workspace(request, container).replay_library(series_id))


def _intake_service(request: Request, container: ApiContainerDep) -> IntakeService:
    override = getattr(request.app.state, "intake_service", None)
    if override is not None:
        return override
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


def _report_service(request: Request, container: ApiContainerDep) -> ReportService:
    return ReportService(_item_service(request, container).library)


def _memory_service(request: Request, container: ApiContainerDep) -> MemoryQAService:
    return MemoryQAService(_item_service(request, container).library)


def _dashboard_service(request: Request, container: ApiContainerDep) -> ReplayDashboardService:
    return ReplayDashboardService(_item_service(request, container).library)


@router.get("/status")
def replay_status(request: Request, container: ApiContainerDep) -> dict[str, object]:
    service = _item_service(request, container)
    today = datetime.now().astimezone().date().isoformat()
    items = service.library.list_items()
    daily_items = service.library.journal_items(today)
    return {
        "library_path": str(service.library.root),
        "today": today,
        "item_count": len(items),
        "today_item_count": len(daily_items),
        "journal_ready": (service.library.root / "journals" / today[:4] / today[5:7] / today).is_dir(),
        "daily_report_ready": service.library.get_report(f"daily_{today}") is not None,
        "replay_task_count": len(service.library.list_tasks()),
    }


@router.post("/items", response_model=KnowledgeItem, status_code=status.HTTP_201_CREATED)
def create_item(
    payload: QuickCaptureRequest,
    request: Request,
    container: ApiContainerDep,
) -> KnowledgeItem:
    try:
        return _item_service(request, container).capture(
            payload.model_copy(update={"series_id": _series_id(request, container)})
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/items", response_model=list[KnowledgeItem])
def list_items(
    request: Request,
    container: ApiContainerDep,
    query: str = "",
    item_type: Literal["video", "note", "clip", "thought", "action", "question", "report"] | None = Query(
        default=None,
        alias="type",
    ),
    in_daily: bool | None = None,
) -> list[KnowledgeItem]:
    service = _item_service(request, container)
    items = service.library.search_items(query) if query.strip() else service.library.list_items()
    if item_type is not None:
        items = [item for item in items if item.type == item_type]
    if in_daily is not None:
        items = [item for item in items if item.status.in_daily is in_daily]
    return items


@router.get("/items/{item_id}", response_model=KnowledgeItem)
def get_item(item_id: str, request: Request, container: ApiContainerDep) -> KnowledgeItem:
    try:
        item = _item_service(request, container).library.get_item(item_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="知识条目不存在。") from error
    if item is None:
        raise HTTPException(status_code=404, detail="知识条目不存在。")
    return item


@router.post("/videos/{record_id}/knowledge-item", response_model=KnowledgeItem)
def create_video_item(
    record_id: str,
    payload: VideoKnowledgeRequest,
    request: Request,
    container: ApiContainerDep,
) -> KnowledgeItem:
    detail = _intake_service(request, container).detail(record_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="视频资料不存在。")
    try:
        return _item_service(request, container).from_video(
            detail,
            include_in_daily=payload.include_in_daily,
            high_value=payload.high_value,
            need_review=payload.need_review,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/reports/daily/{day}", response_model=Report)
def generate_daily_report(day: str, request: Request, container: ApiContainerDep) -> Report:
    try:
        return _report_service(request, container).generate_daily(day)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/reports/weekly/{week}", response_model=Report)
def generate_weekly_report(week: str, request: Request, container: ApiContainerDep) -> Report:
    try:
        return _report_service(request, container).generate_weekly(week)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/reports/monthly/{month}", response_model=Report)
def generate_monthly_report(month: str, request: Request, container: ApiContainerDep) -> Report:
    try:
        return _report_service(request, container).generate_monthly(month)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/reports/yearly/{year}", response_model=Report)
def generate_yearly_report(year: str, request: Request, container: ApiContainerDep) -> Report:
    try:
        return _report_service(request, container).generate_yearly(year)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/reports", response_model=list[Report])
def list_reports(
    request: Request,
    container: ApiContainerDep,
    report_type: Literal["daily", "weekly", "monthly", "yearly"] | None = Query(default=None, alias="type"),
) -> list[Report]:
    return _item_service(request, container).library.list_reports(report_type)


@router.get("/reports/{report_id}", response_model=Report)
def get_report(report_id: str, request: Request, container: ApiContainerDep) -> Report:
    try:
        report = _item_service(request, container).library.get_report(report_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="报告不存在。") from error
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在。")
    return report


@router.get("/reports/{report_id}/export.md", response_class=FileResponse)
def export_report_markdown(report_id: str, request: Request, container: ApiContainerDep) -> FileResponse:
    path = _report_file(report_id, "markdown", request, container)
    return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=path.name)


@router.get("/reports/{report_id}/export.json", response_class=FileResponse)
def export_report_json(report_id: str, request: Request, container: ApiContainerDep) -> FileResponse:
    path = _report_file(report_id, "json", request, container)
    return FileResponse(path, media_type="application/json", filename=path.name)


@router.post("/reports/{report_id}/open-folder")
def open_report_folder(report_id: str, request: Request, container: ApiContainerDep) -> dict[str, str]:
    files = _report_files(report_id, request, container)
    path = files["directory"]
    _open_folder(path)
    return {"status": "opened", "path": str(path)}


@router.post("/journals/{day}/open-folder")
def open_journal_folder(day: str, request: Request, container: ApiContainerDep) -> dict[str, str]:
    try:
        path = _item_service(request, container).library.journal_dir(day)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    _open_folder(path)
    return {"status": "opened", "path": str(path)}


@router.get("/tasks", response_model=list[ReplayTask])
def list_replay_tasks(request: Request, container: ApiContainerDep) -> list[ReplayTask]:
    return _item_service(request, container).library.list_tasks()


@router.post("/memory/ask", response_model=MemoryAnswer)
def ask_memory(
    payload: MemoryQuestionRequest,
    request: Request,
    container: ApiContainerDep,
) -> MemoryAnswer:
    try:
        return _memory_service(request, container).ask(
            payload.model_copy(update={"series_id": _series_id(request, container)})
        )
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/dashboard", response_model=ReplayDashboard)
def get_replay_dashboard(request: Request, container: ApiContainerDep) -> ReplayDashboard:
    return _dashboard_service(request, container).build()


def _report_files(
    report_id: str,
    request: Request,
    container: ApiContainerDep,
) -> dict[str, Path]:
    try:
        files = _item_service(request, container).library.report_files(report_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="报告不存在。") from error
    if files is None:
        raise HTTPException(status_code=404, detail="报告不存在。")
    return files


def _report_file(
    report_id: str,
    kind: Literal["markdown", "json"],
    request: Request,
    container: ApiContainerDep,
) -> Path:
    path = _report_files(report_id, request, container)[kind]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="报告导出文件不存在。")
    return path


def _open_folder(path: Path) -> None:
    if os.name != "nt":
        raise HTTPException(status_code=501, detail="当前系统不支持直接打开文件夹。")
    os.startfile(str(path))  # type: ignore[attr-defined]
