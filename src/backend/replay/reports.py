from __future__ import annotations

from collections import Counter
from calendar import monthrange
from datetime import date
import re

from backend.replay.contracts import (
    DateRange,
    KnowledgeIndex,
    KnowledgeItem,
    KnowledgeLinks,
    KnowledgeSource,
    KnowledgeStatus,
    ReplayTask,
    Report,
    ReportEvidence,
    ReportIndex,
    ReportPersonalKnowledge,
    ReportReview,
    ReportSources,
    ReportSummary,
    ReportVideoKnowledge,
    local_now_iso,
)
from backend.replay.library import ReplayLibrary


class ReportService:
    def __init__(self, library: ReplayLibrary) -> None:
        self.library = library

    def generate_daily(self, day: str) -> Report:
        _validate_date(day)
        date_range = DateRange(start=day, end=day)
        task = ReplayTask(type="daily_report", status="pending", date_range=date_range)
        self.library.save_task(task)
        try:
            task.status = "running"
            task.updated_at = local_now_iso()
            self.library.save_task(task)
            items = self.library.journal_items(day)
            report = self._build_daily(day, items)
            markdown = render_report_markdown(report)
            output_files = self.library.save_report(report, markdown)
            output_files.extend(self.library.save_daily_bundle(report, markdown, items))
            self.library.save_item(_report_knowledge_item(report, output_files[1]))
            task.status = "success"
            task.source_count = len(items)
            task.output_files = [str(path) for path in output_files]
            task.updated_at = local_now_iso()
            self.library.save_task(task)
            return report
        except Exception as error:
            task.status = "failed"
            task.error = str(error)
            task.updated_at = local_now_iso()
            self.library.save_task(task)
            raise

    def generate_weekly(self, week: str) -> Report:
        start, end = _iso_week_range(week)
        reports = [
            report
            for report in self.library.list_reports("daily")
            if start <= date.fromisoformat(report.date_range.start) <= end
        ]
        return self._generate_rollup(
            report_type="weekly",
            report_id=f"weekly_{week}",
            title=f"{week} 周报",
            date_range=DateRange(start=start.isoformat(), end=end.isoformat()),
            source_reports=reports,
            task_type="weekly_report",
            source_label="日报",
        )

    def generate_monthly(self, month: str) -> Report:
        start, end = _month_range(month)
        reports = [
            report
            for report in self.library.list_reports("weekly")
            if date.fromisoformat(report.date_range.start) <= end
            and date.fromisoformat(report.date_range.end) >= start
        ]
        return self._generate_rollup(
            report_type="monthly",
            report_id=f"monthly_{month}",
            title=f"{month} 月报",
            date_range=DateRange(start=start.isoformat(), end=end.isoformat()),
            source_reports=reports,
            task_type="monthly_report",
            source_label="周报",
        )

    def generate_yearly(self, year: str) -> Report:
        start, end = _year_range(year)
        reports = [
            report
            for report in self.library.list_reports("monthly")
            if date.fromisoformat(report.date_range.start) <= end
            and date.fromisoformat(report.date_range.end) >= start
        ]
        return self._generate_rollup(
            report_type="yearly",
            report_id=f"yearly_{year}",
            title=f"{year} 年报",
            date_range=DateRange(start=start.isoformat(), end=end.isoformat()),
            source_reports=reports,
            task_type="yearly_report",
            source_label="月报",
        )

    def _generate_rollup(
        self,
        *,
        report_type: str,
        report_id: str,
        title: str,
        date_range: DateRange,
        source_reports: list[Report],
        task_type: str,
        source_label: str,
    ) -> Report:
        task = ReplayTask(type=task_type, status="pending", date_range=date_range)
        self.library.save_task(task)
        try:
            task.status = "running"
            task.updated_at = local_now_iso()
            self.library.save_task(task)
            report = self._build_rollup(
                report_type=report_type,
                report_id=report_id,
                title=title,
                date_range=date_range,
                source_reports=source_reports,
                source_label=source_label,
            )
            markdown = render_report_markdown(report)
            output_files = self.library.save_report(report, markdown)
            self.library.save_item(_report_knowledge_item(report, output_files[1]))
            task.status = "success"
            task.source_count = len(source_reports)
            task.output_files = [str(path) for path in output_files]
            task.updated_at = local_now_iso()
            self.library.save_task(task)
            return report
        except Exception as error:
            task.status = "failed"
            task.error = str(error)
            task.updated_at = local_now_iso()
            self.library.save_task(task)
            raise

    def _build_daily(self, day: str, items: list[KnowledgeItem]) -> Report:
        existing = self.library.get_report(f"daily_{day}")
        grouped = {
            field: [item for item in items if _ITEM_TO_SOURCE.get(item.type) == field]
            for field in _SOURCE_FIELDS
        }
        keywords = _top_keywords(items)
        videos = grouped["videos"]
        personal = [item for item in items if item.type not in {"video", "report"}]
        report = Report(
            report_id=f"daily_{day}",
            type="daily",
            title=f"{day} 日报",
            date_range=DateRange(start=day, end=day),
            created_at=existing.created_at if existing else local_now_iso(),
            updated_at=local_now_iso(),
            sources=ReportSources(**{field: [item.id for item in grouped[field]] for field in _SOURCE_FIELDS}),
            summary=ReportSummary(
                one_sentence=_daily_sentence(items),
                main_topics=keywords[:8],
                key_takeaways=_unique([item.summary or item.title for item in items if item.type in {"video", "note", "thought"}]),
                important_questions=_unique([item.summary or item.content for item in grouped["questions"]]),
                action_items=_unique([item.summary or item.content for item in grouped["actions"]]),
                review_suggestions=_review_suggestions(items),
            ),
            video_knowledge=[_video_knowledge(item) for item in videos],
            personal_knowledge=[
                ReportPersonalKnowledge(
                    item_id=item.id,
                    type=item.type,
                    title=item.title,
                    summary=item.summary,
                    tags=item.tags,
                )
                for item in personal
            ],
            review=ReportReview(
                what_i_learned=_unique([item.summary or item.title for item in items if item.type not in {"action", "question"}]),
                what_i_should_revisit=_unique([item.title for item in items if item.status.need_review or item.status.high_value]),
                open_questions=_unique([item.summary or item.content for item in grouped["questions"]]),
                next_actions=_unique([item.summary or item.content for item in grouped["actions"]]),
                long_term_patterns=[keyword for keyword, count in _keyword_counts(items).most_common(8) if count > 1],
            ),
            evidence=[evidence for item in items for evidence in _item_evidence(item)],
            index=ReportIndex(
                available=True,
                keywords=keywords,
                linked_item_ids=[item.id for item in items],
            ),
        )
        return report

    def _build_rollup(
        self,
        *,
        report_type: str,
        report_id: str,
        title: str,
        date_range: DateRange,
        source_reports: list[Report],
        source_label: str,
    ) -> Report:
        existing = self.library.get_report(report_id)
        sources = ReportSources(
            **{
                field: _unique([source_id for report in source_reports for source_id in getattr(report.sources, field)])
                for field in _SOURCE_FIELDS
            },
            previous_reports=[report.report_id for report in source_reports],
        )
        video_knowledge = _dedupe_models(
            [video for report in source_reports for video in report.video_knowledge],
            key="video_id",
        )
        personal_knowledge = _dedupe_models(
            [item for report in source_reports for item in report.personal_knowledge],
            key="item_id",
        )
        keywords = _rank_report_values(source_reports, "main_topics")
        linked_items = _unique([item_id for report in source_reports for item_id in report.index.linked_item_ids])
        report_links = [report.report_id for report in source_reports]
        evidence = [item for report in source_reports for item in report.evidence]
        evidence.extend(
            ReportEvidence(
                source_type="report",
                source_id=report.report_id,
                quote=report.summary.one_sentence,
            )
            for report in source_reports
        )
        return Report(
            report_id=report_id,
            type=report_type,
            title=title,
            date_range=date_range,
            created_at=existing.created_at if existing else local_now_iso(),
            updated_at=local_now_iso(),
            sources=sources,
            summary=ReportSummary(
                one_sentence=f"本周期汇总 {len(source_reports)} 篇{source_label}，覆盖 {date_range.start} 至 {date_range.end}。",
                main_topics=keywords[:12],
                key_takeaways=_merge_summary_values(source_reports, "key_takeaways"),
                important_questions=_merge_summary_values(source_reports, "important_questions"),
                action_items=_merge_summary_values(source_reports, "action_items"),
                review_suggestions=_merge_summary_values(source_reports, "review_suggestions"),
            ),
            video_knowledge=video_knowledge,
            personal_knowledge=personal_knowledge,
            review=ReportReview(
                what_i_learned=_merge_review_values(source_reports, "what_i_learned"),
                what_i_should_revisit=_merge_review_values(source_reports, "what_i_should_revisit"),
                open_questions=_merge_review_values(source_reports, "open_questions"),
                next_actions=_merge_review_values(source_reports, "next_actions"),
                long_term_patterns=_rank_review_patterns(source_reports),
            ),
            evidence=evidence,
            index=ReportIndex(
                available=True,
                keywords=keywords,
                linked_item_ids=linked_items,
                linked_report_ids=report_links,
            ),
        )


_SOURCE_FIELDS = ("videos", "notes", "clips", "thoughts", "actions", "questions")
_ITEM_TO_SOURCE = {
    "video": "videos",
    "note": "notes",
    "clip": "clips",
    "thought": "thoughts",
    "action": "actions",
    "question": "questions",
}


def _validate_date(value: str) -> None:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("日期必须使用 YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise ValueError("日期必须使用 YYYY-MM-DD")


def _iso_week_range(value: str) -> tuple[date, date]:
    match = re.fullmatch(r"(\d{4})-W(\d{2})", value)
    if match is None:
        raise ValueError("周编号必须使用 YYYY-Www")
    try:
        start = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
        end = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 7)
    except ValueError as error:
        raise ValueError("周编号必须使用有效的 YYYY-Www") from error
    return start, end


def _month_range(value: str) -> tuple[date, date]:
    match = re.fullmatch(r"(\d{4})-(\d{2})", value)
    if match is None:
        raise ValueError("月份必须使用 YYYY-MM")
    year = int(match.group(1))
    month = int(match.group(2))
    try:
        start = date(year, month, 1)
        end = date(year, month, monthrange(year, month)[1])
    except ValueError as error:
        raise ValueError("月份必须使用有效的 YYYY-MM") from error
    return start, end


def _year_range(value: str) -> tuple[date, date]:
    if re.fullmatch(r"\d{4}", value) is None:
        raise ValueError("年份必须使用 YYYY")
    year = int(value)
    if year < 1:
        raise ValueError("年份必须使用有效的 YYYY")
    return date(year, 1, 1), date(year, 12, 31)


def _daily_sentence(items: list[KnowledgeItem]) -> str:
    counts = Counter(item.type for item in items)
    return (
        f"当天沉淀 {len(items)} 条知识，包括 {counts['video']} 个视频、{counts['note'] + counts['thought']} 条笔记想法、"
        f"{counts['clip']} 条剪贴、{counts['action']} 个行动和 {counts['question']} 个问题。"
    )


def _keyword_counts(items: list[KnowledgeItem]) -> Counter[str]:
    return Counter(keyword for item in items for keyword in [*item.tags, *item.index.keywords] if keyword)


def _top_keywords(items: list[KnowledgeItem]) -> list[str]:
    return [keyword for keyword, _ in _keyword_counts(items).most_common(20)]


def _review_suggestions(items: list[KnowledgeItem]) -> list[str]:
    suggestions = [f"复看：{item.title}" for item in items if item.status.need_review]
    suggestions.extend(f"继续沉淀高价值内容：{item.title}" for item in items if item.status.high_value)
    return _unique(suggestions)


def _video_knowledge(item: KnowledgeItem) -> ReportVideoKnowledge:
    video_id = item.links.linked_video_ids[0] if item.links.linked_video_ids else item.id
    return ReportVideoKnowledge(
        video_id=video_id,
        title=item.title,
        bvid=item.source.bvid,
        summary=item.summary,
        important_timestamps=item.evidence.timestamps,
        visual_frame_ids=item.evidence.frame_ids,
        linked_markdown=item.source.local_path,
    )


def _item_evidence(item: KnowledgeItem) -> list[ReportEvidence]:
    count = max(
        1,
        len(item.evidence.timestamps),
        len(item.evidence.chunk_ids),
        len(item.evidence.frame_ids),
        len(item.evidence.quotes),
    )
    video_id = item.links.linked_video_ids[0] if item.type == "video" and item.links.linked_video_ids else ""
    return [
        ReportEvidence(
            source_type=item.type,
            source_id=item.id,
            video_id=video_id,
            bvid=item.source.bvid if item.type == "video" else "",
            timestamp=_at(item.evidence.timestamps, index),
            chunk_id=_at(item.evidence.chunk_ids, index),
            frame_id=_at(item.evidence.frame_ids, index),
            quote=_at(item.evidence.quotes, index) or item.summary,
        )
        for index in range(count)
    ]


def _at(values: list[str], index: int) -> str:
    return values[index] if index < len(values) else ""


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _merge_summary_values(reports: list[Report], field: str) -> list[str]:
    return _unique([value for report in reports for value in getattr(report.summary, field)])


def _merge_review_values(reports: list[Report], field: str) -> list[str]:
    return _unique([value for report in reports for value in getattr(report.review, field)])


def _rank_report_values(reports: list[Report], field: str) -> list[str]:
    counts = Counter(value for report in reports for value in getattr(report.summary, field))
    return [value for value, _ in counts.most_common(20)]


def _rank_review_patterns(reports: list[Report]) -> list[str]:
    counts = Counter(value for report in reports for value in [*report.review.long_term_patterns, *report.summary.main_topics])
    return [value for value, count in counts.most_common(12) if count > 1]


def _dedupe_models(values: list, *, key: str) -> list:
    result = []
    seen: set[str] = set()
    for value in values:
        identity = str(getattr(value, key))
        if identity not in seen:
            seen.add(identity)
            result.append(value)
    return result


def _report_knowledge_item(report: Report, markdown_path) -> KnowledgeItem:
    return KnowledgeItem(
        id=f"report_{report.report_id}",
        type="report",
        title=report.title,
        content=report.summary.one_sentence,
        summary=report.summary.one_sentence,
        tags=report.summary.main_topics,
        created_at=report.created_at,
        updated_at=report.updated_at,
        source=KnowledgeSource(kind="report", local_path=str(markdown_path), report_id=report.report_id),
        links=KnowledgeLinks(
            related_items=report.index.linked_item_ids,
            child_reports=report.index.linked_report_ids,
        ),
        status=KnowledgeStatus(),
        index=KnowledgeIndex(
            available=True,
            keywords=report.index.keywords,
            chunk_ids=[],
        ),
    )


def render_report_markdown(report: Report) -> str:
    lines = [
        f"# {report.title}",
        "",
        report.summary.one_sentence,
        "",
        "## 核心主题",
        "",
        *([f"- {topic}" for topic in report.summary.main_topics] or ["- 暂无"]),
        "",
        "## 视频知识",
        "",
    ]
    for video in report.video_knowledge:
        lines.extend(
            [
                f"### {video.title}",
                "",
                f"- video_id：`{video.video_id}`",
                f"- BVID：`{video.bvid}`",
                f"- 摘要：{video.summary or '暂无'}",
                f"- 时间：{', '.join(video.important_timestamps) or '暂无'}",
                f"- 画面：{', '.join(video.visual_frame_ids) or '暂无'}",
                "",
            ]
        )
    if not report.video_knowledge:
        lines.extend(["本周期没有视频知识。", ""])
    lines.extend(["## 个人记录", ""])
    lines.extend(
        [f"- [{item.type}] {item.title}：{item.summary or '暂无摘要'}" for item in report.personal_knowledge]
        or ["- 暂无"]
    )
    lines.extend(["", "## 行动与问题", ""])
    lines.extend([f"- 行动：{item}" for item in report.summary.action_items] or ["- 行动：暂无"])
    lines.extend([f"- 问题：{item}" for item in report.summary.important_questions] or ["- 问题：暂无"])
    lines.extend(["", "## 证据索引", ""])
    for evidence in report.evidence:
        reference = " / ".join(
            value
            for value in [
                evidence.source_id,
                evidence.video_id,
                evidence.bvid,
                evidence.chunk_id,
                evidence.timestamp,
                evidence.frame_id,
            ]
            if value
        )
        lines.append(f"- `{reference}`：{evidence.quote or '来源条目'}")
    if not report.evidence:
        lines.append("- 暂无来源")
    return "\n".join(lines) + "\n"
