from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .ports import ObjectStorePort

from .task_model_map_resolver import TaskModelMapResolver


class SourceStructuringError(ValueError):
    """Raised when a Source cannot be structured from completed content."""


@dataclass(frozen=True, slots=True)
class ParagraphTag:
    paragraph_id: str
    text_preview: str
    tags: tuple[str, ...]
    confidence: float


@dataclass(frozen=True, slots=True)
class SourceStructuringResult:
    status: str
    source_id: str
    content_read_id: str
    paragraph_tags: tuple[ParagraphTag, ...]
    tags: tuple[str, ...]
    summary: str
    key_points: tuple[str, ...]
    structured_body: str
    series_candidate: str
    series_confidence: float
    series_reason: str
    structure_ref: str
    activity_refs: tuple[str, ...]
    organization_prompt_refs: tuple[Mapping[str, object], ...]
    memory_publication_state: str
    blocked_operations: tuple[str, ...]


class StructureSourceContent:
    """Create deterministic paragraph tags and a series candidate from completed source_content_read."""

    _BLOCKED_OPERATIONS = (
        "model_provider_execution",
        "automatic_series_write",
        "memory_candidate_auto_creation",
        "long_term_memory_publication",
    )

    _TAG_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("AI", ("ai", "agent", "模型", "智能体", "deepseek", "openai")),
        ("Memory", ("记忆", "memory", "长期", "四层", "l0", "l1", "l2", "l3")),
        ("Product", ("产品", "mvp", "需求", "体验", "功能", "用户")),
        ("Project", ("项目", "推进", "计划", "任务", "交付", "验收")),
        ("Research", ("研究", "论文", "资料", "阅读", "reference", "证据")),
        ("Workflow", ("流程", "workflow", "链路", "步骤", "入库", "处理")),
        ("Video", ("视频", "音频", "转写", "字幕", "bilibili", "录音")),
        ("UI", ("ui", "界面", "玻璃", "视觉", "设计", "交互")),
    )

    _SERIES_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
        ("个人 AI 记忆工作台", ("记忆", "memory", "资料库", "个人 ai", "知识库"), "命中个人记忆、资料库或知识库语义。"),
        ("产品设计", ("产品", "mvp", "需求", "体验", "功能"), "命中产品设计、MVP 或功能体验语义。"),
        ("项目推进", ("项目", "推进", "任务", "验收", "交付"), "命中项目推进、任务或交付语义。"),
        ("视频资料", ("视频", "音频", "转写", "字幕", "bilibili", "录音"), "命中视频、音频或转写语义。"),
        ("研究阅读", ("研究", "论文", "阅读", "资料", "证据"), "命中研究阅读或资料证据语义。"),
    )

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-02T18:20:00+08:00",
        preview_chars: int = 180,
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
        prompt_context: Sequence[Mapping[str, object]] = (),
        task_model_map: Mapping[str, object] | None = None,
    ) -> SourceStructuringResult:
        clean_source_id = source_id.strip()
        if not clean_source_id:
            raise SourceStructuringError("source_id is required")
        source = self._object_store.read("sources", clean_source_id)
        if source is None:
            raise SourceStructuringError("source not found")
        read_id = content_read_id.strip() if isinstance(content_read_id, str) and content_read_id.strip() else (
            _content_read_id_from_source(source) or f"content-read-{clean_source_id}"
        )
        read_record = self._object_store.read("source_content_reads", read_id)
        if read_record is None or read_record.get("source_id") != clean_source_id:
            raise SourceStructuringError("completed source_content_read not found for source")
        if read_record.get("status") != "completed":
            raise SourceStructuringError("source_content_read must be completed before structuring")
        text = read_record.get("text")
        if not isinstance(text, str) or not text.strip():
            raise SourceStructuringError("source_content_read has no text")

        paragraph_tags = self._paragraph_tags(text)
        tags = _top_tags(paragraph_tags)
        summary = _summary(text, self._preview_chars)
        key_points = _key_points(paragraph_tags)
        structured_body = _structured_body(paragraph_tags)
        series_candidate, series_confidence, series_reason = self._classify_series(text, tags)
        organization_prompt_refs = _prompt_refs(prompt_context)
        # 只把旧 task_model_map 作为历史追踪元数据写入 structure_record；
        # 不解析或调用Provider，不能把这些引用当作Model Route运行证据。
        model_profile_refs = _resolve_model_profile_refs(task_model_map, ("memory", "lightweight"))
        structure_id = f"structure-{clean_source_id}"
        structure_ref = f"crp://{self._namespace_id}/source-structures/{structure_id}.json"
        structure_record = {
            "schema_version": "1.0.0",
            "id": structure_id,
            "source_id": clean_source_id,
            "content_read_id": read_id,
            "status": "completed",
            "paragraph_tags": [serialize_paragraph_tag(item) for item in paragraph_tags],
            "tags": list(tags),
            "summary": summary,
            "key_points": list(key_points),
            "structured_body": structured_body,
            "series_candidate": series_candidate,
            "series_confidence": series_confidence,
            "series_reason": series_reason,
            "organization_prompt_refs": [dict(item) for item in organization_prompt_refs],
            "model_profile_refs": [dict(item) for item in model_profile_refs],
            "memory_publication": "not_started",
            "blocked_operations": list(self._BLOCKED_OPERATIONS),
            "created_at": self._now,
            "ref": structure_ref,
        }
        self._object_store.write("source_structures", structure_id, structure_record, expected_revision=None)
        updated_read = dict(read_record)
        updated_read["structure_ref"] = structure_ref
        updated_read["paragraph_tags"] = structure_record["paragraph_tags"]
        updated_read["tags"] = list(tags)
        updated_read["summary"] = summary
        updated_read["key_points"] = list(key_points)
        updated_read["structured_body"] = structured_body
        updated_read["series_candidate"] = series_candidate
        updated_read["series_confidence"] = series_confidence
        self._object_store.write("source_content_reads", read_id, updated_read, expected_revision=None)
        event_ref = self._write_event(
            clean_source_id,
            structure_id=structure_id,
            structure_ref=structure_ref,
            content_read_id=read_id,
            tags=tags,
            series_candidate=series_candidate,
            organization_prompt_refs=organization_prompt_refs,
        )
        self._update_source_structure(
            source,
            structure_ref=structure_ref,
            structure_id=structure_id,
            content_read_id=read_id,
            paragraph_tags=paragraph_tags,
            tags=tags,
            summary=summary,
            key_points=key_points,
            structured_body=structured_body,
            series_candidate=series_candidate,
            series_confidence=series_confidence,
            series_reason=series_reason,
            organization_prompt_refs=organization_prompt_refs,
            activity_refs=(event_ref,),
        )
        return SourceStructuringResult(
            status="completed",
            source_id=clean_source_id,
            content_read_id=read_id,
            paragraph_tags=paragraph_tags,
            tags=tags,
            summary=summary,
            key_points=key_points,
            structured_body=structured_body,
            series_candidate=series_candidate,
            series_confidence=series_confidence,
            series_reason=series_reason,
            structure_ref=structure_ref,
            activity_refs=(event_ref,),
            organization_prompt_refs=organization_prompt_refs,
            memory_publication_state="not_published",
            blocked_operations=self._BLOCKED_OPERATIONS,
        )

    def _paragraph_tags(self, text: str) -> tuple[ParagraphTag, ...]:
        paragraphs = _paragraphs(text)
        if not paragraphs:
            paragraphs = (_preview(text, self._preview_chars),)
        result: list[ParagraphTag] = []
        for index, paragraph in enumerate(paragraphs, start=1):
            tags = _tags_for_text(paragraph, self._TAG_RULES)
            if not tags:
                tags = ("Knowledge",)
            result.append(
                ParagraphTag(
                    paragraph_id=f"p{index:03d}",
                    text_preview=_preview(paragraph, self._preview_chars),
                    tags=tags[:4],
                    confidence=min(0.95, 0.56 + 0.09 * len(tags)),
                )
            )
        return tuple(result)

    def _classify_series(self, text: str, tags: Sequence[str]) -> tuple[str, float, str]:
        text_lower = text.lower()
        tag_text = " ".join(tags).lower()
        if "个人 ai 记忆工作台" in text_lower or ("资料库" in text_lower and "记忆" in text_lower):
            return "个人 AI 记忆工作台", 0.92, "命中个人 AI 记忆工作台、资料库或记忆语义。"
        scores: list[tuple[int, str, str]] = []
        for series, keywords, reason in self._SERIES_RULES:
            score = sum(1 for keyword in keywords if keyword.lower() in text_lower or keyword.lower() in tag_text)
            scores.append((score, series, reason))
        score, series, reason = max(scores, key=lambda item: (item[0], item[1]))
        if score <= 0:
            return "未归类资料", 0.36, "未命中稳定系列规则，进入未归类资料候选。"
        return series, min(0.92, 0.48 + score * 0.11), reason

    def _update_source_structure(
        self,
        source: Mapping[str, object],
        *,
        structure_ref: str,
        structure_id: str,
        content_read_id: str,
        paragraph_tags: Sequence[ParagraphTag],
        tags: Sequence[str],
        summary: str,
        key_points: Sequence[str],
        structured_body: str,
        series_candidate: str,
        series_confidence: float,
        series_reason: str,
        organization_prompt_refs: Sequence[Mapping[str, object]],
        activity_refs: tuple[str, ...],
    ) -> None:
        source_id = _required_str(source, "id")
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        metadata["content_structure"] = {
            "status": "completed",
            "structure_id": structure_id,
            "structure_ref": structure_ref,
            "content_read_id": content_read_id,
            "paragraph_tags": [serialize_paragraph_tag(item) for item in paragraph_tags],
            "tags": list(tags),
            "summary": summary,
            "key_points": list(key_points),
            "structured_body": structured_body,
            "series_candidate": series_candidate,
            "series_confidence": series_confidence,
            "series_reason": series_reason,
            "organization_prompt_refs": [dict(item) for item in organization_prompt_refs],
            "activity_refs": list(activity_refs),
            "memory_publication": "not_started",
            "blocked_operations": list(self._BLOCKED_OPERATIONS),
            "updated_at": self._now,
        }
        updated = dict(source)
        updated["metadata"] = metadata
        self._object_store.write("sources", source_id, updated, expected_revision=None)

    def _write_event(
        self,
        source_id: str,
        *,
        structure_id: str,
        structure_ref: str,
        content_read_id: str,
        tags: Sequence[str],
        series_candidate: str,
        organization_prompt_refs: Sequence[Mapping[str, object]],
    ) -> str:
        event_id = f"event-content-structured-{source_id}"
        event_ref = f"crp://{self._namespace_id}/activity/{event_id}.json"
        self._object_store.write(
            "activity_events",
            event_id,
            {
                "schema_version": "1.0.0",
                "id": event_id,
                "type": "content_structured",
                "source_id": source_id,
                "status": "completed",
                "contentRead": True,
                "memoryPublication": "not_started",
                "details": {
                    "structure_id": structure_id,
                    "structure_ref": structure_ref,
                    "content_read_id": content_read_id,
                    "tags": list(tags),
                    "series_candidate": series_candidate,
                    "organization_prompt_refs": [dict(item) for item in organization_prompt_refs],
                },
                "created_at": self._now,
                "ref": event_ref,
            },
            expected_revision=None,
        )
        return event_ref


def serialize_source_structuring_result(result: SourceStructuringResult) -> dict[str, object]:
    return {
        "status": result.status,
        "source_id": result.source_id,
        "content_read_id": result.content_read_id,
        "paragraph_tags": [serialize_paragraph_tag(item) for item in result.paragraph_tags],
        "tags": list(result.tags),
        "summary": result.summary,
        "key_points": list(result.key_points),
        "structured_body": result.structured_body,
        "series_candidate": result.series_candidate,
        "series_confidence": result.series_confidence,
        "series_reason": result.series_reason,
        "structure_ref": result.structure_ref,
        "activity_refs": list(result.activity_refs),
        "organization_prompt_refs": [dict(item) for item in result.organization_prompt_refs],
        "memory_publication_state": result.memory_publication_state,
        "blocked_operations": list(result.blocked_operations),
    }


def serialize_paragraph_tag(tag: ParagraphTag) -> dict[str, object]:
    return {
        "paragraph_id": tag.paragraph_id,
        "text_preview": tag.text_preview,
        "tags": list(tag.tags),
        "confidence": tag.confidence,
    }


def _paragraphs(text: str) -> tuple[str, ...]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = [part.strip() for part in re.split(r"\n\s*\n|\n", normalized) if part.strip()]
    if len(parts) <= 1:
        sentence_parts = [
            part.strip()
            for part in re.split(r"(?<=[。！？.!?])\s+", normalized)
            if part.strip()
        ]
        if len(sentence_parts) > 1:
            return tuple(sentence_parts)
    return tuple(parts)


def _summary(text: str, limit: int) -> str:
    paragraphs = _paragraphs(text)
    if paragraphs:
        return _preview(paragraphs[0], max(limit, 80))
    return _preview(text, max(limit, 80))


def _key_points(paragraph_tags: Sequence[ParagraphTag]) -> tuple[str, ...]:
    points: list[str] = []
    seen: set[str] = set()
    for paragraph in paragraph_tags:
        point = paragraph.text_preview.strip()
        if not point or point in seen:
            continue
        seen.add(point)
        points.append(point)
        if len(points) >= 6:
            break
    return tuple(points)


def _structured_body(paragraph_tags: Sequence[ParagraphTag]) -> str:
    lines: list[str] = []
    for paragraph in paragraph_tags:
        tags = "、".join(paragraph.tags) if paragraph.tags else "Knowledge"
        lines.append(f"- {paragraph.paragraph_id} [{tags}] {paragraph.text_preview}")
    return "\n".join(lines)


def _tags_for_text(text: str, rules: Sequence[tuple[str, Sequence[str]]]) -> tuple[str, ...]:
    lower = text.lower()
    tags: list[str] = []
    for tag, keywords in rules:
        if any(keyword.lower() in lower for keyword in keywords):
            tags.append(tag)
    return tuple(tags)


def _top_tags(paragraph_tags: Sequence[ParagraphTag]) -> tuple[str, ...]:
    counter: Counter[str] = Counter()
    for paragraph in paragraph_tags:
        counter.update(paragraph.tags)
    ordered = [tag for tag, _count in counter.most_common()]
    return tuple(ordered[:8])


def _prompt_refs(prompt_context: Sequence[Mapping[str, object]]) -> tuple[Mapping[str, object], ...]:
    refs: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for prompt in prompt_context:
        prompt_id = _optional_text(prompt.get("id"))
        if prompt_id is None or prompt_id in seen:
            continue
        seen.add(prompt_id)
        ref: dict[str, object] = {"id": prompt_id}
        revision = prompt.get("revision")
        if isinstance(revision, int):
            ref["revision"] = revision
        source = _optional_text(prompt.get("source"))
        if source is not None:
            ref["source"] = source
        stage_id = _optional_text(prompt.get("stage_id"))
        if stage_id is not None:
            ref["stage_id"] = stage_id
        model_profile_id = _optional_text(prompt.get("model_profile_id"))
        if model_profile_id is not None:
            ref["model_profile_id"] = model_profile_id
        refs.append(ref)
    return tuple(refs)


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


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _preview(text: str, limit: int) -> str:
    compact = " ".join(text.strip().split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1]}..."


def _required_str(source: Mapping[str, object], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value:
        raise SourceStructuringError(f"source requires {key}")
    return value


def _resolve_model_profile_refs(
    task_model_map: Mapping[str, object] | None,
    use_keys: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    """从 task_model_map 解析出业务流程可消费的 model_profile 引用列表。

    task_model_map 为 None 或空时返回空 tuple（向后兼容，不破坏既有调用）。
    """
    if not task_model_map:
        return ()
    resolver = TaskModelMapResolver(task_model_map)
    return resolver.refs_for(use_keys)
