from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .ports import ObjectStorePort

from .local_asr_provider_settings import GetLocalAsrProviderSettings
from .local_document_text_extractor_settings import GetLocalDocumentTextExtractorSettings
from .local_ocr_provider_settings import GetLocalOcrProviderSettings
from .local_video_provider_settings import GetLocalVideoProviderSettings
from .transcript_summary_adapter import GetTranscriptSummarySettings


@dataclass(frozen=True, slots=True)
class DailyReminder:
    reminder_id: str
    reminder_type: str
    severity: str
    title: str
    summary: str
    action: str
    project_id: str | None
    related_ids: tuple[str, ...]
    source_refs: tuple[str, ...]
    due_label: str
    created_at: str


@dataclass(frozen=True, slots=True)
class DailyReminderDigest:
    status: str
    digest_id: str
    project_id: str | None
    generated_at: str
    window: str
    reminders: tuple[DailyReminder, ...]
    counts: Mapping[str, int]
    next_focus: str | None
    memory_publication_state: str
    blocked_operations: tuple[str, ...]


class GenerateDailyReminders:
    """Create local reminders from library and project state without side effects."""

    _BLOCKED_OPERATIONS = (
        "model_provider_execution",
        "remote_upload",
        "cookie_read",
        "automatic_long_term_memory_publication",
    )

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        now: str = "2026-07-03T09:00:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._now = now

    def execute(self, *, project_id: str | None = None, limit: int = 8) -> DailyReminderDigest:
        clean_project_id = _clean_optional(project_id)
        clean_limit = max(1, min(int(limit), 20))
        reminders: list[DailyReminder] = []
        reminders.extend(self._pending_memory_candidates(clean_project_id))
        reminders.extend(self._pending_external_agent_drafts(clean_project_id))
        reminders.extend(self._unread_sources(clean_project_id))
        reminders.extend(self._failed_extractions(clean_project_id))
        reminders.extend(self._blocked_audio_workflows(clean_project_id))
        reminders.extend(self._blocked_video_workflows(clean_project_id))
        reminders.extend(self._incomplete_project_skills(clean_project_id))
        reminders.extend(self._local_provider_readiness(clean_project_id))
        ordered = tuple(sorted(reminders, key=_sort_key)[:clean_limit])
        counts = {
            "total": len(ordered),
            "high": sum(1 for item in ordered if item.severity == "high"),
            "medium": sum(1 for item in ordered if item.severity == "medium"),
            "low": sum(1 for item in ordered if item.severity == "low"),
            "pending_review": sum(1 for item in ordered if item.reminder_type.endswith("pending_review")),
            "blocked_workflow": sum(1 for item in ordered if item.reminder_type.endswith("blocked")),
            "source_attention": sum(1 for item in ordered if item.reminder_type.startswith("source_")),
            "failed_extraction": sum(1 for item in ordered if item.reminder_type.startswith("extraction_")),
            "skill_attention": sum(1 for item in ordered if item.reminder_type.startswith("project_skill_")),
            "provider_attention": sum(1 for item in ordered if item.reminder_type.startswith("provider_")),
        }
        return DailyReminderDigest(
            status="ready" if ordered else "quiet",
            digest_id="daily-reminders-" + (clean_project_id or "default"),
            project_id=clean_project_id,
            generated_at=self._now,
            window="today_and_recent",
            reminders=ordered,
            counts=counts,
            next_focus=ordered[0].action if ordered else None,
            memory_publication_state="not_published",
            blocked_operations=self._BLOCKED_OPERATIONS,
        )

    def _pending_memory_candidates(self, project_id: str | None) -> list[DailyReminder]:
        candidates = [
            item
            for item in self._object_store.list("memory_candidates")
            if item.get("status") == "pending_review" and _matches_project(item, project_id)
        ]
        if not candidates:
            return []
        return [
            DailyReminder(
                reminder_id="reminder-memory-candidates-pending-review",
                reminder_type="memory_candidate_pending_review",
                severity="high",
                title="有待确认的长期记忆候选",
                summary=f"{len(candidates)} 条 Memory Candidate 等待确认，确认后才会进入长期记忆。",
                action="review_memory_candidates",
                project_id=project_id,
                related_ids=tuple(_record_id(item) for item in candidates[:10]),
                source_refs=tuple(_refs_from_records(candidates)),
                due_label="today",
                created_at=self._now,
            )
        ]

    def _pending_external_agent_drafts(self, project_id: str | None) -> list[DailyReminder]:
        drafts = [
            item
            for item in self._object_store.list("external_agent_review_drafts")
            if item.get("status") == "pending_review" and _matches_project(item, project_id)
        ]
        if not drafts:
            return []
        return [
            DailyReminder(
                reminder_id="reminder-external-agent-drafts-pending-review",
                reminder_type="external_agent_review_draft_pending_review",
                severity="high",
                title="有外部 Agent 草稿待审核",
                summary=f"{len(drafts)} 条外部 Agent 草稿等待确认，可先预览差异再应用。",
                action="review_external_agent_drafts",
                project_id=project_id,
                related_ids=tuple(_record_id(item) for item in drafts[:10]),
                source_refs=tuple(_refs_from_records(drafts)),
                due_label="today",
                created_at=self._now,
            )
        ]

    def _blocked_audio_workflows(self, project_id: str | None) -> list[DailyReminder]:
        workflows = [
            item
            for item in self._object_store.list("audio_auto_workflows")
            if item.get("status") == "blocked" and _matches_project(item, project_id)
        ]
        if not workflows:
            return []
        next_steps = sorted({_optional_str(item.get("next_step")) or "check_audio_workflow" for item in workflows})
        return [
            DailyReminder(
                reminder_id="reminder-audio-workflows-blocked",
                reminder_type="audio_auto_workflow_blocked",
                severity="medium",
                title="有音频入库流程被阻塞",
                summary=f"{len(workflows)} 个音频流程需要处理，下一步：{', '.join(next_steps[:3])}。",
                action="resolve_audio_auto_workflow",
                project_id=project_id,
                related_ids=tuple(_record_id(item) for item in workflows[:10]),
                source_refs=tuple(_refs_from_records(workflows)),
                due_label="recent",
                created_at=self._now,
            )
        ]

    def _unread_sources(self, project_id: str | None) -> list[DailyReminder]:
        sources = [
            item
            for item in self._object_store.list("sources")
            if _matches_project(item, project_id) and _source_needs_content_read(item)
        ]
        if not sources:
            return []
        type_counts = _count_by_type(sources)
        return [
            DailyReminder(
                reminder_id="reminder-sources-need-content-read",
                reminder_type="source_content_read_needed",
                severity="medium",
                title="有资料还没有读取正文",
                summary=f"{len(sources)} 条 Source 还没有完成正文读取，类型：{type_counts}。",
                action="read_source_content",
                project_id=project_id,
                related_ids=tuple(_record_id(item) for item in sources[:10]),
                source_refs=tuple(_refs_from_records(sources)),
                due_label="recent",
                created_at=self._now,
            )
        ]

    def _blocked_video_workflows(self, project_id: str | None) -> list[DailyReminder]:
        workflows = [
            item
            for item in self._object_store.list("video_auto_workflows")
            if item.get("status") == "blocked" and _matches_project(item, project_id)
        ]
        if not workflows:
            return []
        blocked_ops = sorted(
            {
                op
                for item in workflows
                for op in _str_list(item.get("blocked_operations"))
            }
        )
        next_steps = sorted(
            {
                _optional_str(item.get("summary_next_step")) or _optional_str(item.get("next_step")) or ""
                for item in workflows
            }
            - {""}
        )
        detail = ", ".join(next_steps[:3] or blocked_ops[:3]) if (next_steps or blocked_ops) else "check_video_workflow"
        return [
            DailyReminder(
                reminder_id="reminder-video-workflows-blocked",
                reminder_type="video_auto_workflow_blocked",
                severity="medium",
                title="有视频入库流程被阻塞",
                summary=f"{len(workflows)} 个视频流程需要处理，阻塞点：{detail}。",
                action="resolve_video_auto_workflow",
                project_id=project_id,
                related_ids=tuple(_record_id(item) for item in workflows[:10]),
                source_refs=tuple(_refs_from_records(workflows)),
                due_label="recent",
                created_at=self._now,
            )
        ]

    def _failed_extractions(self, project_id: str | None) -> list[DailyReminder]:
        failed_records: list[Mapping[str, object]] = []
        for collection in ("media_processing_jobs", "media_processing_outputs", "source_content_reads"):
            for item in self._object_store.list(collection):
                if item.get("status") != "failed":
                    continue
                if not self._matches_project_or_source(item, project_id):
                    continue
                record = dict(item)
                record["_collection"] = collection
                failed_records.append(record)
        for source in self._object_store.list("sources"):
            if not _matches_project(source, project_id):
                continue
            if _source_has_failed_processing(source):
                failed_records.append(source)
        unique = _unique_records(failed_records)
        if not unique:
            return []
        type_counts = _count_failed_extraction_types(unique)
        return [
            DailyReminder(
                reminder_id="reminder-extractions-failed-review-needed",
                reminder_type="extraction_failed_review_needed",
                severity="medium",
                title="有资料抽取失败需要查看",
                summary=f"{len(unique)} 条 OCR / ASR / 文档或媒体抽取结果失败，类型：{type_counts}。",
                action="review_failed_extractions",
                project_id=project_id,
                related_ids=tuple(_record_id(item) for item in unique[:10]),
                source_refs=tuple(_refs_from_records(unique)),
                due_label="recent",
                created_at=self._now,
            )
        ]

    def _incomplete_project_skills(self, project_id: str | None) -> list[DailyReminder]:
        skills = [
            item
            for item in self._object_store.list("project_skills")
            if _matches_project(item, project_id) and not _str_list(item.get("default_reading_requirements"))
        ]
        if not skills:
            return []
        return [
            DailyReminder(
                reminder_id="reminder-project-skills-missing-reading-requirements",
                reminder_type="project_skill_reading_requirements_missing",
                severity="low",
                title="有项目 skill 缺少默认阅读要求",
                summary=f"{len(skills)} 个 Project Skill 缺少 default_reading_requirements，跨 Agent 使用时容易漏读上下文。",
                action="complete_project_skill_reading_requirements",
                project_id=project_id,
                related_ids=tuple(_record_id(item) for item in skills[:10]),
                source_refs=tuple(_refs_from_records(skills)),
                due_label="recent",
                created_at=self._now,
            )
        ]

    def _local_provider_readiness(self, project_id: str | None) -> list[DailyReminder]:
        if not self._provider_readiness_is_relevant(project_id):
            return []
        providers = [
            ("local_ocr", "OCR", GetLocalOcrProviderSettings(self._object_store).execute()),
            ("local_asr", "ASR", GetLocalAsrProviderSettings(self._object_store).execute()),
            ("local_video", "Video", GetLocalVideoProviderSettings(self._object_store).execute()),
            (
                "local_document_text",
                "Document text",
                GetLocalDocumentTextExtractorSettings(self._object_store).execute(),
            ),
            (
                "transcript_summary",
                "Transcript summary",
                GetTranscriptSummarySettings(self._object_store).execute(),
            ),
        ]
        not_ready = [
            {
                "provider_id": provider_id,
                "label": label,
                "status": provider.status,
                "diagnostic": _provider_diagnostic(provider),
            }
            for provider_id, label, provider in providers
            if provider.status != "ready"
        ]
        if not not_ready:
            return []
        detail = ", ".join(
            f"{item['label']}:{item['diagnostic']}" for item in not_ready[:4]
        )
        return [
            DailyReminder(
                reminder_id="reminder-local-provider-readiness",
                reminder_type="provider_local_readiness_needed",
                severity="low",
                title="本地处理 Provider 还未全部 ready",
                summary=f"{len(not_ready)} 个本地 Provider 需要检查：{detail}。",
                action="configure_local_providers",
                project_id=project_id,
                related_ids=tuple(str(item["provider_id"]) for item in not_ready),
                source_refs=(),
                due_label="recent",
                created_at=self._now,
            )
        ]

    def _provider_readiness_is_relevant(self, project_id: str | None) -> bool:
        for source in self._object_store.list("sources"):
            if not _matches_project(source, project_id):
                continue
            if _optional_str(source.get("type")) in {"file", "image", "audio", "video"}:
                return True
        for collection in ("audio_auto_workflows", "video_auto_workflows"):
            if any(_matches_project(item, project_id) for item in self._object_store.list(collection)):
                return True
        return False

    def _matches_project_or_source(self, record: Mapping[str, object], project_id: str | None) -> bool:
        if _matches_project(record, project_id):
            return True
        if project_id is None:
            return True
        source_id = _optional_str(record.get("source_id"))
        if source_id is None:
            return False
        source = self._object_store.read("sources", source_id)
        return isinstance(source, Mapping) and _matches_project(source, project_id)


def serialize_daily_reminder_digest(result: DailyReminderDigest) -> dict[str, object]:
    return {
        "status": result.status,
        "digest_id": result.digest_id,
        "project_id": result.project_id,
        "generated_at": result.generated_at,
        "window": result.window,
        "reminders": [serialize_daily_reminder(item) for item in result.reminders],
        "counts": dict(result.counts),
        "next_focus": result.next_focus,
        "memory_publication_state": result.memory_publication_state,
        "blocked_operations": list(result.blocked_operations),
    }


def serialize_daily_reminder(reminder: DailyReminder) -> dict[str, object]:
    return {
        "reminder_id": reminder.reminder_id,
        "reminder_type": reminder.reminder_type,
        "severity": reminder.severity,
        "title": reminder.title,
        "summary": reminder.summary,
        "action": reminder.action,
        "project_id": reminder.project_id,
        "related_ids": list(reminder.related_ids),
        "source_refs": list(reminder.source_refs),
        "due_label": reminder.due_label,
        "created_at": reminder.created_at,
    }


def _sort_key(reminder: DailyReminder) -> tuple[int, str]:
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    return (severity_rank.get(reminder.severity, 9), reminder.reminder_id)


def _matches_project(record: Mapping[str, object], project_id: str | None) -> bool:
    if project_id is None:
        return True
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    return record.get("project_id") in (None, project_id) and metadata.get("project_id") in (None, project_id)


def _record_id(record: Mapping[str, object]) -> str:
    for key in ("id", "candidate_id", "workflow_id", "draft_id", "source_id"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _refs_from_records(records: list[Mapping[str, object]]) -> list[str]:
    refs: list[str] = []
    for record in records:
        refs.extend(_str_list(record.get("source_refs")))
        for key in ("source_id", "document_id", "ref", "id", "storage_uri"):
            value = record.get(key)
            if isinstance(value, str) and value:
                refs.append(value)
    return list(dict.fromkeys(refs))[:20]


def _source_has_failed_processing(source: Mapping[str, object]) -> bool:
    metadata = source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {}
    content_read = metadata.get("content_read") if isinstance(metadata.get("content_read"), Mapping) else {}
    media_processing = metadata.get("media_processing") if isinstance(metadata.get("media_processing"), Mapping) else {}
    return content_read.get("status") == "failed" or media_processing.get("status") == "failed"


def _unique_records(records: list[Mapping[str, object]]) -> list[Mapping[str, object]]:
    unique: dict[str, Mapping[str, object]] = {}
    for record in records:
        key = f"{_optional_str(record.get('_collection')) or 'record'}:{_record_id(record)}"
        unique.setdefault(key, record)
    return list(unique.values())


def _count_failed_extraction_types(records: list[Mapping[str, object]]) -> str:
    counts: dict[str, int] = {}
    for record in records:
        key = (
            _optional_str(record.get("output_kind"))
            or _optional_str(record.get("required_capability"))
            or _optional_str(record.get("media_type"))
            or _optional_str(record.get("type"))
            or _optional_str(record.get("_collection"))
            or "unknown"
        )
        counts[key] = counts.get(key, 0) + 1
    return ", ".join(f"{key}:{value}" for key, value in sorted(counts.items())[:6])


def _source_needs_content_read(source: Mapping[str, object]) -> bool:
    source_type = _optional_str(source.get("type"))
    if source_type == "question":
        return False
    metadata = source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {}
    content_read = metadata.get("content_read") if isinstance(metadata.get("content_read"), Mapping) else {}
    if content_read.get("status") == "completed" or content_read.get("content_read") is True:
        return False
    if content_read.get("status") == "failed":
        return False
    return True


def _count_by_type(records: list[Mapping[str, object]]) -> str:
    counts: dict[str, int] = {}
    for record in records:
        key = _optional_str(record.get("type")) or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return ", ".join(f"{key}:{value}" for key, value in sorted(counts.items())[:6])


def _provider_diagnostic(provider: object) -> str:
    diagnostic = getattr(provider, "diagnostic", None)
    if isinstance(diagnostic, str) and diagnostic:
        return diagnostic
    status = getattr(provider, "status", None)
    if isinstance(status, str) and status == "disabled":
        return "disabled_until_explicit_enable"
    if isinstance(status, str) and status:
        return status
    return "unknown"


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _clean_optional(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = " ".join(value.strip().split())
    return clean or None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
