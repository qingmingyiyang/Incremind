from __future__ import annotations

from dataclasses import dataclass

from .long_audio_chunker import ChunkProgressInfo, LongAudioChunkedWorkflowResult


@dataclass(frozen=True, slots=True)
class LongAudioChunkPlan:
    index: int
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True, slots=True)
class LongAudioChunkFact:
    plan: LongAudioChunkPlan
    transcript_output_id: str | None
    error: str | None


def plan_long_audio_chunks(
    total_duration_seconds: float, *, chunk_duration_seconds: float,
) -> tuple[LongAudioChunkPlan, ...]:
    """Pure deterministic chunk planner."""

    if total_duration_seconds <= 0 or chunk_duration_seconds <= 0:
        return ()
    plans: list[LongAudioChunkPlan] = []
    start = 0.0
    while start < total_duration_seconds:
        end = min(start + chunk_duration_seconds, total_duration_seconds)
        plans.append(LongAudioChunkPlan(len(plans), start, end))
        start = end
    return tuple(plans)


def decide_long_audio_result(
    *,
    source_id: str,
    project_id: str | None,
    audio_asset_id: str,
    total_duration_seconds: float,
    facts: tuple[LongAudioChunkFact, ...],
    merged_output_id: str | None,
    merged_preview: str | None,
) -> LongAudioChunkedWorkflowResult:
    """Pure projection decision over chunk Handler facts."""

    completed = tuple(fact for fact in facts if fact.transcript_output_id is not None)
    failed = tuple(fact for fact in facts if fact.transcript_output_id is None)
    completed_duration = sum(
        fact.plan.end_seconds - fact.plan.start_seconds for fact in completed
    )
    progress = (
        completed_duration / total_duration_seconds
        if total_duration_seconds > 0 else 0.0
    )
    status = (
        "completed" if facts and not failed
        else "partial_completed" if completed
        else "failed"
    )
    chunks = tuple(
        ChunkProgressInfo(
            chunk_index=fact.plan.index,
            status="done" if fact.transcript_output_id else "failed",
            start_seconds=fact.plan.start_seconds,
            end_seconds=fact.plan.end_seconds,
            duration_seconds=fact.plan.end_seconds - fact.plan.start_seconds,
            transcript_output_id=fact.transcript_output_id,
            error=fact.error,
            retry_count=0,
        )
        for fact in facts
    )
    return LongAudioChunkedWorkflowResult(
        status=status,
        workflow_id=f"long-audio-chunked-workflow-{source_id}",
        source_id=source_id,
        project_id=project_id or "default",
        audio_asset_id=audio_asset_id,
        is_long_audio=True,
        total_duration_seconds=total_duration_seconds,
        completed_duration_seconds=completed_duration,
        chunk_count=len(facts),
        completed_chunk_count=len(completed),
        failed_chunk_count=len(failed),
        progress=round(progress, 4),
        chunks=chunks,
        merged_transcript_output_id=merged_output_id,
        merged_transcript_preview=merged_preview,
        memory_publication="not_started",
        blocked_operations=() if not failed else ("long_audio_chunked_transcription",),
        readiness_reason=None,
        next_step="transcript_ready" if merged_output_id else "await_effect_recovery",
        error=None if not failed else "one or more chunk Effects are incomplete",
        is_partial_result=bool(failed),
    )
