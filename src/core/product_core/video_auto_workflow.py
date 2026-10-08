from __future__ import annotations

from dataclasses import dataclass


class VideoAutoWorkflowError(ValueError):
    """Invalid immutable Video workflow facts or projection contract."""


@dataclass(frozen=True, slots=True)
class VideoAutoWorkflowStep:
    name: str
    status: str
    reason: str | None = None
    job_id: str | None = None
    output_id: str | None = None
    audio_asset_id: str | None = None
    candidate_id: str | None = None


@dataclass(frozen=True, slots=True)
class VideoAutoWorkflowResult:
    status: str
    workflow_id: str
    source_id: str
    project_id: str
    steps: tuple[VideoAutoWorkflowStep, ...]
    audio_asset_id: str | None
    transcript_output_id: str | None
    summary_output_id: str | None
    memory_candidate_id: str | None
    summary_provider_status: str
    summary_provider_name: str | None
    summary_readiness_reason: str | None
    summary_next_step: str | None
    auto_publication_status: str | None
    publication_id: str | None
    published_ref: str | None
    rollback_ref: str | None
    memory_publication: str
    blocked_operations: tuple[str, ...]
    error: str | None


def serialize_video_auto_workflow_result(
    result: VideoAutoWorkflowResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "workflow_id": result.workflow_id,
        "source_id": result.source_id,
        "project_id": result.project_id,
        "steps": [serialize_video_auto_workflow_step(step) for step in result.steps],
        "audio_asset_id": result.audio_asset_id,
        "transcript_output_id": result.transcript_output_id,
        "summary_output_id": result.summary_output_id,
        "memory_candidate_id": result.memory_candidate_id,
        "summary_provider_status": result.summary_provider_status,
        "summary_provider_name": result.summary_provider_name,
        "summary_readiness_reason": result.summary_readiness_reason,
        "summary_next_step": result.summary_next_step,
        "auto_publication_status": result.auto_publication_status,
        "publication_id": result.publication_id,
        "published_ref": result.published_ref,
        "rollback_ref": result.rollback_ref,
        "memory_publication": result.memory_publication,
        "blocked_operations": list(result.blocked_operations),
        "error": result.error,
    }


def serialize_video_auto_workflow_step(
    step: VideoAutoWorkflowStep,
) -> dict[str, object]:
    return {
        "name": step.name,
        "status": step.status,
        "reason": step.reason,
        "job_id": step.job_id,
        "output_id": step.output_id,
        "audio_asset_id": step.audio_asset_id,
        "candidate_id": step.candidate_id,
    }
