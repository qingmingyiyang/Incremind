from __future__ import annotations

import json
import os
from pathlib import Path
import re
from threading import RLock
import time
from typing import Any
from uuid import uuid4

from backend.replay.contracts import KnowledgeItem, ReplayTask, Report
from backend.shared.filesystem import KeyedLockManager


SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_ATOMIC_WRITES = KeyedLockManager()


class ReplayLibrary:
    def __init__(self, root: Path, *, series_id: str | None = None) -> None:
        self.root = root.resolve()
        self.series_id = series_id
        self._lock = RLock()

    def ensure_structure(self) -> dict[str, Path]:
        index_root = self.root / "knowledge" / "indexes" if self.series_id else self.root / "index"
        paths = {
            "videos": self.root / "videos",
            "inbox": self.root / ("intake" if self.series_id else "inbox"),
            "inbox_raw": self.root / ("intake/legacy" if self.series_id else "inbox/raw"),
            "journals": self.root / "journals",
            "reports_daily": self.root / "reports" / "daily",
            "reports_weekly": self.root / "reports" / "weekly",
            "reports_monthly": self.root / "reports" / "monthly",
            "reports_yearly": self.root / "reports" / "yearly",
            "items": self.root / "knowledge" / "items" if self.series_id else self.root / "index" / "items",
            "reports": index_root / "reports",
            "tasks": self.root / "stats" / "tasks" if self.series_id else self.root / "index" / "tasks",
            "index": index_root,
            "config": self.root / "config",
        }
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        return paths

    def save_item(self, item: KnowledgeItem) -> KnowledgeItem:
        item_id = _safe_id(item.id)
        if self.series_id:
            item.series_id = self.series_id
        with self._lock:
            paths = self.ensure_structure()
            payload = item.model_dump(mode="json")
            _atomic_json(paths["items"] / f"{item_id}.json", payload)
            if item.status.in_inbox:
                _atomic_json(paths["inbox_raw"] / f"{item_id}.json", payload)
                self._write_inbox_entry(item)
            if item.status.in_daily:
                self._upsert_journal_item(item)
            self._refresh_indexes()
        return item

    def get_item(self, item_id: str) -> KnowledgeItem | None:
        path = self.ensure_structure()["items"] / f"{_safe_id(item_id)}.json"
        if not path.is_file():
            return None
        return KnowledgeItem.model_validate_json(path.read_text(encoding="utf-8"))

    def list_items(self) -> list[KnowledgeItem]:
        items_dir = self.ensure_structure()["items"]
        if not items_dir.is_dir():
            return []
        result: list[KnowledgeItem] = []
        for path in sorted(items_dir.glob("*.json")):
            result.append(KnowledgeItem.model_validate_json(path.read_text(encoding="utf-8")))
        return sorted(result, key=lambda item: item.created_at, reverse=True)

    def search_items(self, query: str, *, limit: int = 20) -> list[KnowledgeItem]:
        terms = _keywords(query)
        if not terms:
            return []
        matches: list[tuple[int, KnowledgeItem]] = []
        for item in self.list_items():
            haystack = " ".join([item.title, item.summary, item.content, *item.tags, *item.index.keywords]).lower()
            score = sum(haystack.count(term.lower()) for term in terms)
            if score:
                matches.append((score, item))
        matches.sort(key=lambda pair: (pair[0], pair[1].created_at), reverse=True)
        return [item for _, item in matches[: max(1, limit)]]

    def refresh_indexes(self) -> None:
        with self._lock:
            self.ensure_structure()
            self._refresh_indexes()
            self._refresh_report_index()

    def journal_items(self, date: str) -> list[KnowledgeItem]:
        path = self.journal_dir(date) / "items.json"
        if not path.is_file():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [KnowledgeItem.model_validate(item) for item in payload.get("items", [])]

    def _write_inbox_entry(self, item: KnowledgeItem) -> None:
        date = item.created_at[:10]
        inbox = self.root / ("intake/legacy" if self.series_id else "inbox")
        path = inbox / f"{date}.md"
        existing = path.read_text(encoding="utf-8") if path.is_file() else f"# {date} Inbox\n"
        marker = f"<!-- {item.id} -->"
        if marker in existing:
            return
        tags = " ".join(f"#{tag}" for tag in item.tags)
        block = f"\n{marker}\n## {item.title}\n\n{item.content}\n\n{tags}\n"
        _atomic_text(path, existing.rstrip() + block)

    def save_report(self, report: Report, markdown: str) -> list[Path]:
        report_id = _safe_id(report.report_id)
        if self.series_id:
            report.series_id = self.series_id
        with self._lock:
            paths = self.ensure_structure()
            payload = report.model_dump(mode="json")
            _atomic_json(paths["reports"] / f"{report_id}.json", payload)
            output_dir = self.root / "reports" / report.type
            output_dir.mkdir(parents=True, exist_ok=True)
            slug = _report_slug(report)
            json_path = output_dir / f"{slug}.json"
            markdown_path = output_dir / f"{slug}.md"
            _atomic_json(json_path, payload)
            _atomic_text(markdown_path, markdown.rstrip() + "\n")
            self._refresh_report_index()
            return [json_path, markdown_path]

    def save_daily_bundle(self, report: Report, markdown: str, items: list[KnowledgeItem]) -> list[Path]:
        if report.type != "daily" or report.date_range.start != report.date_range.end:
            raise ValueError("daily bundle requires a one-day daily report")
        journal_dir = self.journal_dir(report.date_range.start)
        payload = report.model_dump(mode="json")
        grouped = {kind: [item for item in items if item.type == kind] for kind in {item.type for item in items}}
        files = [
            journal_dir / "daily.json",
            journal_dir / "daily.md",
            journal_dir / "videos.json",
            journal_dir / "clips.json",
            journal_dir / "actions.json",
            journal_dir / "notes.md",
            journal_dir / "review.md",
        ]
        _atomic_json(files[0], payload)
        _atomic_text(files[1], markdown.rstrip() + "\n")
        _atomic_json(files[2], {"items": [item.model_dump(mode="json") for item in grouped.get("video", [])]})
        _atomic_json(files[3], {"items": [item.model_dump(mode="json") for item in grouped.get("clip", [])]})
        _atomic_json(files[4], {"items": [item.model_dump(mode="json") for item in grouped.get("action", [])]})
        notes = [item for item in items if item.type in {"note", "thought", "question"}]
        _atomic_text(files[5], _render_item_notes(report.date_range.start, notes))
        _atomic_text(files[6], _render_review(report))
        return files

    def get_report(self, report_id: str) -> Report | None:
        path = self.ensure_structure()["reports"] / f"{_safe_id(report_id)}.json"
        if not path.is_file():
            return None
        return Report.model_validate_json(path.read_text(encoding="utf-8"))

    def list_reports(self, report_type: str | None = None) -> list[Report]:
        report_dir = self.ensure_structure()["reports"]
        if not report_dir.is_dir():
            return []
        reports = [Report.model_validate_json(path.read_text(encoding="utf-8")) for path in report_dir.glob("*.json")]
        if report_type:
            reports = [report for report in reports if report.type == report_type]
        return sorted(reports, key=lambda report: (report.date_range.end, report.updated_at), reverse=True)

    def report_files(self, report_id: str) -> dict[str, Path] | None:
        report = self.get_report(report_id)
        if report is None:
            return None
        output_dir = self.root / "reports" / report.type
        slug = _report_slug(report)
        return {
            "directory": output_dir,
            "json": output_dir / f"{slug}.json",
            "markdown": output_dir / f"{slug}.md",
        }

    def save_task(self, task: ReplayTask) -> ReplayTask:
        if self.series_id:
            task.series_id = self.series_id
        path = self.ensure_structure()["tasks"] / f"{_safe_id(task.task_id)}.json"
        _atomic_json(path, task.model_dump(mode="json"))
        return task

    def list_tasks(self) -> list[ReplayTask]:
        task_dir = self.ensure_structure()["tasks"]
        if not task_dir.is_dir():
            return []
        tasks = [ReplayTask.model_validate_json(path.read_text(encoding="utf-8")) for path in task_dir.glob("*.json")]
        return sorted(tasks, key=lambda task: task.created_at, reverse=True)

    def _upsert_journal_item(self, item: KnowledgeItem) -> None:
        date = item.created_at[:10]
        path = self.journal_dir(date) / "items.json"
        payload: dict[str, Any] = {"date": date, "items": []}
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
        items = [entry for entry in payload.get("items", []) if entry.get("id") != item.id]
        items.append(item.model_dump(mode="json"))
        payload["items"] = items
        _atomic_json(path, payload)

    def journal_dir(self, date: str) -> Path:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError("journal date must use YYYY-MM-DD")
        path = self.root / "journals" / date[:4] / date[5:7] / date
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _refresh_indexes(self) -> None:
        items = self.list_items()
        search = {
            item.id: {
                "type": item.type,
                "title": item.title,
                "keywords": item.index.keywords,
                "created_at": item.created_at,
            }
            for item in items
        }
        timeline = [{"id": item.id, "type": item.type, "created_at": item.created_at} for item in items]
        index_root = self.ensure_structure()["index"]
        _atomic_json(index_root / "search.json", search)
        _atomic_json(index_root / "timeline.json", timeline)

    def _refresh_report_index(self) -> None:
        reports = self.list_reports()
        payload = [
            {
                "report_id": report.report_id,
                "type": report.type,
                "title": report.title,
                "start": report.date_range.start,
                "end": report.date_range.end,
                "updated_at": report.updated_at,
            }
            for report in reports
        ]
        _atomic_json(self.ensure_structure()["index"] / "reports.json", payload)


def _safe_id(value: str) -> str:
    if not SAFE_ID.fullmatch(value):
        raise ValueError("item id contains unsafe characters")
    return value


def _keywords(text: str, *, limit: int = 20) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,}", text.lower())
    result: list[str] = []
    for token in tokens:
        if token not in result:
            result.append(token)
        if len(result) >= limit:
            break
    return result


def _report_slug(report: Report) -> str:
    prefixes = {"daily": "daily_", "weekly": "weekly_", "monthly": "monthly_", "yearly": "yearly_"}
    value = report.report_id.removeprefix(prefixes[report.type])
    return _safe_id(value)


def _render_item_notes(date: str, items: list[KnowledgeItem]) -> str:
    lines = [f"# {date} 个人记录", ""]
    for item in items:
        lines.extend([f"## {item.title}", "", item.summary or item.content, ""])
    if not items:
        lines.extend(["当天没有个人记录。", ""])
    return "\n".join(lines)


def _render_review(report: Report) -> str:
    sections = [
        ("我学到了什么", report.review.what_i_learned),
        ("值得重看", report.review.what_i_should_revisit),
        ("未解决问题", report.review.open_questions),
        ("下一步行动", report.review.next_actions),
        ("长期模式", report.review.long_term_patterns),
    ]
    lines = [f"# {report.title} 复盘", ""]
    for title, values in sections:
        lines.extend([f"## {title}", ""])
        lines.extend([f"- {value}" for value in values] or ["- 暂无"])
        lines.append("")
    return "\n".join(lines)


def _atomic_json(path: Path, payload: object) -> None:
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _ATOMIC_WRITES.hold(str(path.resolve())):
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(7):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if attempt == 6:
                        raise
                    time.sleep(0.01 * (attempt + 1))
        finally:
            temporary.unlink(missing_ok=True)
