from __future__ import annotations

from dataclasses import dataclass


LONG_AUDIO_THRESHOLD_SECONDS: float = 1800.0
DEFAULT_CHUNK_DURATION_SECONDS: float = 900.0

# Projection vocabulary retained for UI/history compatibility. Recovery state
# is owned by Effect and no production writer emits pending/running/retrying.
CHUNK_STATUS_PENDING = "pending"
CHUNK_STATUS_RUNNING = "running"
CHUNK_STATUS_DONE = "done"
CHUNK_STATUS_FAILED = "failed"
CHUNK_STATUS_RETRYING = "retrying"


class LongAudioChunkerError(ValueError):
    """Invalid immutable long-audio facts or projection contract."""


@dataclass(frozen=True, slots=True)
class ChunkProgressInfo:
    chunk_index: int
    status: str
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    transcript_output_id: str | None
    error: str | None
    retry_count: int


@dataclass(frozen=True, slots=True)
class LongAudioChunkedWorkflowResult:
    status: str
    workflow_id: str
    source_id: str
    project_id: str
    audio_asset_id: str
    is_long_audio: bool
    total_duration_seconds: float
    completed_duration_seconds: float
    chunk_count: int
    completed_chunk_count: int
    failed_chunk_count: int
    progress: float
    chunks: tuple[ChunkProgressInfo, ...]
    merged_transcript_output_id: str | None
    merged_transcript_preview: str | None
    memory_publication: str
    blocked_operations: tuple[str, ...]
    readiness_reason: str | None
    next_step: str
    error: str | None
    is_partial_result: bool


def serialize_chunk_progress_info(chunk: ChunkProgressInfo) -> dict[str, object]:
    return {
        "chunk_index": chunk.chunk_index,
        "status": chunk.status,
        "start_seconds": chunk.start_seconds,
        "end_seconds": chunk.end_seconds,
        "duration_seconds": chunk.duration_seconds,
        "transcript_output_id": chunk.transcript_output_id,
        "error": chunk.error,
        "retry_count": chunk.retry_count,
    }


def serialize_long_audio_chunked_workflow_result(
    result: LongAudioChunkedWorkflowResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "workflow_id": result.workflow_id,
        "source_id": result.source_id,
        "project_id": result.project_id,
        "audio_asset_id": result.audio_asset_id,
        "is_long_audio": result.is_long_audio,
        "is_chunked": True,
        "total_duration_seconds": result.total_duration_seconds,
        "completed_duration_seconds": result.completed_duration_seconds,
        "chunk_count": result.chunk_count,
        "completed_chunk_count": result.completed_chunk_count,
        "failed_chunk_count": result.failed_chunk_count,
        "progress": result.progress,
        "chunks": [serialize_chunk_progress_info(chunk) for chunk in result.chunks],
        "merged_transcript_output_id": result.merged_transcript_output_id,
        "merged_transcript_preview": result.merged_transcript_preview,
        "memory_publication": result.memory_publication,
        "blocked_operations": list(result.blocked_operations),
        "readiness_reason": result.readiness_reason,
        "next_step": result.next_step,
        "error": result.error,
        "is_partial_result": result.is_partial_result,
    }
