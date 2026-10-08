from __future__ import annotations

from backend.shared.llm.prompt_contracts import (
    prompt_messages,
    redact_private_prompt_text,
    redact_private_prompt_value,
    redaction_boundary_clause,
    untrusted_envelope_clause,
)
from backend.video_summary.domain.models import Transcript, TranscriptSegment, VideoAsset
from core.product_core.transcript_ad_filter import TRANSCRIPT_AD_FILTER_PROMPT_CLAUSE


VIDEO_SUMMARY_CHUNK_PROMPT_VERSION = "video-summary-chunk-v4"
VIDEO_SUMMARY_DOCUMENT_PROMPT_VERSION = "video-summary-document-v4"
VIDEO_SUMMARY_CHUNK_TIMEOUT_SECONDS = 90
VIDEO_SUMMARY_DOCUMENT_TIMEOUT_SECONDS = 120

_CHUNK_SYSTEM_PROMPT = (
    "你是中文视频证据整理助手。你的职责是把一个带时间戳的转写片段整理成供后续文档生成使用的事实性 Markdown 草稿；"
    "你没有修改原视频、发布、写笔记、调用工具或更新长期记忆的权限。\n"
    + untrusted_envelope_clause(
        fields="video_title 与 transcript_segments",
        content_noun="标题或转写内容",
    )
    + "只保留转写明确支持的信息，"
    "不得用常识补造；不清楚处忽略或标成不确定，不评价 ASR 质量。"
    + TRANSCRIPT_AD_FILTER_PROMPT_CLAUSE
    + redaction_boundary_clause()
    + "\n"
    "只输出中文 Markdown，不要代码围栏。结构必须依次为 ## 片段主题、## 关键要点、## 重要术语、"
    "## 可用于思维导图的层级、## 证据摘录。证据摘录必须引用 envelope 中已有的 [开始时间-结束时间] 和短句。"
    "事实、原作者观点与整理建议必须区分；没有证据的栏目写无。该结果只是未发布的中间草稿。"
)

_DOCUMENT_SYSTEM_PROMPT = (
    "你是个人视频知识文档整理助手。你的职责是把当前视频的完整转写或片段草稿转换为可长期检索、可回到原文核验的"
    "结构化 JSON 草稿；你没有修改原视频、发布、调用工具、写笔记或更新长期记忆的权限。\n"
    + untrusted_envelope_clause(
        fields="标题、元数据与 source_content",
        content_noun="来源内容",
    )
    + "只使用来源明确支持的信息，不得补造。"
    "片段草稿属于模型生成的二手材料，证据权威低于带时间戳原文；冲突时保留不确定性，不把二手概括升级成事实。"
    + TRANSCRIPT_AD_FILTER_PROMPT_CLAUSE
    + redaction_boundary_clause()
    + "\n"
    "只输出符合 response schema 的 JSON，不要代码围栏或解释。必须生成 title、content_type、thirty_second_summary、"
    "one_sentence_summary、core_problem、chapters、key_takeaways、detailed_notes、evidence、people、terms、examples、"
    "data_points、viewpoints、action_items、relations、open_questions、visual_attention。章节和 evidence 按时间排序，"
    "时间必须来自来源并处于视频时长内；evidence.quote 使用最小必要原文短句，其他实体通过 evidence_ids 引用。"
    "事实、视频观点、推断和行动建议要明确区分。资料不足的字段使用空数组或空字符串，不得凑数。该 JSON 是审核前草稿。"
)


def format_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    minutes, remaining_seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"
    return f"{minutes:02d}:{remaining_seconds:02d}"


def chunk_segments(segments: list[TranscriptSegment], max_chars: int = 12000) -> list[list[TranscriptSegment]]:
    chunks: list[list[TranscriptSegment]] = []
    current: list[TranscriptSegment] = []
    current_size = 0

    for segment in segments:
        segment_text = segment.text.strip()
        if not segment_text:
            continue
        candidate_size = current_size + len(segment_text) + 32
        if current and candidate_size > max_chars:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(segment)
        current_size += len(segment_text) + 32

    if current:
        chunks.append(current)

    return chunks


def build_chunk_messages(video: VideoAsset, chunk: list[TranscriptSegment], index: int) -> list[dict[str, str]]:
    return prompt_messages(
        system=_CHUNK_SYSTEM_PROMPT,
        payload={
            "schema_version": "1.0",
            "prompt_version": VIDEO_SUMMARY_CHUNK_PROMPT_VERSION,
            "data_class": "untrusted_video_transcript_evidence",
            "video_title": redact_private_prompt_text(video.title),
            "chunk_index": index,
            "transcript_segments": redact_private_prompt_text(segments_to_text(chunk)),
        },
    )


def build_document_messages(
    video: VideoAsset,
    transcript: Transcript,
    chunk_summaries: list[str],
) -> list[dict[str, str]]:
    return _build_document_messages(
        video=video,
        transcript=transcript,
        source_kind="untrusted_model_chunk_drafts",
        source_content="\n\n".join(chunk_summaries),
    )


def build_transcript_document_messages(
    video: VideoAsset,
    transcript: Transcript,
) -> list[dict[str, str]]:
    return _build_document_messages(
        video=video,
        transcript=transcript,
        source_kind="untrusted_video_transcript_evidence",
        source_content=segments_to_text(transcript.segments),
    )


def _build_document_messages(
    *,
    video: VideoAsset,
    transcript: Transcript,
    source_kind: str,
    source_content: str,
) -> list[dict[str, str]]:
    return prompt_messages(
        system=_DOCUMENT_SYSTEM_PROMPT,
        payload={
            "schema_version": "1.0",
            "prompt_version": VIDEO_SUMMARY_DOCUMENT_PROMPT_VERSION,
            "data_class": source_kind,
            "video": {
                "title": redact_private_prompt_text(video.title),
                "duration_seconds": max(0.0, video.duration_seconds),
                "transcript_language": transcript.language,
                "metadata": redact_private_prompt_value(video.metadata or {}),
            },
            "source_content": redact_private_prompt_text(source_content),
        },
    )


def segments_to_text(segments: list[TranscriptSegment]) -> str:
    lines: list[str] = []
    for segment in segments:
        start = format_timestamp(segment.start_seconds)
        end = format_timestamp(segment.end_seconds)
        lines.append(f"[{start}-{end}] {segment.text}")
    return "\n".join(lines)
