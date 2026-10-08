from __future__ import annotations

from pydantic import BaseModel, Field


class SummaryChapterPayload(BaseModel):
    id: str
    title: str
    start_seconds: float = Field(default=0.0)
    end_seconds: float = Field(default=0.0)
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class EvidencePayload(BaseModel):
    id: str
    statement: str
    quote: str = ""
    start_seconds: float = Field(default=0.0)
    end_seconds: float = Field(default=0.0)
    confidence: str = "medium"


class StructuredKnowledgePayload(BaseModel):
    name: str
    description: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class ActionItemPayload(BaseModel):
    action: str
    rationale: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class RelationPayload(BaseModel):
    source: str
    target: str
    relation: str
    evidence_ids: list[str] = Field(default_factory=list)


class VisualAttentionPayload(BaseModel):
    importance: str = "unknown"
    reason: str = ""
    signals: list[str] = Field(default_factory=list)


class SummaryPayload(BaseModel):
    title: str
    content_type: str = ""
    thirty_second_summary: str = ""
    one_sentence_summary: str = ""
    core_problem: str = ""
    chapters: list[SummaryChapterPayload] = Field(default_factory=list)
    key_takeaways: list[str] = Field(default_factory=list)
    detailed_notes: list[str] = Field(default_factory=list)
    evidence: list[EvidencePayload] = Field(default_factory=list)
    people: list[StructuredKnowledgePayload] = Field(default_factory=list)
    terms: list[StructuredKnowledgePayload] = Field(default_factory=list)
    examples: list[StructuredKnowledgePayload] = Field(default_factory=list)
    data_points: list[StructuredKnowledgePayload] = Field(default_factory=list)
    viewpoints: list[StructuredKnowledgePayload] = Field(default_factory=list)
    action_items: list[ActionItemPayload] = Field(default_factory=list)
    relations: list[RelationPayload] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    visual_attention: VisualAttentionPayload = Field(default_factory=VisualAttentionPayload)


class MindmapNodePayload(BaseModel):
    id: str
    title: str
    summary: str = ""
    start_seconds: float = Field(default=0.0)
    end_seconds: float = Field(default=0.0)
    children: list["MindmapNodePayload"] = Field(default_factory=list)


class TranscriptSegmentPayload(BaseModel):
    start_seconds: float = Field(default=0.0)
    end_seconds: float = Field(default=0.0)
    text: str


class TranscriptEnhancementPayload(BaseModel):
    segments: list[TranscriptSegmentPayload] = Field(default_factory=list)
