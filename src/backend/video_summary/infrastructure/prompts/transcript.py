from __future__ import annotations

from backend.shared.llm.prompt_contracts import (
    prompt_messages,
    redact_private_prompt_text,
    redaction_boundary_clause,
    untrusted_envelope_clause,
)
from backend.video_summary.domain.models import TranscriptSegment, VideoAsset


VIDEO_TRANSCRIPT_ENHANCER_PROMPT_VERSION = "video-transcript-enhancer-v3"
VIDEO_TRANSCRIPT_ENHANCER_TIMEOUT_SECONDS = 90


def build_transcript_enhancement_messages(
    *,
    video: VideoAsset,
    segments: list[TranscriptSegment],
    chunk_index: int,
    total_chunks: int,
) -> list[dict[str, str]]:
    payload = {
        "schema_version": "1.0",
        "prompt_version": VIDEO_TRANSCRIPT_ENHANCER_PROMPT_VERSION,
        "data_class": "untrusted_asr_transcript_evidence",
        "video_title": redact_private_prompt_text(video.title),
        "chunk_index": chunk_index,
        "total_chunks": total_chunks,
        "segments": [
            {
                "start_seconds": segment.start_seconds,
                "end_seconds": segment.end_seconds,
                "text": redact_private_prompt_text(segment.text),
            }
            for segment in segments
        ],
    }
    return prompt_messages(
        system=(
            "你是中文视频 ASR 转写校对助手。你的唯一职责是纠正当前片段中明显的错字、断句、标点和同音误识别；"
            "你没有总结、补写、翻译、发布、调用工具、修改原媒体或更新长期记忆的权限。\n"
            + untrusted_envelope_clause(
                fields="标题与 segments",
                content_noun="转写内容",
            )
            + "\n"
            "只能依据每条原句校对，不得增加视频未说过的事实、人物、数字、"
            "因果或评价。噪声处保留可确认文字；无法确认时保留原句，不写转写质量评价。\n"
            + redaction_boundary_clause()
            + "\n"
            "只输出符合 response schema 的 JSON segments 数组，条目数与输入完全一致、顺序不变。start_seconds 与 end_seconds"
            "属于本地权威元数据，不得修改；调用方也会忽略模型返回的时间值。只允许 text 发生必要校正。失败时不得返回半段结果。"
        ),
        payload=payload,
    )
