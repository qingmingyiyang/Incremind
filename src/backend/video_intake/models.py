from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class ResolvedVideoItem(BaseModel):
    key: str
    bvid: str
    page: int = 1
    title: str
    duration_seconds: float = 0.0
    cover_url: str = ""
    source_url: str
    uploader: str = ""
    published_at: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    subtitle_languages: list[str] = Field(default_factory=list)


class ResolvedSource(BaseModel):
    source_type: Literal["single", "multi_page", "collection", "favorite", "playlist"] = "single"
    title: str
    source_url: str
    cover_url: str = ""
    items: list[ResolvedVideoItem] = Field(default_factory=list)
    cookie_browser: str = "edge"
    cookie_access: Literal["snapshot", "edge", "anonymous_fallback"] = "edge"
    cookie_warning: str = ""
    requires_selection: bool = False


class ResolveSourceRequest(BaseModel):
    url: str


class StartImportRequest(BaseModel):
    series_id: str = Field(min_length=1, max_length=80)
    url: str
    selected_keys: list[str] = Field(default_factory=list)
    media_mode: Literal["audio", "video"] = "audio"
    visual_analysis: bool = True
    transcript_enhancement_enabled: bool = True


class IntakeTask(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    series_id: str = "default"
    url: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"] = "queued"
    stage: str = "queued"
    progress: float = 0.0
    detail: str = "等待处理"
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    selected_count: int = 0
    completed_count: int = 0
    record_ids: list[str] = Field(default_factory=list)
    error: str = ""
    media_mode: Literal["audio", "video"] = "audio"
    visual_analysis: bool = True


class VisualAssessment(BaseModel):
    importance: Literal["low", "medium", "high", "unknown"] = "unknown"
    reason: str = ""
    signals: list[str] = Field(default_factory=list)
    keyframe_count: int = 0
    ocr_status: Literal["not_requested", "not_available", "completed"] = "not_requested"


class LibraryRecord(BaseModel):
    id: str
    series_id: str = "default"
    bvid: str
    page: int = 1
    title: str
    source_url: str
    uploader: str = ""
    published_at: str = ""
    imported_at: str = Field(default_factory=utc_now_iso)
    duration_seconds: float = 0.0
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    cover_url: str = ""
    status: Literal["preparing", "processing", "completed", "failed"] = "preparing"
    stage: str = "preparing"
    progress: float = 0.0
    error: str = ""
    media_mode: Literal["audio", "video"] = "audio"
    transcript_source: Literal["official_subtitle", "whisper", "unknown"] = "unknown"
    subtitle_language: str = ""
    visual: VisualAssessment = Field(default_factory=VisualAssessment)
    relative_dir: str = ""
    media_file: str = ""
    cover_file: str = ""
    summary_file: str = "内容整理.md"
    transcript_file: str = "完整转写.md"
    note_file: str = "个人笔记.md"
    official_subtitle_available: bool = False
    asr_available: bool = False
    cleaned_transcript_available: bool = False
    summary_available: bool = False
    visual_available: bool = False
    index_available: bool = False
    export_available: bool = False


class LibraryResponse(BaseModel):
    root_path: str
    records: list[LibraryRecord]


class RecordDetail(BaseModel):
    record: LibraryRecord
    summary: dict[str, object] | None = None
    transcript: dict[str, object] | None = None
    transcripts: dict[str, dict[str, object]] = Field(default_factory=dict)
    structured: dict[str, object] | None = None
    notes: str = ""


class UpdateNotesRequest(BaseModel):
    content: str


class AskVideoRequest(BaseModel):
    question: str


class QuestionReference(BaseModel):
    chunk_id: str
    start: float
    end: float
    timestamp: str
    evidence_text: str
    frame_ids: list[str] = Field(default_factory=list)


class AskVideoResponse(BaseModel):
    answer: str
    references: list[QuestionReference] = Field(default_factory=list)


class DeleteRecordsRequest(BaseModel):
    record_ids: list[str] = Field(default_factory=list)
    confirm: bool = False


class VisionSettingsResponse(BaseModel):
    mode: Literal["disabled", "mock", "real"]
    provider: str
    base_url: str
    model: str
    has_api_key: bool
    api_key_masked: str
    max_frames: int
    timeout_seconds: int
    real_configured: bool
    privacy_notice: str
    egress_manifest: dict[str, object]


class UpdateVisionSettingsRequest(BaseModel):
    mode: Literal["disabled", "mock", "real"]
    provider: str = "openai_compatible"
    base_url: str = ""
    model: str = ""
    api_key: str | None = None
    max_frames: int = Field(default=12, ge=8, le=20)
    timeout_seconds: int = Field(default=60, ge=10, le=180)
