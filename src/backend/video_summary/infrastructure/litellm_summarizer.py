from __future__ import annotations

import asyncio

from backend.shared.llm import LiteLLMCompletionGateway, WireAttemptSink
from backend.video_summary.domain.models import SummaryDocument, Transcript, VideoAsset
from backend.video_summary.generation.ports import Summarizer
from backend.video_summary.generation.cancellation import GenerationCancellationContext, cancellable_await
from backend.video_summary.generation import (
    SummaryPayload,
    VIDEO_SUMMARY_CHUNK_TIMEOUT_SECONDS,
    VIDEO_SUMMARY_DOCUMENT_TIMEOUT_SECONDS,
    build_chunk_messages,
    build_document_messages,
    build_transcript_document_messages,
    chunk_segments,
    render_markdown,
)


class LiteLLMCompletionSummarizer(Summarizer):
    def __init__(
        self,
        gateway: LiteLLMCompletionGateway,
        *,
        context_window_tokens: int,
        reserved_output_tokens: int,
        direct_summary_threshold_ratio: float,
        summary_chunk_concurrency: int = 1,
    ) -> None:
        self._gateway = gateway
        self._context_window_tokens = context_window_tokens
        self._reserved_output_tokens = reserved_output_tokens
        self._direct_summary_threshold_ratio = direct_summary_threshold_ratio
        self._summary_chunk_concurrency = max(1, summary_chunk_concurrency)

    async def summarize(
        self,
        video: VideoAsset,
        transcript: Transcript,
        cancellation: GenerationCancellationContext | None = None,
        wire_attempt_sink: WireAttemptSink | None = None,
    ) -> SummaryDocument:
        if _should_use_direct_summary(
            video=video,
            transcript=transcript,
            context_window_tokens=self._context_window_tokens,
            reserved_output_tokens=self._reserved_output_tokens,
            direct_summary_threshold_ratio=self._direct_summary_threshold_ratio,
        ):
            coro = self._gateway.acomplete_structured(
                build_transcript_document_messages(video, transcript),
                response_model=SummaryPayload,
                timeout=VIDEO_SUMMARY_DOCUMENT_TIMEOUT_SECONDS,
                wire_attempt_sink=wire_attempt_sink,
            )
            payload = await cancellable_await(coro, cancellation) if cancellation else await coro
            _validate_summary_payload(payload, duration_seconds=video.duration_seconds)
            summary_data = payload.model_dump()
            markdown = render_markdown(summary_data)
            return SummaryDocument(markdown=markdown, summary_data=summary_data)

        chunks = list(enumerate(chunk_segments(transcript.segments), start=1))
        chunk_summaries = await self._summarize_chunks(video, chunks, cancellation, wire_attempt_sink)
        coro = self._gateway.acomplete_structured(
            build_document_messages(video, transcript, chunk_summaries),
            response_model=SummaryPayload,
            timeout=VIDEO_SUMMARY_DOCUMENT_TIMEOUT_SECONDS,
            wire_attempt_sink=wire_attempt_sink,
        )
        payload = await cancellable_await(coro, cancellation) if cancellation else await coro
        _validate_summary_payload(payload, duration_seconds=video.duration_seconds)
        summary_data = payload.model_dump()
        markdown = render_markdown(summary_data)
        return SummaryDocument(markdown=markdown, summary_data=summary_data)

    async def _summarize_chunks(
        self,
        video: VideoAsset,
        chunks: list[tuple[int, list]],
        cancellation: GenerationCancellationContext | None,
        wire_attempt_sink: WireAttemptSink | None,
    ) -> list[str]:
        semaphore = asyncio.Semaphore(self._summary_chunk_concurrency)

        async def summarize_chunk(index: int, chunk: list) -> tuple[int, str]:
            async with semaphore:
                if cancellation is not None and cancellation.cancel_requested:
                    from backend.video_summary.generation.usecases.generate_summary import GenerateCancelledError
                    raise GenerateCancelledError("任务已取消")
                coro = self._gateway.acomplete_text(
                    build_chunk_messages(video, chunk, index),
                    timeout=VIDEO_SUMMARY_CHUNK_TIMEOUT_SECONDS,
                    wire_attempt_sink=wire_attempt_sink,
                )
                summary = await cancellable_await(coro, cancellation) if cancellation else await coro
                return index, summary

        results = await asyncio.gather(
            *(summarize_chunk(index, chunk) for index, chunk in chunks)
        )
        return [summary for _, summary in sorted(results, key=lambda item: item[0])]


def _should_use_direct_summary(
    *,
    video: VideoAsset,
    transcript: Transcript,
    context_window_tokens: int,
    reserved_output_tokens: int,
    direct_summary_threshold_ratio: float,
) -> bool:
    available_tokens = max(1, context_window_tokens - reserved_output_tokens)
    direct_summary_budget = max(1, int(available_tokens * direct_summary_threshold_ratio))
    direct_messages = build_transcript_document_messages(video, transcript)
    return sum(_estimate_tokens(message["content"]) for message in direct_messages) <= direct_summary_budget


def _estimate_tokens(value: str) -> int:
    text = value.strip()
    if not text:
        return 0
    return max(1, len(text.encode("utf-8")) // 3)


def _validate_summary_payload(payload: SummaryPayload, *, duration_seconds: float) -> None:
    maximum = max(0.0, duration_seconds)

    def validate_range(start: float, end: float, label: str) -> None:
        if start < 0 or end < start or end > maximum:
            raise RuntimeError(f"视频概况中的{label}时间超出原视频范围，已拒绝保存。")

    for chapter in payload.chapters:
        validate_range(chapter.start_seconds, chapter.end_seconds, "章节")
    evidence_ids: set[str] = set()
    for evidence in payload.evidence:
        evidence_id = evidence.id.strip()
        if not evidence_id or evidence_id in evidence_ids:
            raise RuntimeError("视频概况包含空或重复证据 ID，已拒绝保存。")
        evidence_ids.add(evidence_id)
        validate_range(evidence.start_seconds, evidence.end_seconds, "证据")

    referenced_ids = {
        evidence_id
        for item in [
            *payload.chapters,
            *payload.people,
            *payload.terms,
            *payload.examples,
            *payload.data_points,
            *payload.viewpoints,
            *payload.action_items,
            *payload.relations,
        ]
        for evidence_id in item.evidence_ids
        if evidence_id
    }
    if referenced_ids - evidence_ids:
        raise RuntimeError("视频概况引用了不存在的证据 ID，已拒绝保存。")
