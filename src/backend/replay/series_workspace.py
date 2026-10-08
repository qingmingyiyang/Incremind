from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
import hashlib
import json
import mimetypes
from pathlib import Path
import re
import shutil
import time
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

from backend.replay.contracts import (
    AssetMetadata,
    CreateIntakeRequest,
    CreateSeriesRequest,
    IntakeItem,
    IntakeMergeRequest,
    KnowledgeIndex,
    KnowledgeItem,
    KnowledgeLinks,
    KnowledgeSource,
    KnowledgeStatus,
    MemoryAnswer,
    MemoryMessage,
    MemoryQuestionRequest,
    MemorySession,
    ReportMarkdownDocument,
    ReportMergeRequest,
    ReportMergeResponse,
    Series,
    SeriesStats,
    UpdateAssetMetadataRequest,
    UpdateIntakeRequest,
    UpdateSeriesRequest,
    local_now_iso,
)
from backend.replay.library import ReplayLibrary, _atomic_json, _atomic_text, _keywords, _safe_id
from backend.replay.memory_qa import MemoryQAService
from backend.replay.prompts import (
    REPLAY_INTAKE_ORGANIZER_PROMPT_VERSION,
    REPLAY_MEMORY_BOOK_PROMPT_VERSION,
    REPLAY_REPORT_MERGER_PROMPT_VERSION,
    build_intake_organizer_messages,
    build_memory_book_messages,
    build_report_merger_messages,
)


SERIES_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")
INTAKE_STATUSES = ("pending", "reviewing", "approved", "rejected", "archived", "merged", "failed")
ORGANIZE_FAILURE_WARNING = "AI 整理失败，原始内容已保留。"
MERGE_FAILURE_WARNING = "AI 合并失败，报告与待整理项未修改。"
REPORT_MERGER_CONTRACT_REVISION = int(REPLAY_REPORT_MERGER_PROMPT_VERSION.rsplit("-v", 1)[-1])
TEXT_MEDIA_TYPES = {
    "application/json",
    "application/xml",
    "application/x-yaml",
    "text/csv",
    "text/markdown",
    "text/plain",
}
MAX_ASSET_BYTES = 50 * 1024 * 1024
MAX_REPORT_ASSET_TEXT_CHARS = 20_000
MAX_INTAKE_ASSET_TEXT_CHARS = 40_000


class TextMergeGateway(Protocol):
    def complete_text(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        ...


class IntakeRevisionConflictError(RuntimeError):
    """A caller tried to materialize an Intake snapshot that is no longer current."""


class SeriesWorkspace:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir.resolve()
        self.library_root = self.root_dir / "library"
        self.series_root = self.library_root / "series"
        self.trash_root = self.library_root / ".trash"
        self.global_root = self.library_root / "global"
        self.registry_path = self.global_root / "app-config" / "series.json"
        self._lock = RLock()

    def ensure_initialized(self) -> None:
        with self._lock:
            self.series_root.mkdir(parents=True, exist_ok=True)
            self.registry_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.registry_path.is_file():
                default = Series(
                    series_id="default",
                    name="默认系列",
                    description="由旧资料库兼容迁移而来的默认知识工作空间。",
                    is_default=True,
                    relative_path="series/default",
                )
                self._ensure_series_structure(default.series_id)
                self._save_series_file(default)
                self._write_registry({"active_series_id": "default", "series": [default.model_dump(mode="json")]})
            else:
                registry = self._read_registry()
                if not any(item.get("series_id") == "default" for item in registry["series"]):
                    default = Series(
                        series_id="default",
                        name="默认系列",
                        description="默认知识工作空间。",
                        is_default=True,
                        relative_path="series/default",
                    )
                    registry["series"].insert(0, default.model_dump(mode="json"))
                    registry["active_series_id"] = registry.get("active_series_id") or "default"
                    self._write_registry(registry)
                for item in registry["series"]:
                    series_id = _safe_series_id(str(item["series_id"]))
                    self._ensure_series_structure(series_id)

    def list_series(self, *, include_archived: bool = False) -> list[Series]:
        self.ensure_initialized()
        values = [Series.model_validate(item) for item in self._read_registry()["series"]]
        if not include_archived:
            values = [item for item in values if item.status != "archived"]
        return sorted(values, key=lambda item: (not item.is_default, item.created_at, item.name.lower()))

    def get_series(self, series_id: str) -> Series:
        safe_id = _safe_series_id(series_id)
        for item in self.list_series(include_archived=True):
            if item.series_id == safe_id:
                return item
        raise LookupError("系列不存在。")

    def active_series(self) -> Series:
        self.ensure_initialized()
        registry = self._read_registry()
        active_id = str(registry.get("active_series_id") or "default")
        try:
            active = self.get_series(active_id)
        except LookupError:
            active = self.get_series("default")
        if active.status == "archived":
            return self.get_series("default")
        return active

    def record_launch(self, series_id: str | None = None) -> int:
        target = self.get_series(series_id) if series_id else self.active_series()
        path = self.series_path(target.series_id) / "stats" / "launches.json"
        count = _read_launch_count(self.series_path(target.series_id)) + 1
        _atomic_json(path, {"series_id": target.series_id, "count": count, "updated_at": local_now_iso()})
        return count

    def create_series(self, request: CreateSeriesRequest) -> Series:
        name = request.name.strip()
        if not name:
            raise ValueError("系列名称不能为空。")
        color = _validate_color(request.color)
        with self._lock:
            self.ensure_initialized()
            registry = self._read_registry()
            existing_ids = {str(item["series_id"]) for item in registry["series"]}
            series_id = _unique_series_id(name, existing_ids)
            series = Series(
                series_id=series_id,
                name=name,
                description=request.description.strip(),
                color=color,
                relative_path=f"series/{series_id}",
            )
            self._ensure_series_structure(series_id)
            self._save_series_file(series)
            registry["series"].append(series.model_dump(mode="json"))
            self._write_registry(registry)
            return series

    def update_series(self, series_id: str, request: UpdateSeriesRequest) -> Series:
        with self._lock:
            current = self.get_series(series_id)
            values = request.model_dump(exclude_none=True)
            if "name" in values:
                values["name"] = str(values["name"]).strip()
                if not values["name"]:
                    raise ValueError("系列名称不能为空。")
            if "description" in values:
                values["description"] = str(values["description"]).strip()
            if "color" in values:
                values["color"] = _validate_color(str(values["color"]))
            if "preferences" in values and request.preferences is not None:
                values["preferences"] = request.preferences
            updated = current.model_copy(update={**values, "updated_at": local_now_iso()})
            self._replace_series(updated)
            return updated

    def activate_series(self, series_id: str) -> Series:
        current = self.get_series(series_id)
        if current.status == "archived":
            raise ValueError("归档系列不能设为当前系列。")
        with self._lock:
            registry = self._read_registry()
            registry["active_series_id"] = current.series_id
            self._write_registry(registry)
        return current

    def archive_series(self, series_id: str) -> Series:
        current = self.get_series(series_id)
        if current.is_default:
            raise ValueError("默认系列不能归档。")
        updated = current.model_copy(update={"status": "archived", "updated_at": local_now_iso()})
        with self._lock:
            self._replace_series(updated)
            registry = self._read_registry()
            if registry.get("active_series_id") == current.series_id:
                registry["active_series_id"] = "default"
                self._write_registry(registry)
        return updated

    def delete_series(self, series_id: str, *, confirm: bool) -> None:
        if not confirm:
            raise ValueError("删除系列需要明确确认。")
        current = self.get_series(series_id)
        if current.is_default:
            raise ValueError("默认系列不能删除。")
        path = self.series_path(current.series_id)
        with self._lock:
            registry = self._read_registry()
            registry["series"] = [item for item in registry["series"] if item.get("series_id") != current.series_id]
            if registry.get("active_series_id") == current.series_id:
                registry["active_series_id"] = "default"
            self._write_registry(registry)
            if path.is_dir():
                self._move_to_trash(path, current.series_id)

    # —— 回收站（与 Rust business.rs 对称） ——

    def _move_to_trash(self, path: Path, series_id: str) -> None:
        """将系列目录移入 .trash/ 而非永久删除。与 Rust delete_series 行为一致。"""
        self.trash_root.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time())
        trash_name = f"series-{series_id}-{timestamp}"
        trash_path = self.trash_root / trash_name
        # 避免同一秒内重复删除产生同名冲突
        counter = 1
        while trash_path.exists():
            trash_path = self.trash_root / f"{trash_name}-{counter}"
            counter += 1
        try:
            shutil.move(str(path), str(trash_path))
        except OSError:
            # 跨驱动器 move 失败时回退到 copy+delete
            shutil.copytree(str(path), str(trash_path))
            shutil.rmtree(path)

    def _parse_trash_series_id(self, dir_name: str) -> str | None:
        """从 trash 目录名 'series-{id}-{ts}' 中解析 series_id。与 Rust restore_trash 一致。"""
        if not dir_name.startswith("series-"):
            return None
        stem = dir_name[len("series-"):]
        # series_id 可能包含 '-'（如 my-kb），取最后一个 '-' 之前的部分
        last_dash = stem.rfind("-")
        if last_dash <= 0:
            return None
        candidate = stem[:last_dash]
        return candidate if candidate else None

    def list_trash(self) -> list[dict[str, object]]:
        """列出回收站中所有系列条目。与 Rust list_trash 一致。"""
        if not self.trash_root.is_dir():
            return []
        items: list[dict[str, object]] = []
        for entry in sorted(self.trash_root.iterdir(), key=lambda p: p.name):
            if not entry.name.startswith("series-"):
                continue
            size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            items.append({
                "name": entry.name,
                "path": str(entry),
                "size": size,
            })
        return items

    def restore_trash(self, trash_path: str) -> None:
        """从回收站恢复一个系列。与 Rust restore_trash 一致。"""
        trash_dir = Path(trash_path)
        if not trash_dir.is_dir():
            raise LookupError("回收站条目不存在。")
        series_id = self._parse_trash_series_id(trash_dir.name)
        if series_id is None:
            raise ValueError("无法从回收站目录名解析系列 ID。")
        _safe_series_id(series_id)  # 校验格式
        target_dir = self.series_root / series_id
        if target_dir.exists():
            raise FileExistsError(f"系列“{series_id}”的目录已存在，请先处理冲突。")
        # 注册表检查
        registry = self._read_registry()
        if any(item.get("series_id") == series_id for item in registry["series"]):
            raise FileExistsError(f"系列“{series_id}”已在注册表中，无法恢复。")
        # 移回 series/ 目录
        shutil.move(str(trash_dir), str(target_dir))
        # 重新注册
        registry["series"].append({
            "series_id": series_id,
            "name": series_id,
            "description": "从回收站恢复",
            "color": "#a90000",
            "is_default": False,
            "status": "active",
            "relative_path": f"series/{series_id}",
            "created_at": local_now_iso(),
            "updated_at": local_now_iso(),
            "preferences": {
                "smart_model": "",
                "memory_model": "",
                "allow_cross_series_search": False,
                "require_intake_review": True,
                "video_default_ingest": True,
                "asset_policy": "copy_and_extract",
            },
        })
        self._write_registry(registry)

    def clean_trash(self) -> None:
        """清空回收站，永久删除 series- 前缀的已备份内容。与 Rust clean_trash 一致（R92 前缀过滤）。"""
        if not self.trash_root.is_dir():
            return
        for entry in self.trash_root.iterdir():
            if not entry.name.startswith("series-"):
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()

    def series_path(self, series_id: str) -> Path:
        safe_id = _safe_series_id(series_id)
        path = (self.series_root / safe_id).resolve()
        path.relative_to(self.series_root.resolve())
        return path

    def replay_library(self, series_id: str) -> ReplayLibrary:
        current = self.get_series(series_id)
        return ReplayLibrary(self.series_path(current.series_id), series_id=current.series_id)

    def overview(self, series_id: str) -> dict[str, object]:
        series = self.get_series(series_id)
        stats = self.stats(series_id)
        return {
            "series": series.model_dump(mode="json"),
            "active": self.active_series().series_id == series.series_id,
            "stats": stats.model_dump(mode="json"),
            "migration": self.migration_status(),
        }

    def create_intake(
        self,
        series_id: str,
        request: CreateIntakeRequest,
        *,
        warnings: list[str] | None = None,
    ) -> IntakeItem:
        self.get_series(series_id)
        asset_ids = _unique(request.asset_ids)
        asset_text, asset_warnings = self._intake_asset_context(series_id, asset_ids)
        raw_text = request.raw_text.strip()
        title = request.title.strip() or _derive_title(raw_text or asset_text or request.summary or request.type)
        item = IntakeItem(
            series_id=series_id,
            type=request.type,
            title=title,
            raw_text=raw_text,
            asset_text=asset_text,
            structured_text=request.structured_text.strip(),
            summary=request.summary.strip(),
            tags=_unique(request.tags),
            links=_unique(request.links),
            source=request.source.strip(),
            asset_ids=asset_ids,
            suggested_report_type=request.suggested_report_type,
            suggested_actions=_unique(request.suggested_actions),
            created_by=request.created_by,
            warnings=_unique([*(warnings or []), *asset_warnings]),
        )
        return self._save_intake(item)

    def list_intake(
        self,
        series_id: str,
        *,
        status: str | None = None,
        item_type: str | None = None,
        query: str = "",
    ) -> list[IntakeItem]:
        self.get_series(series_id)
        if status and status not in INTAKE_STATUSES:
            raise ValueError("待整理状态无效。")
        values: list[IntakeItem] = []
        statuses = [status] if status else list(INTAKE_STATUSES)
        for state in statuses:
            directory = self.series_path(series_id) / "intake" / str(state)
            if not directory.is_dir():
                continue
            for path in directory.glob("*.json"):
                try:
                    values.append(IntakeItem.model_validate_json(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        if item_type:
            values = [item for item in values if item.type == item_type]
        terms = _keywords(query)
        if terms:
            values = [
                item
                for item in values
                if all(
                    term.lower() in " ".join(
                        [item.title, item.raw_text, item.asset_text, item.summary, *item.tags]
                    ).lower()
                    for term in terms
                )
            ]
        return sorted(values, key=lambda item: item.updated_at, reverse=True)

    def get_intake(self, series_id: str, intake_id: str) -> IntakeItem:
        safe_intake_id = _safe_id(intake_id)
        for state in INTAKE_STATUSES:
            path = self.series_path(series_id) / "intake" / state / f"{safe_intake_id}.json"
            if path.is_file():
                item = IntakeItem.model_validate_json(path.read_text(encoding="utf-8"))
                if item.series_id != series_id:
                    raise PermissionError("待整理项不属于当前系列。")
                return item
        raise LookupError("待整理项不存在。")

    def update_intake(self, series_id: str, intake_id: str, request: UpdateIntakeRequest) -> IntakeItem:
        current = self.get_intake(series_id, intake_id)
        if current.status in {"merged", "rejected"}:
            raise ValueError("当前状态的待整理项不能编辑。")
        values = request.model_dump(exclude_none=True)
        for field in ("title", "raw_text", "structured_text", "summary"):
            if field in values:
                values[field] = str(values[field]).strip()
        for field in ("tags", "links", "asset_ids", "suggested_actions"):
            if field in values:
                values[field] = _unique(list(values[field]))
        updated = current.model_copy(update={**values, "updated_at": local_now_iso()})
        return self._save_intake(updated, previous_status=current.status)

    def approve_intake(self, series_id: str, intake_id: str) -> IntakeItem:
        return self._transition_intake(series_id, intake_id, "approved")

    def reject_intake(self, series_id: str, intake_id: str) -> IntakeItem:
        return self._transition_intake(series_id, intake_id, "rejected")

    def promote_intake(self, series_id: str, intake_id: str) -> IntakeItem:
        current = self.get_intake(series_id, intake_id)
        if current.status != "approved":
            raise ValueError("待整理项确认后才能正式入库。")
        asset_text, asset_warnings = self._intake_asset_context(series_id, current.asset_ids)
        current = current.model_copy(
            update={
                "asset_text": asset_text,
                "warnings": _unique([*current.warnings, *asset_warnings]),
            }
        )
        item_type = {
            "quick_note": "note",
            "asset": "clip",
            "image": "clip",
            "file": "clip",
            "video_summary": "video",
            "report_candidate": "note",
            "memory_capture": "question",
            "manual": "note",
            "clip": "clip",
            "thought": "thought",
            "action": "action",
            "question": "question",
        }[current.type]
        content = current.structured_text or _intake_source_text(current) or current.summary
        if not content.strip() and not current.asset_ids:
            raise ValueError("待整理项没有可入库内容。")
        knowledge = KnowledgeItem(
            series_id=series_id,
            type=item_type,
            title=current.title or _derive_title(content),
            content=content,
            summary=current.summary or _summary(content),
            tags=current.tags,
            source=KnowledgeSource(kind="file" if current.asset_ids else "manual"),
            links=KnowledgeLinks(asset_ids=current.asset_ids),
            status=KnowledgeStatus(),
            index=KnowledgeIndex(available=bool(content.strip()), keywords=_keywords(" ".join([current.title, content, *current.tags]))),
        )
        saved = self.replay_library(series_id).save_item(knowledge)
        for asset_id in current.asset_ids:
            self.attach_asset(series_id, saved.id, asset_id)
        promoted = current.model_copy(
            update={
                "status": "merged",
                "knowledge_item_id": saved.id,
                "reviewed_at": current.reviewed_at or local_now_iso(),
                "updated_at": local_now_iso(),
            }
        )
        return self._save_intake(promoted, previous_status=current.status)

    def batch_intake(self, series_id: str, intake_ids: list[str], action: str) -> list[IntakeItem]:
        handlers = {
            "approve": self.approve_intake,
            "reject": self.reject_intake,
            "promote": self.promote_intake,
        }
        if action not in handlers:
            raise ValueError("批量动作无效。")
        result: list[IntakeItem] = []
        for intake_id in dict.fromkeys(intake_ids):
            result.append(handlers[action](series_id, intake_id))
        return result


    def organization_snapshot(self, series_id: str, intake_id: str) -> tuple[IntakeItem, str]:
        """Freeze the model-visible Intake and its compare-and-apply baseline."""
        current = self.get_intake(series_id, intake_id)
        if current.status not in {"pending", "reviewing", "failed"}:
            raise ValueError("只有待整理、编辑中或整理失败的内容可以执行 AI 整理。")
        asset_text, asset_warnings = self._intake_asset_context(series_id, current.asset_ids)
        snapshot = current.model_copy(
            update={
                "asset_text": asset_text,
                "warnings": _unique([*current.warnings, *asset_warnings]),
            }
        )
        if not (_intake_source_text(snapshot) or snapshot.structured_text.strip()):
            raise ValueError("当前待整理项没有可整理的文本。")
        return snapshot, current.revision

    def mark_intake_organization_failed(
        self,
        series_id: str,
        intake_id: str,
        *,
        expected_revision: str | None = None,
    ) -> IntakeItem:
        current = self.get_intake(series_id, intake_id)
        if current.status not in {"pending", "reviewing", "failed"}:
            return current
        baseline_revision = current.revision
        if expected_revision is not None and expected_revision != baseline_revision:
            raise IntakeRevisionConflictError("待整理项已被更新，请刷新后重试。")
        failed = current.model_copy(
            update={
                "status": "failed",
                "warnings": _unique([*current.warnings, ORGANIZE_FAILURE_WARNING]),
                "updated_at": local_now_iso(),
            }
        )
        return self._save_intake(
            failed,
            previous_status=current.status,
            expected_revision=baseline_revision,
        )

    def read_report_markdown(self, series_id: str, report_id: str) -> ReportMarkdownDocument:
        library = self.replay_library(series_id)
        files = library.report_files(report_id)
        if files is None or not files["markdown"].is_file():
            raise LookupError("报告 Markdown 不存在。")
        markdown = files["markdown"].read_text(encoding="utf-8")
        return ReportMarkdownDocument(
            series_id=series_id,
            report_id=report_id,
            markdown=markdown,
            revision=_revision(markdown),
            updated_at=datetime.fromtimestamp(files["markdown"].stat().st_mtime).astimezone().isoformat(timespec="seconds"),
        )

    def write_report_markdown(
        self,
        series_id: str,
        report_id: str,
        markdown: str,
        *,
        base_revision: str = "",
    ) -> ReportMarkdownDocument:
        current = self.read_report_markdown(series_id, report_id)
        if base_revision and base_revision != current.revision:
            raise RuntimeError("报告已被其他操作修改，请重新载入后保存。")
        path = self.replay_library(series_id).report_files(report_id)["markdown"]  # type: ignore[index]
        history = self.series_path(series_id) / "reports" / "history" / _safe_id(report_id)
        history.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        _atomic_text(history / f"{stamp}.md", current.markdown)
        normalized = markdown.rstrip() + "\n"
        _atomic_text(path, normalized)
        return self.read_report_markdown(series_id, report_id)



    def import_asset(
        self,
        series_id: str,
        *,
        filename: str,
        media_type: str,
        data: bytes,
        create_intake: bool = True,
    ) -> tuple[AssetMetadata, IntakeItem | None]:
        self.get_series(series_id)
        if len(data) > MAX_ASSET_BYTES:
            raise ValueError("附件超过 50 MiB 限制。")
        safe_name = _safe_filename(filename)
        digest = hashlib.sha256(data).hexdigest()
        existing = next((item for item in self.list_assets(series_id) if item.sha256 == digest), None)
        if existing is not None:
            return existing, None
        resolved_type = media_type.strip() or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        category = "images" if resolved_type.startswith("image/") else "files"
        asset_id = f"asset_{uuid4().hex}"
        directory = self.series_path(series_id) / "assets" / category
        directory.mkdir(parents=True, exist_ok=True)
        stored_name = f"{asset_id}-{safe_name}"
        path = directory / stored_name
        path.write_bytes(data)
        warnings: list[str] = []
        extracted_path = ""
        extracted_text = ""
        extraction_status = "unknown"
        if _is_text_asset(resolved_type, safe_name):
            try:
                text = data.decode("utf-8")
                extracted_text = text
                extract = self.series_path(series_id) / "assets" / "extracted" / f"{asset_id}.txt"
                _atomic_text(extract, text)
                extracted_path = extract.relative_to(self.series_path(series_id)).as_posix()
                extraction_status = "extracted"
            except UnicodeDecodeError:
                warnings.append("文本附件不是 UTF-8，未提取正文。")
                extraction_status = "unsupported"
        elif resolved_type == "application/pdf":
            warnings.append("PDF OCR 暂未启用，已安全保存原文件。")
            extraction_status = "unsupported"
        elif resolved_type.startswith("image/"):
            warnings.append("图片 OCR 暂未启用，已安全保存原图。")
            extraction_status = "unsupported"
        else:
            extraction_status = "unsupported"
        metadata = AssetMetadata(
            asset_id=asset_id,
            series_id=series_id,
            filename=safe_name,
            media_type=resolved_type,
            size=len(data),
            sha256=digest,
            relative_path=path.relative_to(self.series_path(series_id)).as_posix(),
            extracted_text_path=extracted_path,
            extraction_status=extraction_status,
            warnings=warnings,
        )
        metadata_dir = self.series_path(series_id) / "assets" / "metadata"
        _atomic_json(metadata_dir / f"{asset_id}.json", metadata.model_dump(mode="json"))
        intake = None
        if create_intake:
            intake = self.create_intake(
                series_id,
                CreateIntakeRequest(
                    type="image" if resolved_type.startswith("image/") else "file",
                    title=safe_name,
                    raw_text="",
                    summary=_summary(extracted_text) if extracted_text else "",
                    source="asset-import",
                    asset_ids=[asset_id],
                ),
            )
            if warnings:
                intake = intake.model_copy(update={"warnings": warnings, "updated_at": local_now_iso()})
                intake = self._save_intake(intake, previous_status=intake.status)
        return metadata, intake

    def list_assets(self, series_id: str) -> list[AssetMetadata]:
        self.get_series(series_id)
        directory = self.series_path(series_id) / "assets" / "metadata"
        if not directory.is_dir():
            return []
        values: list[AssetMetadata] = []
        for path in directory.glob("*.json"):
            try:
                metadata = AssetMetadata.model_validate_json(path.read_text(encoding="utf-8"))
                if metadata.series_id == series_id:
                    values.append(metadata)
            except (OSError, ValueError):
                continue
        return sorted(values, key=lambda item: item.created_at, reverse=True)

    def get_asset(self, series_id: str, asset_id: str) -> AssetMetadata:
        path = self.series_path(series_id) / "assets" / "metadata" / f"{_safe_id(asset_id)}.json"
        if not path.is_file():
            raise LookupError("附件不存在。")
        metadata = AssetMetadata.model_validate_json(path.read_text(encoding="utf-8"))
        if metadata.series_id != series_id:
            raise PermissionError("附件不属于当前系列。")
        return metadata

    def update_asset_metadata(
        self, series_id: str, asset_id: str, request: UpdateAssetMetadataRequest,
    ) -> AssetMetadata:
        """R79: 更新附件元数据（当前仅支持 manual_summary）。"""
        current = self.get_asset(series_id, asset_id)
        values: dict[str, object] = {}
        if request.manual_summary is not None:
            val = str(request.manual_summary).strip()
            if len(val) > 1000:
                val = val[:1000]
            values["manual_summary"] = val
        if not values:
            return current  # 无更新字段
        updated = current.model_copy(update=values)
        _atomic_json(
            self.series_path(series_id) / "assets" / "metadata" / f"{_safe_id(asset_id)}.json",
            updated.model_dump(mode="json"),
        )
        return updated

    def asset_file(self, series_id: str, asset_id: str) -> Path:
        metadata = self.get_asset(series_id, asset_id)
        root = self.series_path(series_id)
        path = (root / metadata.relative_path).resolve()
        path.relative_to(root)
        if not path.is_file():
            raise LookupError("附件文件不存在。")
        return path

    def delete_asset(self, series_id: str, asset_id: str, *, confirm: bool) -> None:
        if not confirm:
            raise ValueError("删除附件需要明确确认。")
        metadata = self.get_asset(series_id, asset_id)
        if metadata.knowledge_item_ids:
            raise ValueError("附件已关联知识条目，请先解除关联。")
        path = self.asset_file(series_id, asset_id)
        path.unlink(missing_ok=True)
        if metadata.extracted_text_path:
            extracted = (self.series_path(series_id) / metadata.extracted_text_path).resolve()
            extracted.relative_to(self.series_path(series_id))
            extracted.unlink(missing_ok=True)
        meta_path = self.series_path(series_id) / "assets" / "metadata" / f"{asset_id}.json"
        meta_path.unlink(missing_ok=True)

    def attach_asset(self, series_id: str, item_id: str, asset_id: str) -> AssetMetadata:
        library = self.replay_library(series_id)
        item = library.get_item(item_id)
        if item is None:
            raise LookupError("知识条目不存在。")
        metadata = self.get_asset(series_id, asset_id)
        if asset_id not in item.links.asset_ids:
            item.links.asset_ids.append(asset_id)
            item.updated_at = local_now_iso()
            library.save_item(item)
        if item.id not in metadata.knowledge_item_ids:
            metadata.knowledge_item_ids.append(item.id)
            _atomic_json(
                self.series_path(series_id) / "assets" / "metadata" / f"{asset_id}.json",
                metadata.model_dump(mode="json"),
            )
        return metadata

    def memory_session(self, series_id: str) -> MemorySession:
        self.get_series(series_id)
        path = self.series_path(series_id) / "memory" / "remember.json"
        if not path.is_file():
            session = MemorySession(series_id=series_id)
            _atomic_json(path, session.model_dump(mode="json"))
            return session
        session = MemorySession.model_validate_json(path.read_text(encoding="utf-8"))
        if session.series_id != series_id:
            raise PermissionError("回忆会话不属于当前系列。")
        return session

    def ask_memory(
        self,
        series_id: str,
        request: MemoryQuestionRequest,
        *,
        gateway: TextMergeGateway | None = None,
    ) -> MemoryAnswer:
        current = self.get_series(series_id)
        if request.cross_series and not current.preferences.allow_cross_series_search:
            raise ValueError("当前系列未开启跨系列检索。")
        target_series = self.list_series() if request.cross_series else [current]
        answers: list[tuple[str, MemoryAnswer]] = []
        for target in target_series:
            payload = request.model_copy(update={"series_id": target.series_id, "cross_series": False, "persist": False})
            answer = MemoryQAService(self.replay_library(target.series_id)).ask(payload)
            for reference in answer.references:
                reference.series_id = target.series_id
            if answer.evidence_sufficient:
                answers.append((target.name, answer))
        if not answers:
            result = MemoryAnswer(
                answer="当前系列资料库中没有找到足够证据。",
                scope="all" if request.cross_series else request.scope if request.scope != "auto" else "all",
                scope_value=request.scope_value,
                evidence_sufficient=False,
            )
        elif len(answers) == 1:
            result = answers[0][1]
        else:
            references = [reference for _, answer in answers for reference in answer.references]
            result = MemoryAnswer(
                answer="\n\n".join(f"## {name}\n\n{answer.answer}" for name, answer in answers),
                scope="all",
                matched_count=sum(answer.matched_count for _, answer in answers),
                evidence_sufficient=True,
                references=references,
            )
        used_model = False
        model_attempted = False
        session = self.memory_session(series_id)
        session.messages.extend(
            [
                MemoryMessage(role="user", content=request.question),
                MemoryMessage(
                    role="assistant",
                    content=result.answer,
                    sources=result.references,
                    tool_calls=[
                        {
                            "name": "series_retrieval",
                            "arguments": {
                                "series_ids": [item.series_id for item in target_series],
                                "cross_series": request.cross_series,
                            },
                            "result_count": result.matched_count,
                            "model_synthesis": used_model,
                            "prompt_version": (
                                REPLAY_MEMORY_BOOK_PROMPT_VERSION if model_attempted else "local-retrieval-only"
                            ),
                        }
                    ],
                ),
            ]
        )
        session.updated_at = local_now_iso()
        _atomic_json(
            self.series_path(series_id) / "memory" / "remember.json",
            session.model_dump(mode="json"),
        )
        return result

    def _record_prompt_trace(
        self,
        series_id: str,
        *,
        operation: str,
        prompt_version: str,
        outcome: str,
        input_text: str,
    ) -> None:
        """Persist bounded replay metadata without retaining private prompt bodies."""

        path = self.series_path(series_id) / "stats" / "prompt-contract-trace.json"
        entry = {
            "operation": operation,
            "prompt_version": prompt_version,
            "outcome": outcome,
            "input_chars": len(input_text),
            "input_sha256": hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
            "recorded_at": local_now_iso(),
        }
        try:
            with self._lock:
                entries: list[dict[str, Any]] = []
                if path.is_file():
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(payload, dict) and isinstance(payload.get("entries"), list):
                        entries = [item for item in payload["entries"] if isinstance(item, dict)][-199:]
                entries.append(entry)
                _atomic_json(path, {"schema_version": "1.0", "entries": entries})
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            # Observability must never turn a successful review/fallback operation into
            # a partial product failure. The user-visible artifact remains authoritative.
            return

    def new_memory_session(self, series_id: str) -> MemorySession:
        session = MemorySession(series_id=series_id)
        _atomic_json(
            self.series_path(series_id) / "memory" / "remember.json",
            session.model_dump(mode="json"),
        )
        return session

    def stats(
        self,
        series_id: str,
        *,
        range_name: str = "all",
        start_date: str = "",
        end_date: str = "",
    ) -> SeriesStats:
        library = self.replay_library(series_id)
        bounds = _date_bounds(range_name, start_date=start_date, end_date=end_date)
        items = [item for item in library.list_items() if _within_dates(item.created_at, bounds)]
        reports = [item for item in library.list_reports() if _within_dates(item.created_at, bounds)]
        intake = [item for item in self.list_intake(series_id) if _within_dates(item.created_at, bounds)]
        assets = [item for item in self.list_assets(series_id) if _within_dates(item.created_at, bounds)]
        tasks = [item for item in library.list_tasks() if _within_dates(item.created_at, bounds)]
        heatmap = Counter(item.created_at[:10] for item in items if len(item.created_at) >= 10)
        dates = sorted(date.fromisoformat(value) for value in heatmap)
        streak = _streak_days(dates)
        today = date.today()
        today_text = today.isoformat()
        week_start = today - timedelta(days=today.weekday())
        video_count, today_video_count = self._video_activity(series_id, today_text, bounds)
        counts = {
            "summary_count": len(reports),
            "record_count": len(items),
            "daily_reports": len([item for item in reports if item.type == "daily"]),
            "weekly_reports": len([item for item in reports if item.type == "weekly"]),
            "monthly_reports": len([item for item in reports if item.type == "monthly"]),
            "yearly_reports": len([item for item in reports if item.type == "yearly"]),
            "app_launches": _read_launch_count(self.series_path(series_id)),
            "video_count": video_count,
            "today_video_count": today_video_count,
            "asset_count": len(assets),
            "knowledge_item_count": len(items),
            "today_records": len([item for item in items if item.created_at[:10] == today_text]),
            "week_records": len(
                [
                    item
                    for item in items
                    if len(item.created_at) >= 10 and date.fromisoformat(item.created_at[:10]) >= week_start
                ]
            ),
            "today_intake": len([item for item in intake if item.created_at[:10] == today_text]),
            "intake_pending": len([item for item in intake if item.status in {"pending", "reviewing"}]),
            "intake_approved": len([item for item in intake if item.status in {"approved", "merged"}]),
            "intake_rejected": len([item for item in intake if item.status == "rejected"]),
            "task_count": len(tasks),
            "series_count": len(self.list_series(include_archived=True)),
        }
        tokens = _read_token_usage(self.series_path(series_id))
        return SeriesStats(
            series_id=series_id,
            range=range_name,
            counts=counts,
            tokens=tokens,
            heatmap=dict(sorted(heatmap.items())),
            last_recorded_at=items[0].created_at if items else "",
            streak_days=streak,
        )

    def _video_activity(
        self,
        series_id: str,
        today_text: str,
        bounds: tuple[date | None, date | None],
    ) -> tuple[int, int]:
        series_root = self.series_path(series_id)
        paths = list((series_root / "videos").rglob("record.json"))
        if series_id == "default":
            mapping = series_root / "videos" / "legacy-map.json"
            if mapping.is_file():
                try:
                    values = json.loads(mapping.read_text(encoding="utf-8")).get("records", [])
                    paths.extend(self.library_root / str(value) for value in values if isinstance(value, str))
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
        total = 0
        today_count = 0
        for path in dict.fromkeys(paths):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            record_series = str(payload.get("series_id") or "default")
            if record_series != series_id:
                continue
            imported_at = str(payload.get("imported_at") or "")
            if not _within_dates(imported_at, bounds):
                continue
            total += 1
            if imported_at[:10] == today_text:
                today_count += 1
        return total, today_count

    def global_overview(self) -> dict[str, object]:
        series = self.list_series(include_archived=True)
        return {
            "series_count": len(series),
            "active_series_id": self.active_series().series_id,
            "knowledge_item_count": sum(self.stats(item.series_id).counts["knowledge_item_count"] for item in series),
            "asset_count": sum(self.stats(item.series_id).counts["asset_count"] for item in series),
            "intake_pending": sum(self.stats(item.series_id).counts["intake_pending"] for item in series),
        }

    def migrate_legacy(self) -> dict[str, object]:
        self.ensure_initialized()
        state = self.migration_status()
        if state.get("status") == "completed":
            return state
        default_root = self.series_path("default")
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        backup_root = self.global_root / "migrations" / stamp / "legacy-metadata"
        copied = 0
        mappings = {
            self.library_root / "index" / "items": default_root / "knowledge" / "items",
            self.library_root / "index" / "reports": default_root / "knowledge" / "indexes" / "reports",
            self.library_root / "index" / "tasks": default_root / "stats" / "tasks",
            self.library_root / "reports": default_root / "reports",
            self.library_root / "journals": default_root / "journals",
            self.library_root / "inbox": default_root / "intake" / "legacy",
        }
        for source, target in mappings.items():
            if source.is_dir():
                copied += _copy_metadata_tree(source, backup_root / source.name)
                copied += _copy_metadata_tree(source, target)
        legacy_index = self.library_root / "index"
        for name in ("search.json", "timeline.json", "reports.json"):
            source = legacy_index / name
            if source.is_file():
                target = default_root / "knowledge" / "indexes" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copy2(source, target)
                    copied += 1
        video_records = [
            path.relative_to(self.library_root).as_posix()
            for path in self.library_root.rglob("record.json")
            if self.series_root not in path.parents and self.global_root not in path.parents
        ]
        _atomic_json(default_root / "videos" / "legacy-map.json", {"series_id": "default", "records": video_records})
        result = {
            "status": "completed",
            "series_id": "default",
            "copied_metadata_files": copied,
            "legacy_video_records": len(video_records),
            "backup_relative_path": backup_root.relative_to(self.library_root).as_posix(),
            "completed_at": local_now_iso(),
        }
        _atomic_json(self.global_root / "migrations" / "series-migration.json", result)
        return result

    def migration_status(self) -> dict[str, object]:
        path = self.global_root / "migrations" / "series-migration.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        legacy_dirs = [name for name in ("index", "reports", "journals", "inbox") if (self.library_root / name).exists()]
        return {"status": "pending" if legacy_dirs else "not_required", "legacy_sources": legacy_dirs}

    def _resolve_incoming_items(
        self,
        series_id: str,
        values: list[str | dict[str, Any]],
    ) -> tuple[list[str], str, list[str]]:
        sources: list[str] = []
        blocks: list[str] = []
        asset_references: list[str] = []
        for value in values:
            if isinstance(value, str):
                intake = self.get_intake(series_id, value)
                sources.append(intake.intake_id)
                attachment_blocks, references = self._report_asset_blocks(series_id, intake.asset_ids)
                asset_references.extend(references)
                blocks.append(
                    "\n".join(
                        [
                            f"### {intake.title}",
                            intake.structured_text or intake.raw_text or intake.summary,
                            f"标签：{', '.join(intake.tags)}" if intake.tags else "",
                            *attachment_blocks,
                        ]
                    ).strip()
                )
            elif isinstance(value, dict):
                supplied_series = str(value.get("series_id") or series_id)
                if supplied_series != series_id:
                    raise PermissionError("待加入内容不属于当前系列。")
                source = str(value.get("intake_id") or value.get("id") or f"inline_{len(sources) + 1}")
                sources.append(source)
                blocks.append(json.dumps(value, ensure_ascii=False, indent=2))
        return sources, "\n\n".join(block for block in blocks if block.strip()), _unique(asset_references)

    def _intake_asset_context(self, series_id: str, asset_ids: list[str]) -> tuple[str, list[str]]:
        blocks: list[str] = []
        warnings: list[str] = []
        remaining = MAX_INTAKE_ASSET_TEXT_CHARS
        series_root = self.series_path(series_id)
        for asset_id in asset_ids:
            metadata = self.get_asset(series_id, asset_id)
            warnings.extend(metadata.warnings)
            if remaining <= 0:
                if metadata.extracted_text_path or metadata.manual_summary:
                    warnings.append(
                        f"{metadata.filename} 的正文未加入待整理预览；原文件和完整提取文本仍保留。"
                    )
                continue
            details = metadata.manual_summary.strip()
            truncated = False
            if not details:
                details, truncated = _read_asset_text(
                    series_root,
                    metadata.extracted_text_path,
                    min(MAX_REPORT_ASSET_TEXT_CHARS, remaining),
                )
            if not details:
                continue
            visible = details[:remaining]
            blocks.append(f"## 附件正文：{metadata.filename}\n\n{visible}")
            remaining -= len(visible)
            if truncated or len(details) > len(visible):
                warnings.append(
                    f"{metadata.filename} 正文较长，待整理预览仅保留部分内容；原文件和完整提取文本仍保留。"
                )
        return "\n\n".join(blocks), _unique(warnings)

    def _report_asset_blocks(self, series_id: str, asset_ids: list[str]) -> tuple[list[str], list[str]]:
        blocks: list[str] = []
        references: list[str] = []
        series_root = self.series_path(series_id)
        for asset_id in _unique(asset_ids):
            metadata = self.get_asset(series_id, asset_id)
            asset_path = self.asset_file(series_id, asset_id)
            relative_path = asset_path.relative_to(series_root).as_posix()
            reference = _report_asset_reference(metadata, relative_path)
            references.append(reference)
            extracted_text = _read_report_asset_text(series_root, metadata.extracted_text_path)
            details = metadata.manual_summary or extracted_text
            blocks.append(
                "\n".join(
                    [
                        f"#### 附件：{metadata.filename}",
                        reference,
                        details,
                    ]
                ).strip()
            )
        return blocks, references

    def _transition_intake(self, series_id: str, intake_id: str, status: str) -> IntakeItem:
        current = self.get_intake(series_id, intake_id)
        if current.status in {"merged", "archived"}:
            raise ValueError("当前状态不能执行该操作。")
        updated = current.model_copy(
            update={
                "status": status,
                "reviewed_at": local_now_iso(),
                "updated_at": local_now_iso(),
            }
        )
        return self._save_intake(updated, previous_status=current.status)

    def _save_intake(
        self,
        item: IntakeItem,
        *,
        previous_status: str | None = None,
        expected_revision: str | None = None,
    ) -> IntakeItem:
        if item.series_id != _safe_series_id(item.series_id):
            raise ValueError("待整理项 series_id 无效。")
        safe_intake_id = _safe_id(item.intake_id)
        with self._lock:
            series_path = self.series_path(item.series_id)
            existing_path = next(
                (
                    series_path / "intake" / status / f"{safe_intake_id}.json"
                    for status in INTAKE_STATUSES
                    if (series_path / "intake" / status / f"{safe_intake_id}.json").is_file()
                ),
                None,
            )
            if expected_revision is not None:
                if existing_path is None:
                    raise IntakeRevisionConflictError("待整理项已不存在，请刷新后重试。")
                existing = IntakeItem.model_validate_json(existing_path.read_text(encoding="utf-8"))
                if existing.revision != expected_revision:
                    raise IntakeRevisionConflictError("待整理项已被更新，请刷新后重试。")
            saved = item.model_copy(update={"revision": item.content_revision()})
            path = series_path / "intake" / saved.status / f"{safe_intake_id}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json(path, saved.model_dump(mode="json"))
            if existing_path is not None and existing_path != path:
                existing_path.unlink(missing_ok=True)
            elif previous_status and previous_status != saved.status:
                old = series_path / "intake" / previous_status / f"{safe_intake_id}.json"
                if old != path:
                    old.unlink(missing_ok=True)
            return saved

    def list_report_history(self, series_id: str, report_id: str) -> list[dict[str, object]]:
        """列出报告的历史版本。"""
        self.get_series(series_id)
        history_dir = self.series_path(series_id) / "reports" / "history" / _safe_id(report_id)
        if not history_dir.is_dir():
            return []
        versions: list[dict[str, object]] = []
        for path in sorted(history_dir.glob("*.md"), reverse=True):
            stamp = path.stem
            try:
                stamp_dt = datetime.strptime(stamp, "%Y%m%d-%H%M%S-%f")
                label = stamp_dt.astimezone().strftime("%m-%d %H:%M")
            except ValueError:
                label = stamp
            versions.append({
                "version": stamp,
                "label": label,
                "size": path.stat().st_size,
            })
        return versions

    def restore_report_history(self, series_id: str, report_id: str, version: str) -> ReportMarkdownDocument:
        """从指定历史版本恢复报告 Markdown。"""
        _safe_id(version)
        current = self.read_report_markdown(series_id, report_id)
        history_path = self.series_path(series_id) / "reports" / "history" / _safe_id(report_id) / f"{version}.md"
        if not history_path.is_file():
            raise LookupError("指定历史版本不存在。")
        restored = history_path.read_text(encoding="utf-8")
        # write_report_markdown 会自动保存当前版本到历史再写入
        return self.write_report_markdown(series_id, report_id, restored, base_revision=current.revision)

    def check_integrity(self, series_id: str) -> dict[str, object]:
        """扫描一个系列的完整性和数据一致性。"""
        root = self.series_path(series_id)
        issues: list[dict[str, str]] = []
        checked = {"assets": 0, "reports": 0}

        # —— 附件检查 ——
        meta_dir = root / "assets" / "metadata"
        image_dir = root / "assets" / "images"
        file_dir = root / "assets" / "files"
        extracted_dir = root / "assets" / "extracted"

        if meta_dir.is_dir():
            meta_ids: set[str] = set()
            for meta_file in meta_dir.glob("*.json"):
                meta_ids.add(meta_file.stem)
                try:
                    metadata = json.loads(meta_file.read_text("utf-8"))
                    checked["assets"] += 1
                    rel = metadata.get("relative_path", "")
                    target = (root / rel).resolve()
                    target.relative_to(root)
                    if not target.is_file():
                        issues.append({
                            "type": "asset_missing_file",
                            "id": metadata.get("asset_id", meta_file.stem),
                            "detail": f"元数据 {meta_file.name} 引用的文件不存在: {rel}",
                        })
                except (OSError, ValueError, json.JSONDecodeError) as e:
                    issues.append({
                        "type": "asset_corrupt_metadata",
                        "id": meta_file.stem,
                        "detail": f"元数据文件无法解析: {e}",
                    })

            # 检查 images/ files/ 中的孤立二进制文件
            for subdir, label in [(image_dir, "images"), (file_dir, "files")]:
                if subdir.is_dir():
                    for f in subdir.iterdir():
                        if f.is_file() and f.suffix.lower() not in (".gitkeep", ".empty"):
                            stem = f.stem.split("-")[0]  # asset_id- 前缀
                            if stem not in meta_ids:
                                issues.append({
                                    "type": "orphan_asset_file",
                                    "id": f.name,
                                    "detail": f"孤立文件（无对应元数据）: {label}/{f.name}",
                                })

            # 检查 extracted/ 中的孤立文本
            if extracted_dir.is_dir():
                for f in extracted_dir.iterdir():
                    if f.is_file() and f.suffix == ".txt":
                        stem = f.stem
                        if stem not in meta_ids:
                            issues.append({
                                "type": "orphan_extracted_text",
                                "id": f.name,
                                "detail": f"孤立提取文本（无对应元数据）: extracted/{f.name}",
                            })

        # —— 报告检查 ——
        reports_dir = root / "reports"
        if reports_dir.is_dir():
            for report_type in ("daily", "weekly", "monthly", "yearly"):
                type_dir = reports_dir / report_type
                if not type_dir.is_dir():
                    continue
                slugs: set[str] = set()
                for f in type_dir.iterdir():
                    if f.suffix == ".json":
                        slugs.add(f.stem)
                    elif f.suffix == ".md":
                        slugs.add(f.stem)
                for slug in sorted(slugs):
                    checked["reports"] += 1
                    md_path = type_dir / f"{slug}.md"
                    json_path = type_dir / f"{slug}.json"
                    if not md_path.is_file() and json_path.is_file():
                        issues.append({
                            "type": "report_missing_markdown",
                            "id": f"{report_type}/{slug}",
                            "detail": f"报告有 JSON 元数据但缺少 Markdown 文件: {report_type}/{slug}.md",
                        })
                    if md_path.is_file() and not json_path.is_file():
                        issues.append({
                            "type": "report_missing_metadata",
                            "id": f"{report_type}/{slug}",
                            "detail": f"报告有 Markdown 文件但缺少 JSON 元数据: {report_type}/{slug}.json",
                        })

        # —— 汇总 ——
        summary = {
            "series_id": series_id,
            "total_checked": checked,
            "issues": issues,
            "issue_count": len(issues),
            "healthy": len(issues) == 0,
        }
        return summary

    def _ensure_series_structure(self, series_id: str) -> None:
        root = self.series_path(series_id)
        paths = [
            root / "reports" / report_type for report_type in ("daily", "weekly", "monthly", "yearly", "history")
        ]
        paths.extend(root / "intake" / status for status in INTAKE_STATUSES)
        paths.extend(
            [
                root / "knowledge" / "items",
                root / "knowledge" / "chunks",
                root / "knowledge" / "indexes" / "reports",
                root / "assets" / "images",
                root / "assets" / "files",
                root / "assets" / "extracted",
                root / "assets" / "metadata",
                root / "videos",
                root / "memory",
                root / "stats" / "tasks",
                root / "journals",
            ]
        )
        for path in paths:
            path.mkdir(parents=True, exist_ok=True)
        remember = root / "memory" / "remember.json"
        if not remember.is_file():
            _atomic_json(remember, MemorySession(series_id=series_id).model_dump(mode="json"))

    def _save_series_file(self, series: Series) -> None:
        _atomic_json(self.series_path(series.series_id) / "series.json", series.model_dump(mode="json"))

    def _replace_series(self, series: Series) -> None:
        registry = self._read_registry()
        replaced = False
        values: list[dict[str, object]] = []
        for item in registry["series"]:
            if item.get("series_id") == series.series_id:
                values.append(series.model_dump(mode="json"))
                replaced = True
            else:
                values.append(item)
        if not replaced:
            raise LookupError("系列不存在。")
        registry["series"] = values
        self._write_registry(registry)
        self._save_series_file(series)

    def _read_registry(self) -> dict[str, Any]:
        payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        if not isinstance(payload.get("series"), list):
            raise ValueError("系列注册表损坏。")
        return payload

    def _write_registry(self, payload: dict[str, Any]) -> None:
        _atomic_json(self.registry_path, payload)


def _safe_series_id(value: str) -> str:
    normalized = value.strip().lower()
    if not SERIES_ID.fullmatch(normalized):
        raise ValueError("series_id 只能包含小写字母、数字和连字符。")
    return normalized


def _unique_series_id(name: str, existing: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        slug = f"series-{uuid4().hex[:8]}"
    slug = slug[:40].rstrip("-")
    candidate = slug
    counter = 2
    while candidate in existing:
        candidate = f"{slug[:40]}-{counter}"
        counter += 1
    return _safe_series_id(candidate)


def _validate_color(value: str) -> str:
    normalized = value.strip()
    if not HEX_COLOR.fullmatch(normalized):
        raise ValueError("系列颜色必须使用 #RRGGBB。")
    return normalized.lower()


def _safe_filename(value: str) -> str:
    name = Path(value).name.strip().replace("\x00", "")
    name = SAFE_FILENAME.sub("_", name).strip(" ._")
    if not name:
        name = "attachment"
    stem = Path(name).stem[:100].rstrip(" ._") or "attachment"
    suffix = Path(name).suffix[:16].lower()
    return f"{stem}{suffix}"


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _json_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return _unique([str(item) for item in value])


def _derive_title(value: str) -> str:
    first = next((line.strip() for line in value.splitlines() if line.strip()), "待整理内容")
    return re.sub(r"^#+\s*", "", first)[:80]


def _summary(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:240]


def _revision(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def _strip_markdown_fence(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        lines = normalized.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1]).strip()
    return normalized


def _report_asset_reference(metadata: AssetMetadata, relative_path: str) -> str:
    target = f"../../{relative_path}"
    if metadata.media_type.startswith("image/"):
        return f"- ![{metadata.filename}]({target})"
    return f"- [{metadata.filename}]({target})"


def _read_report_asset_text(series_root: Path, relative_path: str) -> str:
    return _read_asset_text(series_root, relative_path, MAX_REPORT_ASSET_TEXT_CHARS)[0]


def _read_asset_text(series_root: Path, relative_path: str, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        return "", False
    if not relative_path:
        return "", False
    allowed_root = (series_root / "assets" / "extracted").resolve()
    target = (series_root / relative_path).resolve()
    try:
        target.relative_to(allowed_root)
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            value = handle.read(limit + 1)
            return value[:limit], len(value) > limit
    except (OSError, ValueError):
        return "", False


def _intake_source_text(item: IntakeItem) -> str:
    raw_text = item.raw_text.strip()
    asset_text = item.asset_text.strip()
    if raw_text and asset_text:
        return f"## 用户原文\n\n{raw_text}\n\n{asset_text}"
    return raw_text or asset_text


def _insert_report_asset_references(markdown: str, references: list[str]) -> str:
    normalized = markdown.rstrip()
    existing_lines = {line.strip() for line in normalized.splitlines()}
    missing = [reference for reference in _unique(references) if reference not in existing_lines]
    if not missing:
        return normalized
    lines = normalized.splitlines()
    heading_index = next((index for index, line in enumerate(lines) if line.strip() == "## 附件"), None)
    if heading_index is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["## 附件", "", *missing])
    else:
        insert_at = heading_index + 1
        while insert_at < len(lines) and not lines[insert_at].strip():
            insert_at += 1
        lines[insert_at:insert_at] = [*missing, ""]
    return "\n".join(lines).rstrip()


def _missing_manual_lines(base: str, merged: str) -> list[str]:
    lines = [line.strip() for line in base.splitlines() if line.strip()]
    return [line for line in dict.fromkeys(lines) if line not in merged]


def _is_text_asset(media_type: str, filename: str) -> bool:
    return media_type.startswith("text/") or media_type in TEXT_MEDIA_TYPES or Path(filename).suffix.lower() in {
        ".csv",
        ".json",
        ".log",
        ".md",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }


def _date_bounds(
    range_name: str,
    *,
    start_date: str = "",
    end_date: str = "",
) -> tuple[date | None, date | None]:
    today = date.today()
    if range_name == "all":
        return None, None
    if range_name == "last_30_days":
        return today - timedelta(days=29), today
    if range_name == "last_month":
        first_this_month = today.replace(day=1)
        end = first_this_month - timedelta(days=1)
        return end.replace(day=1), end
    if range_name == "last_quarter":
        current_quarter_month = ((today.month - 1) // 3) * 3 + 1
        current_quarter_start = date(today.year, current_quarter_month, 1)
        end = current_quarter_start - timedelta(days=1)
        previous_quarter_month = ((end.month - 1) // 3) * 3 + 1
        return date(end.year, previous_quarter_month, 1), end
    if range_name == "custom":
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
        except ValueError as error:
            raise ValueError("自定义统计范围必须提供有效的 start 和 end 日期。") from error
        if start > end:
            raise ValueError("自定义统计范围的开始日期不能晚于结束日期。")
        return start, end
    raise ValueError("统计范围无效。")


def _within_dates(value: str, bounds: tuple[date | None, date | None]) -> bool:
    start, end = bounds
    if start is None and end is None:
        return True
    try:
        current = date.fromisoformat(value[:10])
    except ValueError:
        return False
    return (start is None or current >= start) and (end is None or current <= end)


def _streak_days(values: list[date]) -> int:
    if not values:
        return 0
    unique = sorted(set(values), reverse=True)
    streak = 1
    for previous, current in zip(unique, unique[1:]):
        if previous - current != timedelta(days=1):
            break
        streak += 1
    return streak


def _read_launch_count(series_root: Path) -> int:
    path = series_root / "stats" / "launches.json"
    if not path.is_file():
        return 0
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("count", 0)
        return int(value) if int(value) >= 0 else 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def _read_token_usage(series_root: Path) -> dict[str, int]:
    path = series_root / "stats" / "model-usage.json"
    defaults = {"input": 0, "output": 0, "cached": 0}
    if not path.is_file():
        return defaults
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {key: max(0, int(payload.get(key, 0))) for key in defaults}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return defaults


def _video_count(series_root: Path) -> int:
    local_records = len(list((series_root / "videos").rglob("record.json")))
    mapping = series_root / "videos" / "legacy-map.json"
    if not mapping.is_file():
        return local_records
    try:
        records = json.loads(mapping.read_text(encoding="utf-8")).get("records", [])
        return max(local_records, len(records) if isinstance(records, list) else 0)
    except (OSError, ValueError, json.JSONDecodeError):
        return local_records


def _copy_metadata_tree(source: Path, target: Path) -> int:
    copied = 0
    allowed = {".json", ".md", ".txt", ".toml"}
    for path in source.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in allowed:
            continue
        relative = path.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            continue
        shutil.copy2(path, destination)
        copied += 1
    return copied
