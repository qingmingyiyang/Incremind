from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .ports import ObjectStorePort


class InspirationSystemError(ValueError):
    """Raised when an inspiration operation cannot be completed locally."""


@dataclass(frozen=True, slots=True)
class InspirationFragment:
    fragment_id: str
    text_preview: str
    themes: tuple[str, ...]
    source_id: str


@dataclass(frozen=True, slots=True)
class InspirationRecordResult:
    status: str
    inspiration_id: str
    source_id: str
    series_id: str
    series_name: str
    themes: tuple[str, ...]
    summary: str
    fragments: tuple[InspirationFragment, ...]
    inspiration_ref: str
    series_ref: str
    activity_refs: tuple[str, ...]
    memory_publication_state: str
    blocked_operations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InspirationCollisionResult:
    status: str
    collision_id: str
    query: str
    themes: tuple[str, ...]
    selected_fragments: tuple[InspirationFragment, ...]
    prompts: tuple[str, ...]
    collision_ref: str
    memory_publication_state: str
    blocked_operations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InspirationOverviewResult:
    status: str
    project_id: str | None
    counts: Mapping[str, int]
    heatmap_days: tuple[Mapping[str, object], ...]
    themes: tuple[Mapping[str, object], ...]
    records: tuple[Mapping[str, object], ...]
    series: tuple[Mapping[str, object], ...]
    collisions: tuple[Mapping[str, object], ...]
    summaries: tuple[str, ...]
    recommended_prompts: tuple[str, ...]
    memory_publication_state: str
    blocked_operations: tuple[str, ...]


class RecordInspirationFromSource:
    """Persist a local inspiration object and attach it to the inspiration series."""

    _BLOCKED_OPERATIONS = (
        "model_provider_execution",
        "automatic_long_term_memory_publication",
        "external_agent_write",
    )

    _INSPIRATION_KEYWORDS = (
        "灵感",
        "想法",
        "点子",
        "idea",
        "inspiration",
        "构思",
        "创意",
        "启发",
        "突然想到",
        "我觉得",
        "可以试试",
        "如果",
    )

    _THEME_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("产品", ("产品", "体验", "功能", "mvp", "用户")),
        ("记忆", ("记忆", "memory", "资料库", "知识库", "四层")),
        ("项目", ("项目", "推进", "任务", "交付", "复盘")),
        ("写作", ("写作", "表达", "文章", "报告", "总结")),
        ("视频", ("视频", "音频", "转写", "字幕", "录音")),
        ("AI", ("ai", "agent", "模型", "deepseek", "openai")),
    )

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-02T20:40:00+08:00",
        preview_chars: int = 220,
    ) -> None:
        if preview_chars <= 0:
            raise ValueError("preview_chars must be positive")
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now
        self._preview_chars = preview_chars

    def execute(
        self,
        *,
        source_id: str,
        content_read_id: str | None = None,
        text: str | None = None,
        project_id: str | None = None,
        series_name: str | None = None,
        theme_hint: str | None = None,
    ) -> InspirationRecordResult:
        clean_source_id = source_id.strip()
        if not clean_source_id:
            raise InspirationSystemError("source_id is required")
        source = self._object_store.read("sources", clean_source_id)
        if source is None:
            raise InspirationSystemError("source not found")
        clean_text, read_id = self._resolve_text(source, content_read_id=content_read_id, text=text)
        if not clean_text:
            raise InspirationSystemError("source has no readable text for inspiration")
        if not self._looks_like_inspiration(source, clean_text, theme_hint=theme_hint):
            raise InspirationSystemError("source is not classified as inspiration")

        themes = _dedupe((*self._themes(clean_text), *_optional_tuple(theme_hint)))
        if not themes:
            themes = ("灵感",)
        clean_project_id = _clean_optional(project_id)
        clean_series_name = _clean_optional(series_name) or (
            f"灵感 · {clean_project_id}" if clean_project_id else "灵感系列"
        )
        series_id = _stable_id("inspiration-series", clean_series_name)
        inspiration_id = f"inspiration-{clean_source_id}"
        inspiration_ref = f"crp://{self._namespace_id}/inspirations/{inspiration_id}.json"
        series_ref = f"crp://{self._namespace_id}/inspiration-series/{series_id}.json"
        fragments = self._fragments(clean_text, themes=themes, source_id=clean_source_id)
        summary = _summary(clean_text, self._preview_chars)
        event_ref = self._write_event(
            source_id=clean_source_id,
            inspiration_id=inspiration_id,
            inspiration_ref=inspiration_ref,
            series_id=series_id,
            series_name=clean_series_name,
        )
        record = {
            "schema_version": "1.0.0",
            "id": inspiration_id,
            "status": "recorded",
            "kind": "inspiration",
            "source_id": clean_source_id,
            "content_read_id": read_id,
            "project_id": clean_project_id,
            "series_id": series_id,
            "series_name": clean_series_name,
            "series_ref": series_ref,
            "themes": list(themes),
            "summary": summary,
            "fragments": [serialize_inspiration_fragment(item) for item in fragments],
            "source_refs": [_source_ref(clean_source_id)],
            "trace_refs": [event_ref],
            "memory_publication": "not_started",
            "blocked_operations": list(self._BLOCKED_OPERATIONS),
            "created_at": self._now,
            "updated_at": self._now,
            "ref": inspiration_ref,
        }
        self._object_store.write("inspiration_records", inspiration_id, record, expected_revision=None)
        self._upsert_series(
            series_id=series_id,
            series_name=clean_series_name,
            series_ref=series_ref,
            inspiration_id=inspiration_id,
            source_id=clean_source_id,
            themes=themes,
        )
        self._update_source(
            source,
            inspiration_id=inspiration_id,
            inspiration_ref=inspiration_ref,
            series_id=series_id,
            series_name=clean_series_name,
            themes=themes,
            summary=summary,
            activity_refs=(event_ref,),
        )
        return InspirationRecordResult(
            status="recorded",
            inspiration_id=inspiration_id,
            source_id=clean_source_id,
            series_id=series_id,
            series_name=clean_series_name,
            themes=themes,
            summary=summary,
            fragments=fragments,
            inspiration_ref=inspiration_ref,
            series_ref=series_ref,
            activity_refs=(event_ref,),
            memory_publication_state="not_published",
            blocked_operations=self._BLOCKED_OPERATIONS,
        )

    def _resolve_text(
        self,
        source: Mapping[str, object],
        *,
        content_read_id: str | None,
        text: str | None,
    ) -> tuple[str, str | None]:
        if isinstance(text, str) and text.strip():
            return text.strip(), None
        read_id = _clean_optional(content_read_id) or _content_read_id_from_source(source)
        if read_id:
            read_record = self._object_store.read("source_content_reads", read_id)
            if isinstance(read_record, Mapping) and read_record.get("source_id") == source.get("id"):
                read_text = read_record.get("text")
                if isinstance(read_text, str) and read_text.strip():
                    return read_text.strip(), read_id
        metadata = source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {}
        candidates = (
            source.get("title"),
            source.get("display_name"),
            metadata.get("text") if isinstance(metadata, Mapping) else None,
            metadata.get("preview") if isinstance(metadata, Mapping) else None,
        )
        fallback = "\n".join(item.strip() for item in candidates if isinstance(item, str) and item.strip())
        return fallback.strip(), read_id

    def _looks_like_inspiration(
        self,
        source: Mapping[str, object],
        text: str,
        *,
        theme_hint: str | None,
    ) -> bool:
        metadata = source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {}
        intake_intent = metadata.get("intake_intent") if isinstance(metadata, Mapping) else None
        if isinstance(intake_intent, Mapping) and intake_intent.get("intent") == "inspiration":
            return True
        if _clean_optional(theme_hint):
            return True
        lower = text.lower()
        return any(keyword.lower() in lower for keyword in self._INSPIRATION_KEYWORDS)

    def _themes(self, text: str) -> tuple[str, ...]:
        lower = text.lower()
        themes = [
            theme
            for theme, keywords in self._THEME_RULES
            if any(keyword.lower() in lower for keyword in keywords)
        ]
        return tuple(themes)

    def _fragments(
        self,
        text: str,
        *,
        themes: Sequence[str],
        source_id: str,
    ) -> tuple[InspirationFragment, ...]:
        parts = _paragraphs(text)
        if not parts:
            parts = (_summary(text, self._preview_chars),)
        fragments: list[InspirationFragment] = []
        for index, part in enumerate(parts[:6], start=1):
            fragments.append(
                InspirationFragment(
                    fragment_id=f"fragment-{index:03d}",
                    text_preview=_preview(part, self._preview_chars),
                    themes=tuple(themes[:4]),
                    source_id=source_id,
                )
            )
        return tuple(fragments)

    def _write_event(
        self,
        *,
        source_id: str,
        inspiration_id: str,
        inspiration_ref: str,
        series_id: str,
        series_name: str,
    ) -> str:
        event_id = f"event-inspiration-recorded-{source_id}"
        event_ref = f"crp://{self._namespace_id}/activity/{event_id}.json"
        self._object_store.write(
            "activity_events",
            event_id,
            {
                "schema_version": "1.0.0",
                "id": event_id,
                "type": "inspiration_recorded",
                "source_id": source_id,
                "status": "recorded",
                "details": {
                    "inspiration_id": inspiration_id,
                    "inspiration_ref": inspiration_ref,
                    "series_id": series_id,
                    "series_name": series_name,
                },
                "memoryPublication": "not_started",
                "created_at": self._now,
                "ref": event_ref,
            },
            expected_revision=None,
        )
        return event_ref

    def _upsert_series(
        self,
        *,
        series_id: str,
        series_name: str,
        series_ref: str,
        inspiration_id: str,
        source_id: str,
        themes: Sequence[str],
    ) -> None:
        existing = self._object_store.read("inspiration_series", series_id)
        inspiration_ids = _str_list(existing.get("inspiration_ids")) if isinstance(existing, Mapping) else []
        source_ids = _str_list(existing.get("source_ids")) if isinstance(existing, Mapping) else []
        existing_themes = _str_list(existing.get("themes")) if isinstance(existing, Mapping) else []
        if inspiration_id not in inspiration_ids:
            inspiration_ids.append(inspiration_id)
        if source_id not in source_ids:
            source_ids.append(source_id)
        payload = {
            "schema_version": "1.0.0",
            "id": series_id,
            "name": series_name,
            "status": "active",
            "kind": "inspiration_series",
            "themes": sorted(set(existing_themes).union(themes)),
            "inspiration_ids": sorted(inspiration_ids),
            "source_ids": sorted(source_ids),
            "created_at": _optional_str(existing.get("created_at")) if isinstance(existing, Mapping) else self._now,
            "updated_at": self._now,
            "ref": series_ref,
        }
        self._object_store.write("inspiration_series", series_id, payload, expected_revision=None)

    def _update_source(
        self,
        source: Mapping[str, object],
        *,
        inspiration_id: str,
        inspiration_ref: str,
        series_id: str,
        series_name: str,
        themes: Sequence[str],
        summary: str,
        activity_refs: tuple[str, ...],
    ) -> None:
        source_id = _required_str(source, "id")
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        metadata["inspiration"] = {
            "status": "recorded",
            "inspiration_id": inspiration_id,
            "inspiration_ref": inspiration_ref,
            "series_id": series_id,
            "series_name": series_name,
            "themes": list(themes),
            "summary": summary,
            "activity_refs": list(activity_refs),
            "memory_publication": "not_started",
            "blocked_operations": list(self._BLOCKED_OPERATIONS),
            "updated_at": self._now,
        }
        updated = dict(source)
        updated["metadata"] = metadata
        self._object_store.write("sources", source_id, updated, expected_revision=None)


class GetInspirationOverview:
    """Read a local inspiration management overview for Library UI."""

    _BLOCKED_OPERATIONS = (
        "model_provider_execution",
        "automatic_long_term_memory_publication",
        "external_agent_write",
    )

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        today: str = "2026-07-03",
        heatmap_days: int = 35,
        record_limit: int = 12,
    ) -> None:
        if heatmap_days <= 0:
            raise ValueError("heatmap_days must be positive")
        if record_limit <= 0:
            raise ValueError("record_limit must be positive")
        self._object_store = object_store
        self._today = _parse_date(today)
        self._heatmap_days = heatmap_days
        self._record_limit = record_limit

    def execute(self, *, project_id: str | None = None) -> InspirationOverviewResult:
        clean_project_id = _clean_optional(project_id)
        records = _project_filter(self._object_store.list("inspiration_records"), clean_project_id)
        record_ids = {_str(item.get("id")) for item in records if _str(item.get("id"))}
        series = _series_filter(self._object_store.list("inspiration_series"), clean_project_id, record_ids)
        collisions = _project_filter(self._object_store.list("inspiration_collisions"), clean_project_id)
        recent_records = tuple(_overview_record(item) for item in _sort_by_updated(records)[: self._record_limit])
        theme_rows = tuple(
            {"theme": theme, "count": count}
            for theme, count in Counter(
                theme for item in records for theme in _str_list(item.get("themes"))
            ).most_common(10)
        )
        summaries = tuple(
            _str(item.get("summary")) for item in _sort_by_updated(records)[:5] if _str(item.get("summary"))
        )
        return InspirationOverviewResult(
            status="ready" if records or series or collisions else "empty",
            project_id=clean_project_id,
            counts={
                "records": len(records),
                "series": len(series),
                "collisions": len(collisions),
                "themes": len(theme_rows),
            },
            heatmap_days=self._heatmap(records),
            themes=theme_rows,
            records=recent_records,
            series=tuple(_overview_series(item) for item in _sort_by_updated(series)[: self._record_limit]),
            collisions=tuple(_overview_collision(item) for item in _sort_by_updated(collisions)[: self._record_limit]),
            summaries=summaries,
            recommended_prompts=_overview_prompts(theme_rows, recent_records),
            memory_publication_state="not_published",
            blocked_operations=self._BLOCKED_OPERATIONS,
        )

    def _heatmap(self, records: Sequence[Mapping[str, object]]) -> tuple[Mapping[str, object], ...]:
        counts: Counter[str] = Counter()
        for item in records:
            day = _record_date(item)
            if day:
                counts[day] += 1
        start = self._today - timedelta(days=self._heatmap_days - 1)
        return tuple(
            {
                "date": (start + timedelta(days=offset)).isoformat(),
                "count": counts.get((start + timedelta(days=offset)).isoformat(), 0),
            }
            for offset in range(self._heatmap_days)
        )


class CreateInspirationCollision:
    """Recall local inspiration fragments and generate deterministic collision prompts."""

    _BLOCKED_OPERATIONS = (
        "model_provider_execution",
        "automatic_long_term_memory_publication",
        "external_agent_write",
    )

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-02T20:45:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now

    def execute(
        self,
        *,
        query: str | None = None,
        themes: Sequence[str] | None = None,
        project_id: str | None = None,
        limit: int = 6,
    ) -> InspirationCollisionResult:
        clean_query = _clean_optional(query) or "灵感碰撞"
        clean_themes = tuple(theme.strip() for theme in themes or () if isinstance(theme, str) and theme.strip())
        clean_project_id = _clean_optional(project_id)
        limit = max(1, min(limit, 12))
        records = [dict(item) for item in self._object_store.list("inspiration_records")]
        ranked = sorted(
            (
                (_score_record(record, query=clean_query, themes=clean_themes, project_id=clean_project_id), record)
                for record in records
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        selected_records = [record for score, record in ranked if score > 0][:limit]
        if not selected_records and records:
            selected_records = records[:limit]
        fragments = _selected_fragments(selected_records, limit=limit)
        prompts = _collision_prompts(clean_query, fragments)
        collision_id = f"inspiration-collision-{hashlib.sha256((clean_query + '|'.join(clean_themes)).encode('utf-8')).hexdigest()[:12]}"
        collision_ref = f"crp://{self._namespace_id}/inspiration-collisions/{collision_id}.json"
        payload = {
            "schema_version": "1.0.0",
            "id": collision_id,
            "status": "created",
            "query": clean_query,
            "themes": list(clean_themes),
            "project_id": clean_project_id,
            "selected_fragments": [serialize_inspiration_fragment(item) for item in fragments],
            "prompts": list(prompts),
            "memory_publication": "not_started",
            "blocked_operations": list(self._BLOCKED_OPERATIONS),
            "created_at": self._now,
            "ref": collision_ref,
        }
        self._object_store.write("inspiration_collisions", collision_id, payload, expected_revision=None)
        return InspirationCollisionResult(
            status="created",
            collision_id=collision_id,
            query=clean_query,
            themes=clean_themes,
            selected_fragments=fragments,
            prompts=prompts,
            collision_ref=collision_ref,
            memory_publication_state="not_published",
            blocked_operations=self._BLOCKED_OPERATIONS,
        )


def serialize_inspiration_record_result(result: InspirationRecordResult) -> dict[str, object]:
    return {
        "status": result.status,
        "inspiration_id": result.inspiration_id,
        "source_id": result.source_id,
        "series_id": result.series_id,
        "series_name": result.series_name,
        "themes": list(result.themes),
        "summary": result.summary,
        "fragments": [serialize_inspiration_fragment(item) for item in result.fragments],
        "inspiration_ref": result.inspiration_ref,
        "series_ref": result.series_ref,
        "activity_refs": list(result.activity_refs),
        "memory_publication_state": result.memory_publication_state,
        "blocked_operations": list(result.blocked_operations),
    }


def serialize_inspiration_collision_result(result: InspirationCollisionResult) -> dict[str, object]:
    return {
        "status": result.status,
        "collision_id": result.collision_id,
        "query": result.query,
        "themes": list(result.themes),
        "selected_fragments": [serialize_inspiration_fragment(item) for item in result.selected_fragments],
        "prompts": list(result.prompts),
        "collision_ref": result.collision_ref,
        "memory_publication_state": result.memory_publication_state,
        "blocked_operations": list(result.blocked_operations),
    }


def serialize_inspiration_overview_result(result: InspirationOverviewResult) -> dict[str, object]:
    return {
        "status": result.status,
        "project_id": result.project_id,
        "counts": dict(result.counts),
        "heatmap_days": list(result.heatmap_days),
        "themes": list(result.themes),
        "records": list(result.records),
        "series": list(result.series),
        "collisions": list(result.collisions),
        "summaries": list(result.summaries),
        "recommended_prompts": list(result.recommended_prompts),
        "memory_publication_state": result.memory_publication_state,
        "blocked_operations": list(result.blocked_operations),
    }


def serialize_inspiration_fragment(fragment: InspirationFragment) -> dict[str, object]:
    return {
        "fragment_id": fragment.fragment_id,
        "text_preview": fragment.text_preview,
        "themes": list(fragment.themes),
        "source_id": fragment.source_id,
    }


def _score_record(
    record: Mapping[str, object],
    *,
    query: str,
    themes: Sequence[str],
    project_id: str | None,
) -> int:
    haystack = " ".join(
        item
        for item in (
            _optional_str(record.get("summary")),
            " ".join(_str_list(record.get("themes"))),
            _optional_str(record.get("series_name")),
        )
        if item
    ).lower()
    query_tokens = _tokens(query)
    score = sum(1 for token in query_tokens if token in haystack)
    record_themes = set(_str_list(record.get("themes")))
    score += len(record_themes.intersection(themes)) * 3
    if project_id and record.get("project_id") == project_id:
        score += 4
    return score


def _selected_fragments(records: Sequence[Mapping[str, object]], *, limit: int) -> tuple[InspirationFragment, ...]:
    selected: list[InspirationFragment] = []
    for record in records:
        source_id = _optional_str(record.get("source_id")) or ""
        for raw in record.get("fragments") if isinstance(record.get("fragments"), list) else []:
            if not isinstance(raw, Mapping):
                continue
            text_preview = _optional_str(raw.get("text_preview"))
            if not text_preview:
                continue
            selected.append(
                InspirationFragment(
                    fragment_id=_optional_str(raw.get("fragment_id")) or f"fragment-{len(selected) + 1:03d}",
                    text_preview=text_preview,
                    themes=tuple(_str_list(raw.get("themes"))),
                    source_id=_optional_str(raw.get("source_id")) or source_id,
                )
            )
            if len(selected) >= limit:
                return tuple(selected)
    return tuple(selected)


def _overview_record(item: Mapping[str, object]) -> Mapping[str, object]:
    return {
        "inspiration_id": item.get("id"),
        "source_id": item.get("source_id"),
        "series_id": item.get("series_id"),
        "series_name": item.get("series_name"),
        "themes": _str_list(item.get("themes")),
        "summary": item.get("summary"),
        "fragments": list(_overview_fragments(item.get("fragments"))),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "ref": item.get("ref"),
        "memory_publication": item.get("memory_publication") or "not_started",
    }


def _overview_series(item: Mapping[str, object]) -> Mapping[str, object]:
    return {
        "series_id": item.get("id"),
        "series_name": item.get("series_name") or item.get("name"),
        "themes": _str_list(item.get("themes")),
        "inspiration_ids": _str_list(item.get("inspiration_ids")),
        "source_ids": _str_list(item.get("source_ids")),
        "updated_at": item.get("updated_at"),
        "ref": item.get("ref"),
    }


def _overview_collision(item: Mapping[str, object]) -> Mapping[str, object]:
    return {
        "collision_id": item.get("id"),
        "query": item.get("query"),
        "themes": _str_list(item.get("themes")),
        "selected_fragments": list(_overview_fragments(item.get("selected_fragments"))),
        "prompts": _str_list(item.get("prompts")),
        "created_at": item.get("created_at"),
        "ref": item.get("ref"),
    }


def _overview_fragments(value: object) -> tuple[Mapping[str, object], ...]:
    return tuple(
        {
            "fragment_id": item.get("fragment_id"),
            "text_preview": item.get("text_preview"),
            "themes": _str_list(item.get("themes")),
            "source_id": item.get("source_id"),
        }
        for item in _list_mappings(value)
    )


def _overview_prompts(
    themes: Sequence[Mapping[str, object]],
    records: Sequence[Mapping[str, object]],
) -> tuple[str, ...]:
    theme_names = [_str(item.get("theme")) for item in themes if _str(item.get("theme"))][:3]
    if not records:
        return ("先记录 3 条灵感，再执行灵感碰撞。",)
    if not theme_names:
        theme_names = ["灵感"]
    return (
        f"围绕「{' / '.join(theme_names)}」抽取 3 条灵感，生成一个本周可执行实验。",
        "找出最近灵感里的共同问题，并反推一个资料库整理动作。",
        "把一条灵感转成项目 skill 的默认输出要求草案。",
    )


def _project_filter(items: Sequence[Mapping[str, object]], project_id: str | None) -> list[Mapping[str, object]]:
    if not project_id:
        return [dict(item) for item in items]
    return [dict(item) for item in items if item.get("project_id") == project_id]


def _series_filter(
    items: Sequence[Mapping[str, object]],
    project_id: str | None,
    record_ids: set[str],
) -> list[Mapping[str, object]]:
    if not project_id:
        return [dict(item) for item in items]
    return [
        dict(item)
        for item in items
        if item.get("project_id") == project_id
        or bool(record_ids.intersection(_str_list(item.get("inspiration_ids"))))
    ]


def _sort_by_updated(items: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    return sorted(
        items,
        key=lambda item: _str(item.get("updated_at")) or _str(item.get("created_at")),
        reverse=True,
    )


def _record_date(item: Mapping[str, object]) -> str | None:
    raw = _str(item.get("created_at")) or _str(item.get("updated_at"))
    if not raw:
        return None
    return raw[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", raw) else None


def _parse_date(value: str) -> date:
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return date(2026, 7, 3)


def _collision_prompts(query: str, fragments: Sequence[InspirationFragment]) -> tuple[str, ...]:
    if not fragments:
        return (f"围绕「{query}」继续记录 3 条可执行想法，再回来做碰撞。",)
    theme_counter: Counter[str] = Counter()
    for fragment in fragments:
        theme_counter.update(fragment.themes)
    main_themes = [theme for theme, _count in theme_counter.most_common(3)] or ["灵感"]
    prompts = [
        f"把「{query}」和「{main_themes[0]}」合并，提炼一个今天能推进的小实验。",
        f"从 {len(fragments)} 条灵感里找一个共同问题，再反推一个资料库或项目动作。",
    ]
    if len(main_themes) >= 2:
        prompts.append(f"尝试连接「{main_themes[0]}」与「{main_themes[1]}」，形成一个新系列或子主题。")
    return tuple(prompts)


def _content_read_id_from_source(source: Mapping[str, object]) -> str | None:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    content_read = metadata.get("content_read")
    if not isinstance(content_read, Mapping):
        return None
    read_ref = content_read.get("read_ref")
    if not isinstance(read_ref, str) or not read_ref:
        return None
    marker = "/source-content-reads/"
    if marker not in read_ref:
        return None
    return read_ref.rsplit(marker, 1)[-1].removesuffix(".json")


def _paragraphs(text: str) -> tuple[str, ...]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = [part.strip() for part in re.split(r"\n\s*\n|\n", normalized) if part.strip()]
    if len(parts) <= 1:
        parts = [part.strip() for part in re.split(r"(?<=[。！？.!?])\s+", normalized) if part.strip()]
    return tuple(parts)


def _summary(text: str, limit: int) -> str:
    paragraphs = _paragraphs(text)
    return _preview(paragraphs[0] if paragraphs else text, max(limit, 80))


def _preview(text: str, limit: int) -> str:
    compact = " ".join(text.strip().split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1]}..."


def _tokens(value: str) -> tuple[str, ...]:
    words = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", value.lower())
    return tuple(dict.fromkeys(words))


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.lower().encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _source_ref(source_id: str) -> str:
    return f"source:{source_id}"


def _required_str(source: Mapping[str, object], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value:
        raise InspirationSystemError(f"source requires {key}")
    return value


def _clean_optional(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = " ".join(value.strip().split())
    return clean or None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_tuple(value: object) -> tuple[str, ...]:
    clean = _clean_optional(value)
    return (clean,) if clean else ()


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _list_mappings(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _str(value: object) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = _clean_optional(value)
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return tuple(result)
