from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from backend.shared.filesystem import atomic_write_text
from backend.video_intake.models import LibraryRecord, utc_now_iso


RECORD_FILE = "record.json"
DATA_DIR = "data"


class LibraryStorage:
    def __init__(self, root_dir: Path, series_id: str = "default") -> None:
        self.library_root = root_dir / "library"
        self.series_id = series_id
        self.root_dir = self.library_root / "series" / series_id / "videos"

    def ensure_root(self) -> Path:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        index = self.root_dir / "README.md"
        if not index.exists():
            atomic_write_text(
                index,
                "# 视频阅览库\n\n"
                "每个视频单独保存在日期目录中。可以直接删除整个视频文件夹。\n\n"
                "- `内容整理.md`：分层总结与证据\n"
                "- `完整转写.md`：带时间戳全文\n"
                "- `个人笔记.md`：你的补充笔记\n"
                "- `media`：音频、视频和封面\n"
                "- `data`：供程序读取的结构化数据\n",
            )
        return self.root_dir

    def create_record(self, item, *, media_mode: str) -> tuple[LibraryRecord, Path]:
        self.ensure_root()
        record_id = item.bvid if item.page <= 1 else f"{item.bvid}-p{item.page}"
        existing = self.find_record(record_id)
        if existing is not None:
            path = self.record_dir(existing)
            existing.status = "preparing"
            existing.stage = "preparing"
            existing.progress = 0.0
            existing.error = ""
            existing.media_mode = media_mode
            self.save_record(existing)
            return existing, path

        now = datetime.now().astimezone()
        folder_name = f"{_safe_name(item.title, 72)} [{record_id}]"
        record_dir = self.root_dir / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}" / folder_name
        record_dir.mkdir(parents=True, exist_ok=True)
        (record_dir / "media").mkdir(exist_ok=True)
        (record_dir / DATA_DIR).mkdir(exist_ok=True)
        record = LibraryRecord(
            id=record_id,
            series_id=self.series_id,
            bvid=item.bvid,
            page=item.page,
            title=item.title,
            source_url=item.source_url,
            uploader=item.uploader,
            published_at=item.published_at,
            duration_seconds=item.duration_seconds,
            description=item.description,
            tags=item.tags,
            cover_url=item.cover_url,
            media_mode=media_mode,
            relative_dir=record_dir.relative_to(self.library_root).as_posix(),
        )
        self.save_record(record)
        self.write_readme(record)
        note_path = record_dir / record.note_file
        if not note_path.exists():
            atomic_write_text(note_path, f"# {record.title} · 个人笔记\n\n")
        return record, record_dir

    def list_records(self) -> list[LibraryRecord]:
        self.ensure_root()
        records: list[LibraryRecord] = []
        paths = list(self.root_dir.rglob(RECORD_FILE))
        if self.series_id == "default" and self.library_root.exists():
            paths.extend(
                path
                for path in self.library_root.rglob(RECORD_FILE)
                if self.root_dir not in path.parents and (self.library_root / "series") not in path.parents
            )
        for path in dict.fromkeys(paths):
            try:
                record = LibraryRecord.model_validate_json(path.read_text(encoding="utf-8"))
                if record.series_id != self.series_id:
                    continue
                _enrich_availability(record, path.parent)
                records.append(record)
            except (OSError, ValueError):
                continue
        return sorted(records, key=lambda item: item.imported_at, reverse=True)

    def find_record(self, record_id: str) -> LibraryRecord | None:
        return next((item for item in self.list_records() if item.id == record_id), None)

    def record_dir(self, record: LibraryRecord) -> Path:
        if record.series_id != self.series_id:
            raise ValueError("视频记录不属于当前系列。")
        path = (self.library_root / record.relative_dir).resolve()
        path.relative_to(self.library_root.resolve())
        if self.series_id != "default":
            path.relative_to(self.root_dir.resolve())
        return path

    def save_record(self, record: LibraryRecord) -> None:
        record_dir = self.record_dir(record)
        record_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            record_dir / RECORD_FILE,
            json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2),
        )

    def write_readme(self, record: LibraryRecord) -> None:
        record_dir = self.record_dir(record)
        status = {
            "preparing": "准备中",
            "processing": "处理中",
            "completed": "已完成",
            "failed": "处理失败",
        }.get(record.status, record.status)
        lines = [
            f"# {record.title}",
            "",
            f"- 状态：{status}",
            f"- 来源：{record.source_url}",
            f"- UP 主：{record.uploader or '未知'}",
            f"- 发布时间：{record.published_at or '未知'}",
            f"- 导入时间：{record.imported_at}",
            f"- 内容模式：{'保留 1080P 视频' if record.media_mode == 'video' else '仅保留音频'}",
            f"- 转写来源：{record.transcript_source}",
            f"- 画面重要性：{record.visual.importance}",
            "",
            "## 直接阅读",
            "",
            f"- [{record.summary_file}]({record.summary_file})",
            f"- [{record.transcript_file}]({record.transcript_file})",
            f"- [{record.note_file}]({record.note_file})",
            "",
            "## 简介",
            "",
            record.description or "暂无简介。",
            "",
        ]
        if record.error:
            lines.extend(["## 错误", "", record.error, ""])
        atomic_write_text(record_dir / "README.md", "\n".join(lines))

    def mark_progress(self, record: LibraryRecord, *, stage: str, progress: float) -> None:
        record.status = "processing"
        record.stage = stage
        record.progress = max(0.0, min(100.0, progress))
        self.save_record(record)

    def complete(self, record: LibraryRecord) -> None:
        record.status = "completed"
        record.stage = "completed"
        record.progress = 100.0
        record.error = ""
        self.save_record(record)
        self.write_readme(record)

    def fail(self, record: LibraryRecord, error: str) -> None:
        record.status = "failed"
        record.stage = "failed"
        record.error = error
        self.save_record(record)
        self.write_readme(record)

    def write_notes(self, record: LibraryRecord, content: str) -> None:
        path = self.record_dir(record) / record.note_file
        atomic_write_text(path, content.strip() + "\n")

    def read_notes(self, record: LibraryRecord) -> str:
        path = self.record_dir(record) / record.note_file
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def delete_records(self, record_ids: list[str]) -> list[str]:
        deleted: list[str] = []
        for record_id in dict.fromkeys(record_ids):
            record = self.find_record(record_id)
            if record is None:
                continue
            path = self.record_dir(record)
            path.relative_to(self.root_dir.resolve())
            if path.exists():
                trash_root = self.library_root / ".trash" / "records"
                trash_root.mkdir(parents=True, exist_ok=True)
                timestamp = int(time.time())
                trash_name = f"{record_id}-{timestamp}"
                trash_path = trash_root / trash_name
                counter = 1
                while trash_path.exists():
                    trash_path = trash_root / f"{trash_name}-{counter}"
                    counter += 1
                try:
                    shutil.move(str(path), str(trash_path))
                except OSError:
                    # 跨驱动器 move 失败时回退到 copy+delete
                    shutil.copytree(str(path), str(trash_path))
                    shutil.rmtree(path)
            deleted.append(record_id)
        return deleted


def _safe_name(value: str, limit: int) -> str:
    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" ._")
    normalized = re.sub(r"\s+", " ", normalized)
    return (normalized or "未命名视频")[:limit].rstrip(" .")


def _enrich_availability(record: LibraryRecord, record_dir: Path) -> None:
    data_dir = record_dir / DATA_DIR
    record.official_subtitle_available = (data_dir / "transcript.official.json").exists()
    record.asr_available = (data_dir / "transcript.asr.json").exists() or (
        data_dir / ".cache" / "whisper" / "transcript.raw.json"
    ).exists()
    record.cleaned_transcript_available = (data_dir / "transcript.cleaned.json").exists()
    record.summary_available = (data_dir / "summary.json").exists()
    record.visual_available = (data_dir / "visual-analysis.json").exists()
    record.index_available = (data_dir / "index.json").exists()
    record.export_available = (record_dir / record.summary_file).exists()
