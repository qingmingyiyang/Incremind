from __future__ import annotations

from collections.abc import Mapping
import json
import os
import secrets
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse

from backend.api.container import ApiContainerDep
from backend.api.ai_runtime import get_or_build_ai_runtime
from backend.api.series_intake_ai_runtime import (
    SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY,
    SERIES_INTAKE_ORGANIZE_OUTCOME,
    SERIES_INTAKE_ORGANIZE_SNAPSHOT_KIND,
)
from backend.api.series_turn_scope_authority import build_series_turn_scope_authority
from backend.replay.contracts import (
    AssetMetadata,
    CreateIntakeRequest,
    CreateSeriesRequest,
    IntakeBatchRequest,
    IntakeItem,
    IntakeMergeRequest,
    MemoryAnswer,
    MemoryQuestionRequest,
    MemorySession,
    Report,
    ReportMarkdownDocument,
    ReportMergeRequest,
    ReportMergeResponse,
    SaveReportMarkdownRequest,
    Series,
    SeriesStats,
    UpdateAssetMetadataRequest,
    UpdateIntakeRequest,
    UpdateSeriesRequest,
    VideoKnowledgeRequest,
)
from backend.replay.reports import ReportService
from backend.replay.prompts import REPLAY_INTAKE_ORGANIZER_PROMPT_VERSION
from backend.replay.series_workspace import MAX_ASSET_BYTES, SeriesWorkspace
from backend.video_intake.service import IntakeService


router = APIRouter(tags=["knowledge-series"])


def _workspace(request: Request, container: ApiContainerDep) -> SeriesWorkspace:
    current = getattr(request.app.state, "series_workspace", None)
    if current is None:
        current = SeriesWorkspace(container.root_dir)
        request.app.state.series_workspace = current
    return current


def _legacy_series_model_unavailable() -> None:
    raise HTTPException(
        status_code=503,
        detail="旧版系列 AI 合并入口已停用，请通过统一 AI 工作台创建合并任务；原始内容未修改。",
    )


# R100: 确认令牌 TTL（5 分钟，与 Rust CONFIRM_TOKEN_TTL_SECS 一致）
_CONFIRM_TOKEN_TTL = 300


def _token_store(request: Request) -> dict[str, tuple[str, float]]:
    """获取或初始化确认令牌存储（存于 app.state）。"""
    store = getattr(request.app.state, "confirm_tokens", None)
    if store is None:
        store = {}
        request.app.state.confirm_tokens = store
    # 清理过期令牌
    now = time.time()
    expired = [t for t, (_, exp) in store.items() if exp < now]
    for t in expired:
        del store[t]
    return store


def _validate_token(request: Request, token: str, action: str) -> None:
    """R100: 验证并消费确认令牌。验证通过后令牌即失效（一次性）。"""
    if not token or len(token) > 48:
        raise HTTPException(status_code=403, detail="确认令牌格式无效。")
    store = _token_store(request)
    entry = store.pop(token, None)
    if entry is None:
        raise HTTPException(status_code=403, detail="确认令牌无效或已过期，请重新获取确认令牌。")
    stored_action, _ = entry
    if stored_action != action:
        raise HTTPException(status_code=403, detail="确认令牌与当前操作不匹配。")


@router.post("/api/confirm/generate")
def generate_confirm_token(payload: dict[str, str], request: Request) -> dict[str, str]:
    """R100: 生成一次性确认令牌（Python fallback 路径）。"""
    action = (payload.get("action") or "").strip()
    if not action or len(action) > 64:
        raise HTTPException(status_code=422, detail="操作标识无效。")
    token = secrets.token_hex(4).upper()[:8]
    store = _token_store(request)
    store[token] = (action, time.time() + _CONFIRM_TOKEN_TTL)
    return {"token": token}


@router.get("/api/series", response_model=list[Series])
def list_series(
    request: Request,
    container: ApiContainerDep,
    include_archived: bool = False,
) -> list[Series]:
    return _workspace(request, container).list_series(include_archived=include_archived)


@router.post("/api/series", response_model=Series, status_code=status.HTTP_201_CREATED)
def create_series(payload: CreateSeriesRequest, request: Request, container: ApiContainerDep) -> Series:
    return _call(_workspace(request, container).create_series, payload)


@router.get("/api/series/trash")
def list_trash(request: Request, container: ApiContainerDep) -> list[dict[str, object]]:
    return _call(_workspace(request, container).list_trash)


@router.get("/api/series/{series_id}", response_model=Series)
def get_series(series_id: str, request: Request, container: ApiContainerDep) -> Series:
    return _call(_workspace(request, container).get_series, series_id)


@router.patch("/api/series/{series_id}", response_model=Series)
def update_series(
    series_id: str,
    payload: UpdateSeriesRequest,
    request: Request,
    container: ApiContainerDep,
) -> Series:
    return _call(_workspace(request, container).update_series, series_id, payload)


@router.post("/api/series/{series_id}/activate", response_model=Series)
def activate_series(series_id: str, request: Request, container: ApiContainerDep) -> Series:
    return _call(_workspace(request, container).activate_series, series_id)


@router.post("/api/series/{series_id}/archive", response_model=Series)
def archive_series(series_id: str, request: Request, container: ApiContainerDep) -> Series:
    return _call(_workspace(request, container).archive_series, series_id)


@router.delete("/api/series/{series_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_series(
    series_id: str,
    request: Request,
    container: ApiContainerDep,
    token: str = Query(default=""),
) -> None:
    _validate_token(request, token, "delete_series")
    _call(_workspace(request, container).delete_series, series_id, confirm=True)


# —— 回收站路由（与 Rust business.rs trash 对称） ——


@router.post("/api/series/trash/restore")
def restore_trash_item(payload: dict[str, str], request: Request, container: ApiContainerDep) -> dict[str, str]:
    trash_path = payload.get("trash_path", "")
    if not trash_path or ".." in trash_path:
        raise HTTPException(status_code=422, detail="回收站路径无效。")
    _call(_workspace(request, container).restore_trash, trash_path)
    return {"status": "ok", "restored_from": trash_path}


@router.delete("/api/series/trash", status_code=status.HTTP_204_NO_CONTENT)
def clean_trash(request: Request, container: ApiContainerDep, token: str = Query(default="")) -> None:
    _validate_token(request, token, "clean_trash")
    _call(_workspace(request, container).clean_trash)
def migrate_default_series(request: Request, container: ApiContainerDep) -> dict[str, object]:
    return _call(_workspace(request, container).migrate_legacy)


@router.get("/api/series/{series_id}/overview")
def series_overview(series_id: str, request: Request, container: ApiContainerDep) -> dict[str, object]:
    return _call(_workspace(request, container).overview, series_id)


@router.post("/api/series/{series_id}/open-folder")
def open_series_folder(series_id: str, request: Request, container: ApiContainerDep) -> dict[str, str]:
    path = _call(_workspace(request, container).series_path, series_id)
    _call(_workspace(request, container).get_series, series_id)
    if os.name != "nt":
        raise HTTPException(status_code=501, detail="当前系统不支持直接打开文件夹。")
    os.startfile(str(path))  # type: ignore[attr-defined]
    return {"status": "opened"}


@router.get("/api/series/{series_id}/stats", response_model=SeriesStats)
@router.get("/api/series/{series_id}/stats/overview", response_model=SeriesStats)
def series_stats(
    series_id: str,
    request: Request,
    container: ApiContainerDep,
    range_name: str = Query(default="all", alias="range"),
    start_date: str = Query(default="", alias="start"),
    end_date: str = Query(default="", alias="end"),
) -> SeriesStats:
    return _call(
        _workspace(request, container).stats,
        series_id,
        range_name=range_name,
        start_date=start_date,
        end_date=end_date,
    )


@router.get("/api/series/{series_id}/stats/heatmap")
def series_heatmap(series_id: str, request: Request, container: ApiContainerDep) -> dict[str, object]:
    stats = _call(_workspace(request, container).stats, series_id)
    return {"series_id": series_id, "heatmap": stats.heatmap, "streak_days": stats.streak_days}


@router.get("/api/series/{series_id}/integrity-check")
def integrity_check(series_id: str, request: Request, container: ApiContainerDep) -> dict[str, object]:
    return _call(_workspace(request, container).check_integrity, series_id)


@router.post("/api/series/{series_id}/backup")
def backup_series(series_id: str, request: Request, container: ApiContainerDep) -> dict[str, object]:
    ws = _workspace(request, container)
    series = ws.get_series(series_id)
    backup_root = ws.series_root.parent / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in series.name).strip()[:60] or "series"
    archive_path = shutil.make_archive(
        str(backup_root / f"{safe_name}_{series.series_id}_{stamp}"),
        "zip",
        root_dir=ws.series_path(series_id),
    )
    size = Path(archive_path).stat().st_size
    backup_label = f"{series.name} ({series.series_id}) — {stamp}"
    return {"status": "ok", "path": archive_path, "size": size, "label": backup_label}


@router.get("/api/series/{series_id}/backups")
def list_backups(series_id: str, container: ApiContainerDep) -> list[dict[str, object]]:
    backup_root = Path(container.root_dir) / "library" / "backups"
    if not backup_root.is_dir():
        return []
    backups: list[dict[str, object]] = []
    for path in sorted(backup_root.glob(f"*{series_id}*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = path.stat()
        backups.append({
            "filename": path.name,
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return backups


@router.post("/api/series/{series_id}/backups/restore")
def restore_backup(series_id: str, body: dict[str, str], request: Request, container: ApiContainerDep) -> dict[str, object]:
    filename = body.get("filename", "")
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=422, detail="文件名无效。")
    ws = _workspace(request, container)
    backup_root = ws.series_root.parent / "backups"
    archive_path = backup_root / filename
    if not archive_path.is_file():
        raise HTTPException(status_code=404, detail="备份文件不存在。")

    # 自动备份当前系列（还原前安全快照）
    series = ws.get_series(series_id)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in series.name).strip()[:60] or "series"
    backup_root.mkdir(parents=True, exist_ok=True)
    pre_restore_path = shutil.make_archive(
        str(backup_root / f"{safe_name}_{series.series_id}_{stamp}_prerestore"),
        "zip",
        root_dir=ws.series_path(series_id),
    )
    pre_size = Path(pre_restore_path).stat().st_size

    # 解压备份到系列目录
    series_path = ws.series_path(series_id)
    with zipfile.ZipFile(archive_path, "r") as zf:
        zf.extractall(series_path)

    return {"status": "ok", "restored_from": filename, "pre_restore_backup": Path(pre_restore_path).name, "pre_restore_size": pre_size}


@router.get("/api/series/{series_id}/stats/model-usage")
@router.get("/api/series/{series_id}/stats/tokens")
def series_tokens(series_id: str, request: Request, container: ApiContainerDep) -> dict[str, object]:
    stats = _call(_workspace(request, container).stats, series_id)
    return {"series_id": series_id, "tokens": stats.tokens}


@router.get("/api/series/{series_id}/stats/activity")
def series_activity(series_id: str, request: Request, container: ApiContainerDep) -> dict[str, object]:
    stats = _call(_workspace(request, container).stats, series_id)
    return {
        "series_id": series_id,
        "heatmap": stats.heatmap,
        "last_recorded_at": stats.last_recorded_at,
        "streak_days": stats.streak_days,
    }


@router.get("/api/stats/global-overview")
def global_stats(request: Request, container: ApiContainerDep) -> dict[str, object]:
    return _call(_workspace(request, container).global_overview)


@router.get("/api/series/{series_id}/knowledge-items")
def list_knowledge_items(
    series_id: str,
    request: Request,
    container: ApiContainerDep,
    query: str = "",
) -> list[dict[str, object]]:
    workspace = _workspace(request, container)
    library = _call(workspace.replay_library, series_id)
    values = library.search_items(query, limit=100) if query.strip() else library.list_items()
    return [item.model_dump(mode="json") for item in values]


@router.post("/api/series/{series_id}/videos/{record_id}/intake", response_model=IntakeItem)
def create_video_intake(
    series_id: str,
    record_id: str,
    payload: VideoKnowledgeRequest,
    request: Request,
    container: ApiContainerDep,
) -> IntakeItem:
    workspace = _workspace(request, container)
    _call(workspace.get_series, series_id)
    detail = IntakeService(container.root_dir, series_id).detail(record_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="当前系列中不存在该视频资料。")
    summary = detail.summary if isinstance(detail.summary, dict) else {}
    summary_text = str(summary.get("thirty_second_summary") or summary.get("summary") or "").strip()
    core_problem = str(summary.get("core_problem") or "").strip()
    raw_takeaways = summary.get("key_takeaways", [])
    takeaways = (
        [str(value).strip() for value in raw_takeaways if str(value).strip()]
        if isinstance(raw_takeaways, list)
        else []
    )
    structured_lines = [f"# {detail.record.title}", ""]
    if summary_text:
        structured_lines.extend(["## 摘要", "", summary_text, ""])
    if core_problem:
        structured_lines.extend(["## 核心问题", "", core_problem, ""])
    if takeaways:
        structured_lines.extend(["## 关键收获", "", *[f"- {value}" for value in takeaways], ""])
    warnings = [] if summary else ["视频结构化摘要尚不可用，已保留视频元数据供入库前复核。"]
    actions = []
    if payload.high_value:
        actions.append("标记为高价值")
    if payload.need_review:
        actions.append("加入待复盘")
    return _call(
        workspace.create_intake,
        series_id,
        CreateIntakeRequest(
            type="video_summary",
            title=detail.record.title,
            raw_text=summary_text or detail.record.description,
            structured_text="\n".join(structured_lines).rstrip(),
            summary=summary_text,
            tags=detail.record.tags,
            source=detail.record.source_url,
            suggested_report_type="daily" if payload.include_in_daily else "none",
            suggested_actions=actions,
            created_by="user",
        ),
        warnings=warnings,
    )


@router.get("/api/series/{series_id}/intake", response_model=list[IntakeItem])
def list_intake(
    series_id: str,
    request: Request,
    container: ApiContainerDep,
    intake_status: str | None = Query(default=None, alias="status"),
    item_type: str | None = Query(default=None, alias="type"),
    query: str = "",
) -> list[IntakeItem]:
    return _call(
        _workspace(request, container).list_intake,
        series_id,
        status=intake_status,
        item_type=item_type,
        query=query,
    )


@router.post("/api/series/{series_id}/intake", response_model=IntakeItem, status_code=status.HTTP_201_CREATED)
def create_intake(
    series_id: str,
    payload: CreateIntakeRequest,
    request: Request,
    container: ApiContainerDep,
) -> IntakeItem:
    return _call(_workspace(request, container).create_intake, series_id, payload)


@router.get("/api/series/{series_id}/intake/{intake_id}", response_model=IntakeItem)
def get_intake(series_id: str, intake_id: str, request: Request, container: ApiContainerDep) -> IntakeItem:
    return _call(_workspace(request, container).get_intake, series_id, intake_id)


@router.patch("/api/series/{series_id}/intake/{intake_id}", response_model=IntakeItem)
def update_intake(
    series_id: str,
    intake_id: str,
    payload: UpdateIntakeRequest,
    request: Request,
    container: ApiContainerDep,
) -> IntakeItem:
    return _call(_workspace(request, container).update_intake, series_id, intake_id, payload)


@router.post("/api/series/{series_id}/intake/{intake_id}/approve", response_model=IntakeItem)
def approve_intake(series_id: str, intake_id: str, request: Request, container: ApiContainerDep) -> IntakeItem:
    return _call(_workspace(request, container).approve_intake, series_id, intake_id)


@router.post("/api/series/{series_id}/intake/{intake_id}/reject", response_model=IntakeItem)
def reject_intake(series_id: str, intake_id: str, request: Request, container: ApiContainerDep) -> IntakeItem:
    return _call(_workspace(request, container).reject_intake, series_id, intake_id)


@router.post("/api/series/{series_id}/intake/{intake_id}/promote", response_model=IntakeItem)
def promote_intake(series_id: str, intake_id: str, request: Request, container: ApiContainerDep) -> IntakeItem:
    return _call(_workspace(request, container).promote_intake, series_id, intake_id)


@router.post("/api/series/{series_id}/intake/{intake_id}/organize", response_model=IntakeItem)
def organize_intake(series_id: str, intake_id: str, request: Request, container: ApiContainerDep) -> IntakeItem:
    workspace = _workspace(request, container)
    try:
        item, expected_revision = workspace.organization_snapshot(series_id, intake_id)
        try:
            runtime = get_or_build_ai_runtime(request, container)
        except Exception as error:
            _mark_series_intake_failed_if_current(
                workspace, series_id, intake_id, expected_revision,
            )
            raise HTTPException(status_code=503, detail="AI 服务未配置或不可用。") from error
        metadata = getattr(runtime, "composition_metadata", {})
        if not isinstance(metadata, Mapping) or metadata.get("series_intake_organize_remote_usable") is not True:
            _mark_series_intake_failed_if_current(
                workspace, series_id, intake_id, expected_revision,
            )
            raise HTTPException(status_code=503, detail="AI 服务未配置或不可用。")
        scope = build_series_turn_scope_authority(container.root_dir).freeze_agent_series_scope(
            series_id=series_id,
        )
        turn = _series_intake_organize_turn(
            item=item,
            expected_revision=expected_revision,
            scope=scope,
        )
        waiting = runtime.submit_turn(turn)
        if getattr(waiting, "status", None) == "waiting_approval":
            waiting = runtime.apply_action(_series_intake_approval(runtime, waiting, turn))
        if getattr(waiting, "status", None) != "completed":
            certainty = _series_intake_failure_certainty(runtime, str(getattr(waiting, "turn_id")))
            if certainty == "confirmed_none":
                try:
                    workspace.mark_intake_organization_failed(
                        series_id, intake_id, expected_revision=expected_revision,
                    )
                except RuntimeError:
                    pass
            if certainty == "unknown":
                raise RuntimeError("AI 整理结果存在未知影响，需要核对后再试。")
            raise RuntimeError("AI 整理未完成，原始内容未修改。")
        presentation = runtime.presentation_for(str(getattr(waiting, "turn_id")))
        if (
            not isinstance(presentation, Mapping)
            or presentation.get("status") != "reviewing"
            or presentation.get("series_id") != series_id
            or presentation.get("intake_id") != intake_id
        ):
            raise RuntimeError("AI 整理结果不可用，原始内容未修改。")
        return workspace.get_intake(series_id, intake_id)
    except HTTPException:
        raise
    except Exception as error:
        return _call(_raise_series_intake_bridge_error, error)


@router.post("/api/series/{series_id}/intake/{intake_id}/merge-to-report", response_model=ReportMergeResponse)
def merge_intake_to_report(
    series_id: str,
    intake_id: str,
    payload: IntakeMergeRequest,
    request: Request,
    container: ApiContainerDep,
) -> ReportMergeResponse:
    del series_id, intake_id, payload, request, container
    _legacy_series_model_unavailable()


@router.post("/api/series/{series_id}/intake/batch-approve", response_model=list[IntakeItem])
def batch_approve(
    series_id: str,
    payload: IntakeBatchRequest,
    request: Request,
    container: ApiContainerDep,
) -> list[IntakeItem]:
    return _call(_workspace(request, container).batch_intake, series_id, payload.intake_ids, "approve")


@router.post("/api/series/{series_id}/intake/batch-reject", response_model=list[IntakeItem])
def batch_reject(
    series_id: str,
    payload: IntakeBatchRequest,
    request: Request,
    container: ApiContainerDep,
) -> list[IntakeItem]:
    return _call(_workspace(request, container).batch_intake, series_id, payload.intake_ids, "reject")


@router.post("/api/series/{series_id}/intake/batch-promote", response_model=list[IntakeItem])
def batch_promote(
    series_id: str,
    payload: IntakeBatchRequest,
    request: Request,
    container: ApiContainerDep,
) -> list[IntakeItem]:
    return _call(_workspace(request, container).batch_intake, series_id, payload.intake_ids, "promote")


@router.get("/api/series/{series_id}/reports", response_model=list[Report])
@router.get("/api/series/{series_id}/reports/list", response_model=list[Report])
def list_reports(
    series_id: str,
    request: Request,
    container: ApiContainerDep,
    report_type: Literal["daily", "weekly", "monthly", "yearly"] | None = Query(default=None, alias="type"),
) -> list[Report]:
    workspace = _workspace(request, container)
    return _call(workspace.replay_library, series_id).list_reports(report_type)


@router.post("/api/series/{series_id}/reports/{report_id}/merge", response_model=ReportMergeResponse)
def merge_report(
    series_id: str,
    report_id: str,
    payload: ReportMergeRequest,
    request: Request,
    container: ApiContainerDep,
) -> ReportMergeResponse:
    del series_id, report_id, payload, request, container
    _legacy_series_model_unavailable()


@router.post("/api/series/{series_id}/reports/{report_type}/{period}", response_model=Report)
def generate_report(
    series_id: str,
    report_type: Literal["daily", "weekly", "monthly", "yearly"],
    period: str,
    request: Request,
    container: ApiContainerDep,
) -> Report:
    service = ReportService(_call(_workspace(request, container).replay_library, series_id))
    generator = {
        "daily": service.generate_daily,
        "weekly": service.generate_weekly,
        "monthly": service.generate_monthly,
        "yearly": service.generate_yearly,
    }[report_type]
    return _call(generator, period)


@router.get("/api/series/{series_id}/reports/{report_id}/metadata", response_model=Report)
def report_metadata(series_id: str, report_id: str, request: Request, container: ApiContainerDep) -> Report:
    report = _call(_workspace(request, container).replay_library, series_id).get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在。")
    return report


@router.get("/api/series/{series_id}/reports/{report_id}/markdown", response_model=ReportMarkdownDocument)
def get_report_markdown(
    series_id: str,
    report_id: str,
    request: Request,
    container: ApiContainerDep,
) -> ReportMarkdownDocument:
    return _call(_workspace(request, container).read_report_markdown, series_id, report_id)


@router.put("/api/series/{series_id}/reports/{report_id}/markdown", response_model=ReportMarkdownDocument)
def save_report_markdown(
    series_id: str,
    report_id: str,
    payload: SaveReportMarkdownRequest,
    request: Request,
    container: ApiContainerDep,
) -> ReportMarkdownDocument:
    return _call(
        _workspace(request, container).write_report_markdown,
        series_id,
        report_id,
        payload.markdown,
        base_revision=payload.base_revision,
    )


@router.get("/api/series/{series_id}/reports/{report_id}/history")
def report_history(
    series_id: str,
    report_id: str,
    request: Request,
    container: ApiContainerDep,
) -> list[dict[str, object]]:
    return _call(_workspace(request, container).list_report_history, series_id, report_id)


@router.post("/api/series/{series_id}/reports/{report_id}/history/restore", response_model=ReportMarkdownDocument)
def restore_report_history(
    series_id: str,
    report_id: str,
    payload: dict[str, str],
    request: Request,
    container: ApiContainerDep,
) -> ReportMarkdownDocument:
    version = str(payload.get("version", ""))
    if not version:
        raise HTTPException(status_code=422, detail="version 是必填参数。")
    return _call(_workspace(request, container).restore_report_history, series_id, report_id, version)


@router.get("/api/series/{series_id}/assets", response_model=list[AssetMetadata])
def list_assets(series_id: str, request: Request, container: ApiContainerDep) -> list[AssetMetadata]:
    return _call(_workspace(request, container).list_assets, series_id)


@router.post("/api/series/{series_id}/assets/import", status_code=status.HTTP_201_CREATED)
async def import_asset(
    series_id: str,
    request: Request,
    container: ApiContainerDep,
    file: UploadFile = File(...),
    create_intake: bool = Form(default=True),
) -> dict[str, object]:
    data = await file.read(MAX_ASSET_BYTES + 1)
    metadata, intake = _call(
        _workspace(request, container).import_asset,
        series_id,
        filename=file.filename or "attachment",
        media_type=file.content_type or "application/octet-stream",
        data=data,
        create_intake=create_intake,
    )
    return {
        "asset": metadata.model_dump(mode="json"),
        "intake": intake.model_dump(mode="json") if intake else None,
    }


@router.get("/api/series/{series_id}/assets/{asset_id}/metadata", response_model=AssetMetadata)
def asset_metadata(series_id: str, asset_id: str, request: Request, container: ApiContainerDep) -> AssetMetadata:
    return _call(_workspace(request, container).get_asset, series_id, asset_id)


@router.patch("/api/series/{series_id}/assets/{asset_id}/metadata", response_model=AssetMetadata)
def update_asset_metadata(
    series_id: str,
    asset_id: str,
    body: UpdateAssetMetadataRequest,
    request: Request,
    container: ApiContainerDep,
) -> AssetMetadata:
    """R79: 更新附件元数据（当前仅支持 manual_summary）。"""
    return _call(_workspace(request, container).update_asset_metadata, series_id, asset_id, body)


@router.get("/api/series/{series_id}/assets/{asset_id}", response_class=FileResponse)
def read_asset(series_id: str, asset_id: str, request: Request, container: ApiContainerDep) -> FileResponse:
    workspace = _workspace(request, container)
    metadata = _call(workspace.get_asset, series_id, asset_id)
    path = _call(workspace.asset_file, series_id, asset_id)
    return FileResponse(path, media_type=metadata.media_type, filename=metadata.filename)


@router.delete("/api/series/{series_id}/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_asset(
    series_id: str,
    asset_id: str,
    request: Request,
    container: ApiContainerDep,
    token: str = Query(default=""),
) -> None:
    _validate_token(request, token, "delete_asset")
    _call(_workspace(request, container).delete_asset, series_id, asset_id, confirm=True)


@router.post("/api/series/{series_id}/knowledge-items/{item_id}/attach", response_model=AssetMetadata)
def attach_asset(
    series_id: str,
    item_id: str,
    asset_id: str,
    request: Request,
    container: ApiContainerDep,
) -> AssetMetadata:
    return _call(_workspace(request, container).attach_asset, series_id, item_id, asset_id)


@router.get("/api/series/{series_id}/memory/session", response_model=MemorySession)
def memory_session(series_id: str, request: Request, container: ApiContainerDep) -> MemorySession:
    return _call(_workspace(request, container).memory_session, series_id)


@router.post("/api/series/{series_id}/memory/ask", response_model=MemoryAnswer)
def ask_memory(
    series_id: str,
    payload: MemoryQuestionRequest,
    request: Request,
    container: ApiContainerDep,
) -> MemoryAnswer:
    workspace = _workspace(request, container)
    return _call(
        workspace.ask_memory,
        series_id,
        payload.model_copy(update={"series_id": series_id}),
        gateway=None,
    )


@router.post("/api/series/{series_id}/memory/new", response_model=MemorySession)
def new_memory_session(series_id: str, request: Request, container: ApiContainerDep) -> MemorySession:
    return _call(_workspace(request, container).new_memory_session, series_id)


def _series_intake_organize_turn(
    *, item: IntakeItem, expected_revision: str, scope: object,
) -> dict[str, object]:
    identity = uuid5(
        NAMESPACE_URL,
        f"series-intake-organize:{item.series_id}:{item.intake_id}:{expected_revision}",
    ).hex
    authority = {
        "kind": "project_series_scope_v1",
        "object_id": str(getattr(scope, "object_id")),
        "payload_revision": int(getattr(scope, "payload_revision")),
        "storage_revision": int(getattr(scope, "storage_revision")),
        "authority_identity": str(getattr(scope, "authority_identity")),
        "authority_ref": str(getattr(scope, "authority_ref")),
    }
    snapshot = {
        "kind": SERIES_INTAKE_ORGANIZE_SNAPSHOT_KIND,
        "series_id": item.series_id,
        "project_id": str(getattr(scope, "project_id")),
        "authority": authority,
        "intake_id": item.intake_id,
        "intake": item.model_dump(mode="json"),
        "expected_revision": expected_revision,
        "prompt_version": REPLAY_INTAKE_ORGANIZER_PROMPT_VERSION,
    }
    turn_id = f"turn-{identity}"
    operation_id = f"op-series-intake-{identity}"
    return {
        "schema_version": "1.0.0",
        "turn_id": turn_id,
        "session_id": f"session-series-intake-{identity}",
        "operation_id": operation_id,
        "idempotency_key": f"series-intake-organize-{identity}",
        "scope": {
            "kind": "series",
            "project_id": str(getattr(scope, "project_id")),
            "series_id": item.series_id,
            "authority": authority,
        },
        "input": {
            "kind": "text",
            "text": json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "refs": [{
                "kind": "atom",
                "object_id": item.intake_id,
                "uri": f"crp://default/series/{item.series_id}/intake/{item.intake_id}",
            }],
        },
        "desired_outcome": SERIES_INTAKE_ORGANIZE_OUTCOME,
        "privacy": {
            "mode": "remote_allowed",
            "allow_remote": True,
            "pii": "possible",
            "consent_refs": ["crp://default/consent/provider-egress-policy"],
            "retention": "local_durable",
        },
        "capability_policy": {
            "allowed": [SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY],
            "denied": [],
            "require_approval": [SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY],
        },
        "context_policy": {
            "include_project_skill": False,
            "include_memory": False,
            "include_session_history": False,
            "max_context_bytes": 4096,
        },
        "approval_policy": {"mode": "always", "auto_approve_read_only": True},
        "created_at": item.updated_at,
    }


def _series_intake_approval(
    runtime: object, waiting: object, turn: Mapping[str, object],
) -> dict[str, object]:
    events = tuple(runtime.events_after(str(getattr(waiting, "turn_id"))))
    approval = next(event for event in reversed(events) if event.get("type") == "approval.required")
    return {
        "schema_version": "1.0.0",
        "action_id": "action-" + uuid5(
            NAMESPACE_URL, f"approve:{turn['turn_id']}:{approval['event_id']}",
        ).hex,
        "turn_id": str(getattr(waiting, "turn_id")),
        "type": "approve",
        "target_event_id": approval["event_id"],
        "reason": "legacy Series Intake organize mapped to canonical AI Turn approval",
        "actor": "user",
        "expected_sequence": int(getattr(waiting, "current_sequence")),
        "idempotency_key": f"approve-{turn['idempotency_key']}",
        "created_at": turn["created_at"],
    }


def _raise_series_intake_bridge_error(error: Exception) -> None:
    raise RuntimeError("AI 整理未完成，原始内容未修改。") from error


def _mark_series_intake_failed_if_current(
    workspace: SeriesWorkspace,
    series_id: str,
    intake_id: str,
    expected_revision: str,
) -> None:
    try:
        workspace.mark_intake_organization_failed(
            series_id, intake_id, expected_revision=expected_revision,
        )
    except RuntimeError:
        return


def _series_intake_failure_certainty(runtime: object, turn_id: str) -> str | None:
    try:
        projection = runtime.execution_projection_for(turn_id, "developer")
    except Exception:
        return None
    if not isinstance(projection, Mapping):
        return None
    steps = projection.get("tool_steps")
    if not isinstance(steps, list):
        return None
    if not steps:
        return "confirmed_none"
    step = next(
        (
            item for item in reversed(steps)
            if isinstance(item, Mapping)
            and item.get("capability_id") == SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY
        ),
        None,
    )
    if not isinstance(step, Mapping):
        return None
    attempts = step.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return None
    attempt = attempts[-1]
    certainty = attempt.get("effect_certainty") if isinstance(attempt, Mapping) else None
    return certainty if certainty in {"confirmed_none", "unknown"} else None


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except FileExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
