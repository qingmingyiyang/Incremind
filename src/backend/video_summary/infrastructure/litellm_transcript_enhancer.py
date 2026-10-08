from __future__ import annotations

from backend.shared.llm import LiteLLMCompletionGateway, WireAttemptSink
from backend.video_summary.domain.models import Transcript, TranscriptSegment, VideoAsset
from backend.video_summary.generation import TranscriptEnhancementPayload
from backend.video_summary.generation.cancellation import GenerationCancellationContext, cancellable_await
from backend.video_summary.infrastructure.prompts import (
    VIDEO_TRANSCRIPT_ENHANCER_TIMEOUT_SECONDS,
    build_transcript_enhancement_messages,
)


class LiteLLMTranscriptEnhancer:
    def __init__(self, gateway: LiteLLMCompletionGateway) -> None:
        self._gateway = gateway
        gateway_identity = getattr(gateway, "cache_identity", type(gateway).__qualname__)
        self.cache_identity = "|".join([type(self).__module__, type(self).__qualname__, str(gateway_identity)])

    async def enhance(
        self,
        video: VideoAsset,
        transcript: Transcript,
        cancellation: GenerationCancellationContext | None = None,
        wire_attempt_sink: WireAttemptSink | None = None,
    ) -> Transcript:
        chunks = _chunk_segments(transcript.segments)
        enhanced_segments = []
        for index, chunk in enumerate(chunks, start=1):
            if cancellation is not None and cancellation.cancel_requested:
                from backend.video_summary.generation.usecases.generate_summary import GenerateCancelledError
                raise GenerateCancelledError("任务已取消")
            coro = self._gateway.acomplete_structured(
                build_transcript_enhancement_messages(
                    video=video,
                    segments=chunk,
                    chunk_index=index,
                    total_chunks=len(chunks),
                ),
                response_model=TranscriptEnhancementPayload,
                timeout=VIDEO_TRANSCRIPT_ENHANCER_TIMEOUT_SECONDS,
                wire_attempt_sink=wire_attempt_sink,
            )
            if cancellation is not None:
                corrected_payload = await cancellable_await(coro, cancellation)
            else:
                corrected_payload = await coro
            enhanced_segments.extend(_parse_corrected_segments(corrected_payload, chunk))

        return Transcript(
            language=transcript.language,
            segments=enhanced_segments or transcript.segments,
        )


def _parse_corrected_segments(
    payload: TranscriptEnhancementPayload,
    fallback_segments: list[TranscriptSegment],
) -> list[TranscriptSegment]:
    segments = payload.segments
    normalized_segments = []
    for index, item in enumerate(segments):
        fallback = fallback_segments[min(index, len(fallback_segments) - 1)]
        text = item.text.strip() or fallback.text
        normalized_segments.append(
            TranscriptSegment(
                start_seconds=fallback.start_seconds,
                end_seconds=fallback.end_seconds,
                text=text,
            )
        )

    if len(normalized_segments) != len(fallback_segments):
        return fallback_segments
    return normalized_segments


def _chunk_segments(segments: list[TranscriptSegment], max_chars: int = 10000) -> list[list[TranscriptSegment]]:
    chunks: list[list[TranscriptSegment]] = []
    current_chunk: list[TranscriptSegment] = []
    current_size = 0

    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        candidate_size = current_size + len(text) + 64
        if current_chunk and candidate_size > max_chars:
            chunks.append(current_chunk)
            current_chunk = []
            current_size = 0
        current_chunk.append(segment)
        current_size += len(text) + 64

    if current_chunk:
        chunks.append(current_chunk)
    return chunks
