from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


SCHEMA_VERSION = "1.0"


def local_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="microseconds")


class ReplayModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnowledgeSource(ReplayModel):
    kind: Literal["bilibili", "manual", "clipboard", "file", "report", "qa"]
    url: str = ""
    bvid: str = ""
    local_path: str = ""
    report_id: str = ""


class KnowledgeEvidence(ReplayModel):
    chunk_ids: list[str] = Field(default_factory=list)
    frame_ids: list[str] = Field(default_factory=list)
    timestamps: list[str] = Field(default_factory=list)
    quotes: list[str] = Field(default_factory=list)


class KnowledgeLinks(ReplayModel):
    related_items: list[str] = Field(default_factory=list)
    parent_report: str = ""
    child_reports: list[str] = Field(default_factory=list)
    linked_video_ids: list[str] = Field(default_factory=list)
    asset_ids: list[str] = Field(default_factory=list)


class KnowledgeStatus(ReplayModel):
    in_inbox: bool = False
    in_daily: bool = False
    archived: bool = False
    favorite: bool = False
    high_value: bool = False
    need_review: bool = False


class KnowledgeIndex(ReplayModel):
    available: bool = False
    keywords: list[str] = Field(default_factory=list)
    chunk_ids: list[str] = Field(default_factory=list)


class KnowledgeItem(ReplayModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    id: str = Field(default_factory=lambda: f"item_{uuid4().hex}")
    series_id: str = "default"
    type: Literal["video", "note", "clip", "thought", "action", "question", "report"]
    title: str
    content: str = ""
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=local_now_iso)
    updated_at: str = Field(default_factory=local_now_iso)
    source: KnowledgeSource
    evidence: KnowledgeEvidence = Field(default_factory=KnowledgeEvidence)
    links: KnowledgeLinks = Field(default_factory=KnowledgeLinks)
    status: KnowledgeStatus = Field(default_factory=KnowledgeStatus)
    index: KnowledgeIndex = Field(default_factory=KnowledgeIndex)


class QuickCaptureRequest(ReplayModel):
    series_id: str = "default"
    type: Literal["note", "clip", "thought", "action", "question"]
    title: str = ""
    content: str
    tags: list[str] = Field(default_factory=list)
    source_kind: Literal["manual", "clipboard", "file"] = "manual"
    source_url: str = ""
    linked_video_ids: list[str] = Field(default_factory=list)
    linked_chunk_ids: list[str] = Field(default_factory=list)
    linked_frame_ids: list[str] = Field(default_factory=list)
    timestamps: list[str] = Field(default_factory=list)
    quotes: list[str] = Field(default_factory=list)
    status: Literal["inbox", "organized", "archived"] = "inbox"
    include_in_daily: bool = True


class VideoKnowledgeRequest(ReplayModel):
    series_id: str = "default"
    include_in_daily: bool = True
    high_value: bool = False
    need_review: bool = False


class MemoryQuestionRequest(ReplayModel):
    series_id: str = "default"
    question: str = Field(min_length=1)
    scope: Literal["auto", "video", "day", "week", "month", "year", "all"] = "auto"
    scope_value: str = ""
    limit: int = Field(default=8, ge=1, le=20)
    persist: bool = True
    cross_series: bool = False


class MemoryReference(ReplayModel):
    series_id: str = "default"
    source_type: str
    title: str = ""
    report_id: str = ""
    item_id: str = ""
    asset_id: str = ""
    video_id: str = ""
    bvid: str = ""
    chunk_id: str = ""
    timestamp: str = ""
    frame_id: str = ""
    quote: str = ""


class MemoryAnswer(ReplayModel):
    answer: str
    scope: Literal["video", "day", "week", "month", "year", "all"]
    scope_value: str = ""
    matched_count: int = Field(default=0, ge=0)
    evidence_sufficient: bool = False
    references: list[MemoryReference] = Field(default_factory=list)
    saved_item_id: str = ""


class DateRange(ReplayModel):
    start: str
    end: str


class ReportSources(ReplayModel):
    videos: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    clips: list[str] = Field(default_factory=list)
    thoughts: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    previous_reports: list[str] = Field(default_factory=list)


class ReportSummary(ReplayModel):
    one_sentence: str = ""
    main_topics: list[str] = Field(default_factory=list)
    key_takeaways: list[str] = Field(default_factory=list)
    important_questions: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    review_suggestions: list[str] = Field(default_factory=list)


class ReportVideoKnowledge(ReplayModel):
    video_id: str
    title: str
    bvid: str = ""
    summary: str = ""
    important_timestamps: list[str] = Field(default_factory=list)
    visual_frame_ids: list[str] = Field(default_factory=list)
    linked_markdown: str = ""


class ReportPersonalKnowledge(ReplayModel):
    item_id: str
    type: str
    title: str
    summary: str = ""
    tags: list[str] = Field(default_factory=list)


class ReportReview(ReplayModel):
    what_i_learned: list[str] = Field(default_factory=list)
    what_i_should_revisit: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    long_term_patterns: list[str] = Field(default_factory=list)


class ReportEvidence(ReplayModel):
    source_type: Literal["video", "note", "clip", "thought", "action", "question", "report"]
    source_id: str
    video_id: str = ""
    bvid: str = ""
    timestamp: str = ""
    chunk_id: str = ""
    frame_id: str = ""
    quote: str = ""


class ReportIndex(ReplayModel):
    available: bool = True
    keywords: list[str] = Field(default_factory=list)
    linked_item_ids: list[str] = Field(default_factory=list)
    linked_report_ids: list[str] = Field(default_factory=list)


class Report(ReplayModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    report_id: str = Field(default_factory=lambda: f"report_{uuid4().hex}")
    series_id: str = "default"
    type: Literal["daily", "weekly", "monthly", "yearly"]
    title: str
    date_range: DateRange
    created_at: str = Field(default_factory=local_now_iso)
    updated_at: str = Field(default_factory=local_now_iso)
    sources: ReportSources = Field(default_factory=ReportSources)
    summary: ReportSummary = Field(default_factory=ReportSummary)
    video_knowledge: list[ReportVideoKnowledge] = Field(default_factory=list)
    personal_knowledge: list[ReportPersonalKnowledge] = Field(default_factory=list)
    review: ReportReview = Field(default_factory=ReportReview)
    evidence: list[ReportEvidence] = Field(default_factory=list)
    index: ReportIndex = Field(default_factory=ReportIndex)


class ReplayTask(ReplayModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    task_id: str = Field(default_factory=lambda: f"replay_{uuid4().hex}")
    series_id: str = "default"
    type: Literal["daily_report", "weekly_report", "monthly_report", "yearly_report"]
    status: Literal["pending", "running", "success", "failed"] = "pending"
    date_range: DateRange
    source_count: int = Field(default=0, ge=0)
    output_files: list[str] = Field(default_factory=list)
    error: str = ""
    created_at: str = Field(default_factory=local_now_iso)
    updated_at: str = Field(default_factory=local_now_iso)


class DashboardReportState(ReplayModel):
    key: Literal["today_daily", "yesterday_daily", "current_week", "current_month", "current_year"]
    type: Literal["daily", "weekly", "monthly", "yearly"]
    report_id: str
    status: Literal["missing", "ready", "outdated"]
    date_range: DateRange
    source_count: int = Field(default=0, ge=0)
    updated_at: str = ""


class ReplayDashboard(ReplayModel):
    series_id: str = "default"
    today: str
    journal_path: str
    journal_ready: bool
    today_item_count: int = Field(default=0, ge=0)
    today_counts: dict[str, int] = Field(default_factory=dict)
    report_states: list[DashboardReportState] = Field(default_factory=list)
    missing_or_outdated: list[str] = Field(default_factory=list)
    recent_items: list[KnowledgeItem] = Field(default_factory=list)
    active_tasks: list[ReplayTask] = Field(default_factory=list)
    failed_tasks: list[ReplayTask] = Field(default_factory=list)


class SeriesPreferences(ReplayModel):
    smart_model: str = ""
    memory_model: str = ""
    allow_cross_series_search: bool = False
    require_intake_review: bool = True
    asset_policy: Literal["copy", "copy_and_extract"] = "copy_and_extract"
    video_default_ingest: bool = True


class Series(ReplayModel):
    series_id: str
    name: str
    description: str = ""
    color: str = "#a90000"
    status: Literal["active", "archived"] = "active"
    is_default: bool = False
    relative_path: str
    created_at: str = Field(default_factory=local_now_iso)
    updated_at: str = Field(default_factory=local_now_iso)
    preferences: SeriesPreferences = Field(default_factory=SeriesPreferences)


class CreateSeriesRequest(ReplayModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    color: str = "#a90000"


class UpdateSeriesRequest(ReplayModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    color: str | None = None
    preferences: SeriesPreferences | None = None


IntakeType = Literal[
    "quick_note",
    "asset",
    "image",
    "file",
    "video_summary",
    "report_candidate",
    "memory_capture",
    "manual",
    "clip",
    "thought",
    "action",
    "question",
]
IntakeStatus = Literal["pending", "reviewing", "approved", "rejected", "archived", "merged", "failed"]


class IntakeItem(ReplayModel):
    intake_id: str = Field(default_factory=lambda: f"intake_{uuid4().hex}")
    series_id: str
    type: IntakeType = "manual"
    title: str = ""
    raw_text: str = ""
    asset_text: str = ""
    structured_text: str = ""
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    source: str = ""
    asset_ids: list[str] = Field(default_factory=list)
    suggested_report_type: Literal["daily", "weekly", "monthly", "yearly", "none"] = "none"
    suggested_actions: list[str] = Field(default_factory=list)
    status: IntakeStatus = "pending"
    created_at: str = Field(default_factory=local_now_iso)
    updated_at: str = Field(default_factory=local_now_iso)
    created_by: Literal["user", "ai", "system"] = "user"
    reviewed_at: str = ""
    knowledge_item_id: str = ""
    report_id: str = ""
    warnings: list[str] = Field(default_factory=list)
    # The revision is derived from business state, not timestamps or itself.  It
    # is intentionally additive so pre-revision JSON remains readable.
    revision: str = ""

    @model_validator(mode="after")
    def _derive_revision(self) -> "IntakeItem":
        self.revision = self.content_revision()
        return self

    def content_revision(self) -> str:
        payload = self.model_dump(mode="json", exclude={"revision", "created_at", "updated_at"})
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CreateIntakeRequest(ReplayModel):
    type: IntakeType = "manual"
    title: str = ""
    raw_text: str = ""
    structured_text: str = ""
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    source: str = ""
    asset_ids: list[str] = Field(default_factory=list)
    suggested_report_type: Literal["daily", "weekly", "monthly", "yearly", "none"] = "none"
    suggested_actions: list[str] = Field(default_factory=list)
    created_by: Literal["user", "ai", "system"] = "user"


class UpdateIntakeRequest(ReplayModel):
    title: str | None = None
    raw_text: str | None = None
    structured_text: str | None = None
    summary: str | None = None
    tags: list[str] | None = None
    links: list[str] | None = None
    asset_ids: list[str] | None = None
    suggested_report_type: Literal["daily", "weekly", "monthly", "yearly", "none"] | None = None
    suggested_actions: list[str] | None = None
    status: Literal["pending", "reviewing", "archived"] | None = None


class IntakeBatchRequest(ReplayModel):
    intake_ids: list[str] = Field(min_length=1, max_length=200)


class IntakeMergeRequest(ReplayModel):
    report_id: str
    dry_run: bool = True
    base_markdown: str | None = None
    base_revision: str = ""


class ReportMarkdownDocument(ReplayModel):
    series_id: str
    report_id: str
    markdown: str
    revision: str
    updated_at: str = ""


class SaveReportMarkdownRequest(ReplayModel):
    markdown: str
    base_revision: str = ""


class ReportMergeRequest(ReplayModel):
    base_markdown: str
    incoming_items: list[str | dict[str, Any]] = Field(default_factory=list)
    merge_mode: Literal["preserve_manual_edits"] = "preserve_manual_edits"
    dry_run: bool = True
    base_revision: str = ""


class ReportMergeResponse(ReplayModel):
    series_id: str
    report_id: str
    merged_markdown: str
    changed: bool
    warnings: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    revision: str = ""


class AssetMetadata(ReplayModel):
    asset_id: str
    series_id: str
    filename: str
    media_type: str = "application/octet-stream"
    size: int = Field(ge=0)
    sha256: str
    relative_path: str
    extracted_text_path: str = ""
    extraction_status: str = "unknown"  # "extracted" | "unsupported" | "unknown"
    created_at: str = Field(default_factory=local_now_iso)
    knowledge_item_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    manual_summary: str = ""  # R79: 用户手动补的附件摘要，仅 unsupported 附件有值


class UpdateAssetMetadataRequest(ReplayModel):
    """R79: 更新附件元数据（当前仅支持 manual_summary）。"""
    manual_summary: str | None = None  # None 表示不修改


class MemoryMessage(ReplayModel):
    message_id: str = Field(default_factory=lambda: f"message_{uuid4().hex}")
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    sources: list[MemoryReference] = Field(default_factory=list)
    timestamp: str = Field(default_factory=local_now_iso)


class MemorySession(ReplayModel):
    series_id: str
    messages: list[MemoryMessage] = Field(default_factory=list)
    updated_at: str = Field(default_factory=local_now_iso)


class SeriesStats(ReplayModel):
    series_id: str
    range: str = "all"
    counts: dict[str, int] = Field(default_factory=dict)
    tokens: dict[str, int] = Field(default_factory=dict)
    heatmap: dict[str, int] = Field(default_factory=dict)
    last_recorded_at: str = ""
    streak_days: int = 0


class VideoContract(ReplayModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    video_id: str
    series_id: str
    bvid: str
    page: int = Field(default=1, ge=1)
    title: str
    uploader: str = ""
    published_at: str = ""
    source_url: str
    local_path: str = ""
    tags: list[str] = Field(default_factory=list)
    status: Literal["preparing", "processing", "completed", "failed"]
    media_mode: Literal["audio", "video"] = "audio"
    transcript_source: Literal["official_subtitle", "whisper", "unknown"] = "unknown"
    structured_available: bool = False
    visual_mode: Literal["disabled", "mock", "real"] = "disabled"
    in_daily: bool = False
    exported_markdown: bool = False


class VisionFrame(ReplayModel):
    frame_id: str
    timestamp: str
    local_path: str = ""


class VisionClaim(ReplayModel):
    frame_id: str
    timestamp: str
    claim: str
    source_mode: Literal["mock", "real"]


class VisionContract(ReplayModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    mode: Literal["disabled", "mock", "real"]
    provider: str = ""
    model: str = ""
    is_mock: bool = False
    local_detection_used: bool = False
    frames: list[VisionFrame] = Field(default_factory=list)
    claims: list[VisionClaim] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


CORE_SCHEMA_MODELS: dict[str, type[ReplayModel]] = {
    "asset.schema.json": AssetMetadata,
    "intake_item.schema.json": IntakeItem,
    "knowledge_item.schema.json": KnowledgeItem,
    "memory_session.schema.json": MemorySession,
    "report.schema.json": Report,
    "report_markdown.schema.json": ReportMarkdownDocument,
    "series.schema.json": Series,
    "task.schema.json": ReplayTask,
    "video.schema.json": VideoContract,
    "vision.schema.json": VisionContract,
}


def exportable_schema(file_name: str) -> dict[str, object]:
    model = CORE_SCHEMA_MODELS[file_name]
    schema = model.model_json_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://chriptmas-replay.local/contracts/{file_name}",
        "x-contract-version": SCHEMA_VERSION,
        **schema,
    }
