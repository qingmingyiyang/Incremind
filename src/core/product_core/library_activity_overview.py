from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .library_overview import GetLibraryOverview, LibraryOverviewItem, LibraryOverviewReaderPort


_PUBLISHED_MEMORY_TYPES = frozenset({"atom", "scenario", "series_memory", "project_skill"})


@dataclass(frozen=True, slots=True)
class LibraryActivityOverview:
    status: str
    scope: str
    project_id: str | None
    recent_days: tuple[Mapping[str, object], ...]
    date_counts: Mapping[str, int]
    item_refs_by_date: Mapping[str, tuple[Mapping[str, object], ...]]
    years: tuple[int, ...]
    year_days: tuple[Mapping[str, object], ...]
    by_type: tuple[Mapping[str, object], ...]
    counts: Mapping[str, int]
    blocked_operations: tuple[str, ...]
    next_step_boundary: str


class GetLibraryActivityOverview:
    """Build a read-only activity heatmap for the Library UI."""

    _BLOCKED_OPERATIONS = (
        "source_content_read",
        "parser_execution",
        "media_processing_provider_execution",
        "model_provider_execution",
        "memory_publication",
        "legacy_library_write",
    )

    def __init__(
        self,
        reader: LibraryOverviewReaderPort,
        *,
        namespace_id: str = "default",
        today: date | None = None,
        recent_day_count: int = 35,
    ) -> None:
        self._reader = reader
        self._namespace_id = namespace_id
        self._today = today or datetime.now().astimezone().date()
        self._recent_day_count = max(1, recent_day_count)

    def execute(self, *, project_id: str | None = None, year: int | None = None) -> LibraryActivityOverview:
        overview = GetLibraryOverview(self._reader, namespace_id=self._namespace_id).execute(project_id=project_id)
        raw_by_key = _raw_objects_by_key(self._reader)
        date_counts: Counter[str] = Counter()
        item_refs_by_date: defaultdict[str, list[Mapping[str, object]]] = defaultdict(list)
        type_counts: Counter[str] = Counter()
        undated = 0
        pending_memory_candidate_count = 0
        published_memory_count = 0
        today_memory_count = 0
        recent_7d_memory_count = 0
        today_key = self._today.isoformat()
        recent_7d_start = (self._today - timedelta(days=6)).isoformat()

        for item in overview.items:
            raw = raw_by_key.get((item.item_type, item.item_id), {})
            item_date = _date_key(raw)
            type_label = _type_label(item)
            type_counts[type_label] += 1
            if item.item_type == "memory_candidate" and item.status == "pending_review":
                pending_memory_candidate_count += 1
            if item.item_type in _PUBLISHED_MEMORY_TYPES and item.status == "published":
                published_memory_count += 1
                memory_date = _published_memory_date_key(raw)
                if memory_date == today_key:
                    today_memory_count += 1
                if recent_7d_start <= memory_date <= today_key:
                    recent_7d_memory_count += 1
            if not item_date:
                undated += 1
                continue
            date_counts[item_date] += 1
            item_refs_by_date[item_date].append(_activity_item_ref(item, item_date, type_label))

        start = self._today - timedelta(days=self._recent_day_count - 1)
        recent_days = tuple(
            {
                "date": (start + timedelta(days=offset)).isoformat(),
                "count": date_counts.get((start + timedelta(days=offset)).isoformat(), 0),
            }
            for offset in range(self._recent_day_count)
        )
        years = tuple(sorted({int(key[:4]) for key in date_counts if key[:4].isdigit()} | {self._today.year}, reverse=True))
        year_days = _year_days(year, date_counts, fallback_year=self._today.year)
        by_type = tuple(
            {"label": label, "count": count}
            for label, count in sorted(type_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        )
        counts = {
            "items": len(overview.items),
            "dated_items": sum(date_counts.values()),
            "undated_items": undated,
            "active_days": len(date_counts),
            "pending_memory_candidates": pending_memory_candidate_count,
            "published_memories": published_memory_count,
            "today_published_memories": today_memory_count,
            "recent_7d_published_memories": recent_7d_memory_count,
        }
        return LibraryActivityOverview(
            status="ready" if overview.items else "empty",
            scope=overview.scope,
            project_id=overview.project_id,
            recent_days=recent_days,
            date_counts=dict(sorted(date_counts.items())),
            item_refs_by_date={key: tuple(value) for key, value in sorted(item_refs_by_date.items())},
            years=years,
            year_days=year_days,
            by_type=by_type,
            counts=counts,
            blocked_operations=self._BLOCKED_OPERATIONS,
            next_step_boundary="library_activity_overview_ready_without_content_read",
        )


def serialize_library_activity_overview(overview: LibraryActivityOverview) -> dict[str, object]:
    return {
        "status": overview.status,
        "scope": overview.scope,
        "project_id": overview.project_id,
        "recent_days": list(overview.recent_days),
        "date_counts": dict(overview.date_counts),
        "item_refs_by_date": {key: list(value) for key, value in overview.item_refs_by_date.items()},
        "years": list(overview.years),
        "year_days": list(overview.year_days),
        "by_type": list(overview.by_type),
        "counts": dict(overview.counts),
        "blocked_operations": list(overview.blocked_operations),
        "next_step_boundary": overview.next_step_boundary,
    }


def _year_days(
    year: int | None,
    date_counts: Mapping[str, int],
    *,
    fallback_year: int,
) -> tuple[Mapping[str, object], ...]:
    target_year = year if isinstance(year, int) and year > 0 else fallback_year
    try:
        cursor = date(target_year, 1, 1)
    except ValueError:
        return ()
    days: list[Mapping[str, object]] = []
    while cursor.year == target_year:
        key = cursor.isoformat()
        days.append(
            {
                "date": key,
                "count": date_counts.get(key, 0),
                "month": cursor.month,
                "day": cursor.day,
            }
        )
        cursor += timedelta(days=1)
    return tuple(days)


def _raw_objects_by_key(reader: LibraryOverviewReaderPort) -> dict[tuple[str, str], Mapping[str, object]]:
    raw: dict[tuple[str, str], Mapping[str, object]] = {}
    for item in reader.sources():
        _add_raw(raw, "source", item)
    for item in reader.documents():
        _add_raw(raw, "document", item)
    for item in reader.memory_candidates():
        _add_raw(raw, "memory_candidate", item)
    for item in reader.external_agent_review_drafts():
        _add_raw(raw, "external_agent_review_draft", item)
    for item in reader.memory_objects():
        item_type = _str(item.get("layer")) or _str(item.get("type")) or _str(item.get("item_type")) or "memory"
        _add_raw(raw, item_type, item)
    return raw


def _add_raw(target: dict[tuple[str, str], Mapping[str, object]], item_type: str, item: Mapping[str, object]) -> None:
    item_id = _str(item.get("id")) or _str(item.get("item_id")) or _str(item.get("candidate_id"))
    if item_id:
        target[(item_type, item_id)] = item


def _date_key(item: Mapping[str, object]) -> str:
    for key in (
        "created_at",
        "captured_at",
        "imported_at",
        "published_at",
        "updated_at",
        "completed_at",
        "reviewed_at",
    ):
        value = _str(item.get(key))
        if len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-":
            return value[:10]
    return ""


def _published_memory_date_key(item: Mapping[str, object]) -> str:
    """Return a published-memory date without reading any memory content."""
    for key in ("published_at", "updated_at", "created_at"):
        value = _str(item.get(key))
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            parsed = parsed.astimezone()
        return parsed.date().isoformat()
    return ""


def _activity_item_ref(item: LibraryOverviewItem, item_date: str, type_label: str) -> Mapping[str, object]:
    return {
        "item_id": item.item_id,
        "item_type": item.item_type,
        "title": item.title,
        "project_id": item.project_id,
        "date": item_date,
        "type_label": type_label,
        "status": item.status,
        "source_refs": list(item.source_refs[:4]),
        "trace_refs": list(item.trace_refs[:4]),
    }


def _type_label(item: LibraryOverviewItem) -> str:
    media_type = (item.source_media_type or "").lower()
    if item.item_type == "source":
        if item.source_content_kind == "video":
            return "视频"
        if "uri" in media_type or "bookmark" in media_type:
            return "链接"
        if media_type.startswith("image/"):
            return "图片"
        if media_type.startswith("audio/"):
            return "音频"
        if media_type.startswith("video/"):
            return "视频"
        return "资料"
    if item.item_type == "document":
        return "文档"
    if item.item_type == "memory_candidate":
        return "候选"
    if item.item_type == "external_agent_review_draft":
        return "Agent 草稿"
    return "记忆"


def _str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
