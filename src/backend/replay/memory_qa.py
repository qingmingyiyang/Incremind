from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
import re
from typing import Callable, Literal
from uuid import uuid4

from backend.replay.contracts import (
    AssetMetadata,
    KnowledgeEvidence,
    KnowledgeIndex,
    KnowledgeItem,
    KnowledgeLinks,
    KnowledgeSource,
    KnowledgeStatus,
    MemoryAnswer,
    MemoryQuestionRequest,
    MemoryReference,
    Report,
)
from backend.replay.library import ReplayLibrary


NO_EVIDENCE = "当前系列资料库中没有找到足够证据。"
MAX_MEMORY_ASSET_TEXT_CHARS = 20_000
ResolvedScope = Literal["video", "day", "week", "month", "year", "all"]


@dataclass
class _Candidate:
    title: str
    text: str
    score: int
    references: list[MemoryReference]
    item_id: str = ""
    report_id: str = ""


class MemoryQAService:
    def __init__(self, library: ReplayLibrary, *, today_provider: Callable[[], date] | None = None) -> None:
        self.library = library
        self._today_provider = today_provider or date.today

    def ask(self, request: MemoryQuestionRequest) -> MemoryAnswer:
        question = request.question.strip()
        if not question:
            raise ValueError("问题不能为空")
        scope, scope_value = resolve_scope(question, request.scope, request.scope_value, today=self._today_provider())
        tokens = _topic_tokens(question)
        candidates = self._candidates(scope, scope_value, tokens)
        candidates.sort(key=lambda item: item.score, reverse=True)
        matched_count = len(candidates)
        selected = candidates[: request.limit]
        references = _dedupe_references([reference for item in selected for reference in item.references])[:30]
        for reference in references:
            reference.series_id = self.library.series_id or request.series_id
        sufficient = bool(selected and references)
        if sufficient:
            lines = [f"根据资料库，共找到 {matched_count} 条相关记录："]
            lines.extend(f"- {item.title}：{_excerpt(item.text)}" for item in selected)
            if matched_count > len(selected):
                lines.append(f"本次展示前 {len(selected)} 条，完整内容仍保存在本地资料库。")
            answer_text = "\n".join(lines)
        else:
            answer_text = NO_EVIDENCE
        answer = MemoryAnswer(
            answer=answer_text,
            scope=scope,
            scope_value=scope_value,
            matched_count=matched_count,
            evidence_sufficient=sufficient,
            references=references if sufficient else [],
        )
        if request.persist:
            saved = self.library.save_item(_qa_item(question, answer))
            answer.saved_item_id = saved.id
        return answer

    def _candidates(self, scope: ResolvedScope, value: str, tokens: list[str]) -> list[_Candidate]:
        start, end = _scope_dates(scope, value)
        candidates: list[_Candidate] = []
        for item in self.library.list_items():
            if item.type == "report" or not _item_in_scope(item, scope, value, start, end):
                continue
            asset_text, asset_references = _asset_evidence(self.library, item)
            evidence_labels = []
            if item.evidence.frame_ids:
                evidence_labels.extend(["画面", "视觉", "关键帧"])
            if item.evidence.chunk_ids:
                evidence_labels.extend(["原文", "分块"])
            text = " ".join(
                [
                    item.title,
                    item.summary,
                    item.content,
                    *item.tags,
                    *item.index.keywords,
                    *item.evidence.quotes,
                    *item.evidence.chunk_ids,
                    *item.evidence.frame_ids,
                    *evidence_labels,
                    asset_text,
                ]
            )
            score = _score(text, tokens, item.type)
            if score > 0 or not tokens:
                candidates.append(
                    _Candidate(
                        title=item.title,
                        text=item.summary or item.content or item.title,
                        score=max(1, score),
                        references=[*asset_references, *_item_references(item)],
                        item_id=item.id,
                    )
                )
        for report in self.library.list_reports():
            if not _report_in_scope(report, scope, value, start, end):
                continue
            text = _report_text(report)
            score = _score(text, tokens, report.type)
            if score > 0 or not tokens:
                candidates.append(
                    _Candidate(
                        title=report.title,
                        text=report.summary.one_sentence or report.title,
                        score=max(1, score + 2),
                        references=_report_references(report),
                        report_id=report.report_id,
                    )
                )
        return candidates


def resolve_scope(
    question: str,
    requested: str,
    scope_value: str,
    *,
    today: date,
) -> tuple[ResolvedScope, str]:
    if requested != "auto":
        return _validate_scope(requested, scope_value, today)
    bvid = re.search(r"BV[0-9A-Za-z]+", question, re.IGNORECASE)
    if bvid:
        return "video", bvid.group(0)
    full_date = re.search(r"\b\d{4}-\d{2}-\d{2}\b", question)
    if full_date:
        return _validate_scope("day", full_date.group(0), today)
    week = re.search(r"\b\d{4}-W\d{2}\b", question, re.IGNORECASE)
    if week:
        return _validate_scope("week", week.group(0).upper(), today)
    month = re.search(r"\b\d{4}-\d{2}\b", question)
    if month:
        return _validate_scope("month", month.group(0), today)
    if "昨天" in question:
        return "day", (today - timedelta(days=1)).isoformat()
    if "今天" in question:
        return "day", today.isoformat()
    if any(value in question for value in ["本周", "这周"]):
        iso = today.isocalendar()
        return "week", f"{iso.year}-W{iso.week:02d}"
    if any(value in question for value in ["本月", "这个月"]):
        return "month", today.strftime("%Y-%m")
    if "今年" in question:
        return "year", str(today.year)
    year = re.search(r"\b\d{4}\b", question)
    if year:
        return _validate_scope("year", year.group(0), today)
    return "all", ""


def _validate_scope(requested: str, value: str, today: date) -> tuple[ResolvedScope, str]:
    if requested == "all":
        return "all", ""
    if requested == "video":
        if not value.strip():
            raise ValueError("视频范围需要 video_id、BVID 或标题")
        return "video", value.strip()
    if requested == "day":
        parsed = date.fromisoformat(value or today.isoformat())
        return "day", parsed.isoformat()
    if requested == "week":
        match = re.fullmatch(r"(\d{4})-W(\d{2})", value)
        if not match:
            raise ValueError("周范围必须使用 YYYY-Www")
        try:
            date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
        except ValueError as error:
            raise ValueError("周范围必须使用有效的 YYYY-Www") from error
        return "week", value
    if requested == "month":
        match = re.fullmatch(r"(\d{4})-(\d{2})", value)
        if not match:
            raise ValueError("月范围必须使用 YYYY-MM")
        try:
            monthrange(int(match.group(1)), int(match.group(2)))
        except ValueError as error:
            raise ValueError("月范围必须使用有效的 YYYY-MM") from error
        return "month", value
    if requested == "year":
        if not re.fullmatch(r"\d{4}", value) or int(value) < 1:
            raise ValueError("年范围必须使用 YYYY")
        return "year", value
    raise ValueError("不支持的问答范围")


def _scope_dates(scope: ResolvedScope, value: str) -> tuple[date | None, date | None]:
    if scope == "day":
        parsed = date.fromisoformat(value)
        return parsed, parsed
    if scope == "week":
        year, week = value.split("-W")
        return date.fromisocalendar(int(year), int(week), 1), date.fromisocalendar(int(year), int(week), 7)
    if scope == "month":
        year, month = map(int, value.split("-"))
        return date(year, month, 1), date(year, month, monthrange(year, month)[1])
    if scope == "year":
        year = int(value)
        return date(year, 1, 1), date(year, 12, 31)
    return None, None


def _item_in_scope(item: KnowledgeItem, scope: ResolvedScope, value: str, start, end) -> bool:
    if scope == "all":
        return True
    if scope == "video":
        haystack = " ".join([item.id, item.title, item.source.bvid, *item.links.linked_video_ids]).lower()
        return item.type == "video" and value.lower() in haystack
    created = date.fromisoformat(item.created_at[:10])
    return start <= created <= end


def _report_in_scope(report: Report, scope: ResolvedScope, value: str, start, end) -> bool:
    if scope == "all":
        return True
    if scope == "video":
        haystack = " ".join([video.video_id + " " + video.bvid + " " + video.title for video in report.video_knowledge]).lower()
        return value.lower() in haystack
    expected_type = {"day": "daily", "week": "weekly", "month": "monthly", "year": "yearly"}[scope]
    if report.type != expected_type:
        return False
    report_start = date.fromisoformat(report.date_range.start)
    report_end = date.fromisoformat(report.date_range.end)
    return report_start <= end and report_end >= start


def _topic_tokens(question: str) -> list[str]:
    cleaned = re.sub(r"BV[0-9A-Za-z]+|\d{4}(?:-W\d{2}|-\d{2}(?:-\d{2})?)?", " ", question, flags=re.IGNORECASE)
    for phrase in ["最重要", "知识主题", "主题", "知识", "我", "什么", "哪些", "这个", "本周", "这周", "本月", "这个月", "今年", "昨天", "今天", "最近", "看过", "学了", "资料库", "主要", "相关", "有没有"]:
        cleaned = cleaned.replace(phrase, " ")
    tokens = re.findall(r"[A-Za-z0-9_+-]{2,}", cleaned.lower())
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", cleaned):
        tokens.append(segment)
        if len(segment) > 2:
            tokens.extend(segment[index : index + 2] for index in range(len(segment) - 1))
    return list(dict.fromkeys(token for token in tokens if token.strip()))


def _score(text: str, tokens: list[str], source_type: str) -> int:
    lowered = text.lower()
    score = sum(2 if token.lower() in lowered else 0 for token in tokens)
    if "行动" in tokens and source_type == "action":
        score += 4
    if "问题" in tokens and source_type == "question":
        score += 4
    return score


def _report_text(report: Report) -> str:
    values = [
        report.title,
        report.summary.one_sentence,
        *report.summary.main_topics,
        *report.summary.key_takeaways,
        *report.summary.important_questions,
        *report.summary.action_items,
        *report.review.long_term_patterns,
        *[video.title + " " + video.summary + " " + video.bvid for video in report.video_knowledge],
        *[item.title + " " + item.summary for item in report.personal_knowledge],
        *[item.quote for item in report.evidence],
    ]
    return " ".join(values)


def _item_references(item: KnowledgeItem) -> list[MemoryReference]:
    count = max(1, len(item.evidence.chunk_ids), len(item.evidence.frame_ids), len(item.evidence.timestamps), len(item.evidence.quotes))
    video_id = item.links.linked_video_ids[0] if item.links.linked_video_ids else ""
    return [
        MemoryReference(
            source_type=item.type,
            title=item.title,
            item_id=item.id,
            video_id=video_id,
            bvid=item.source.bvid,
            chunk_id=_at(item.evidence.chunk_ids, index),
            timestamp=_at(item.evidence.timestamps, index),
            frame_id=_at(item.evidence.frame_ids, index),
            quote=_at(item.evidence.quotes, index) or item.summary or item.content,
        )
        for index in range(count)
    ]


def _asset_evidence(library: ReplayLibrary, item: KnowledgeItem) -> tuple[str, list[MemoryReference]]:
    search_values: list[str] = []
    references: list[MemoryReference] = []
    metadata_root = (library.root / "assets" / "metadata").resolve()
    for asset_id in item.links.asset_ids:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", asset_id):
            continue
        metadata_path = (metadata_root / f"{asset_id}.json").resolve()
        try:
            metadata_path.relative_to(metadata_root)
            metadata = AssetMetadata.model_validate_json(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if metadata.asset_id != asset_id or (library.series_id and metadata.series_id != library.series_id):
            continue
        extracted_text = _read_asset_text(library.root, metadata.extracted_text_path)
        search_values.extend(
            value
            for value in [metadata.filename, metadata.media_type, metadata.manual_summary, extracted_text]
            if value
        )
        references.append(
            MemoryReference(
                source_type="asset",
                title=metadata.filename,
                item_id=item.id,
                asset_id=metadata.asset_id,
                quote=metadata.manual_summary or _excerpt(extracted_text) or metadata.filename,
            )
        )
    return " ".join(search_values), references


def _read_asset_text(root, relative_path: str) -> str:
    if not relative_path:
        return ""
    allowed_root = (root / "assets" / "extracted").resolve()
    target = (root / relative_path).resolve()
    try:
        target.relative_to(allowed_root)
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(MAX_MEMORY_ASSET_TEXT_CHARS)
    except (OSError, ValueError):
        return ""


def _report_references(report: Report) -> list[MemoryReference]:
    references = [
        MemoryReference(source_type="report", title=report.title, report_id=report.report_id, quote=report.summary.one_sentence)
    ]
    references.extend(
        MemoryReference(
            source_type=item.source_type,
            title=report.title,
            report_id=report.report_id,
            item_id=item.source_id if item.source_type != "report" else "",
            video_id=item.video_id,
            bvid=item.bvid,
            chunk_id=item.chunk_id,
            timestamp=item.timestamp,
            frame_id=item.frame_id,
            quote=item.quote,
        )
        for item in report.evidence
    )
    return references


def _dedupe_references(values: list[MemoryReference]) -> list[MemoryReference]:
    result: list[MemoryReference] = []
    seen: set[tuple[str, ...]] = set()
    for value in values:
        key = (
            value.report_id,
            value.item_id,
            value.asset_id,
            value.video_id,
            value.chunk_id,
            value.timestamp,
            value.frame_id,
            value.quote,
        )
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _qa_item(question: str, answer: MemoryAnswer) -> KnowledgeItem:
    references = answer.references
    return KnowledgeItem(
        id=f"qa_{uuid4().hex}",
        type="question",
        title=question[:80],
        content=question,
        summary=answer.answer,
        tags=_topic_tokens(question)[:12],
        source=KnowledgeSource(kind="qa"),
        evidence=KnowledgeEvidence(
            chunk_ids=list(dict.fromkeys(item.chunk_id for item in references if item.chunk_id)),
            frame_ids=list(dict.fromkeys(item.frame_id for item in references if item.frame_id)),
            timestamps=list(dict.fromkeys(item.timestamp for item in references if item.timestamp)),
            quotes=list(dict.fromkeys(item.quote for item in references if item.quote)),
        ),
        links=KnowledgeLinks(
            related_items=list(dict.fromkeys([item.item_id for item in references if item.item_id] + [item.report_id for item in references if item.report_id])),
            linked_video_ids=list(dict.fromkeys(item.video_id for item in references if item.video_id)),
        ),
        status=KnowledgeStatus(in_inbox=not answer.evidence_sufficient, need_review=not answer.evidence_sufficient),
        index=KnowledgeIndex(available=answer.evidence_sufficient, keywords=_topic_tokens(question)[:20]),
    )


def _at(values: list[str], index: int) -> str:
    return values[index] if index < len(values) else ""


def _excerpt(value: str, limit: int = 240) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[:limit] + "…"
