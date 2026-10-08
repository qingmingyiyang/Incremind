from __future__ import annotations

from dataclasses import dataclass


class AudioAutoWorkflowError(ValueError):
    """Invalid immutable Audio workflow facts or projection contract."""


@dataclass(frozen=True, slots=True)
class AudioAutoWorkflowStep:
    name: str
    status: str
    reason: str | None = None
    job_id: str | None = None
    output_id: str | None = None
    audio_asset_id: str | None = None


@dataclass(frozen=True, slots=True)
class AudioAutoWorkflowResult:
    status: str
    workflow_id: str
    source_id: str
    project_id: str
    steps: tuple[AudioAutoWorkflowStep, ...]
    audio_asset_id: str | None
    transcript_output_id: str | None
    transcriber_status: str
    transcriber_model_profile: str
    transcriber_model_name: str
    audio_asset_status: str
    readiness_reason: str | None
    next_step: str
    memory_publication: str
    blocked_operations: tuple[str, ...]
    error: str | None


def serialize_audio_auto_workflow_result(
    result: AudioAutoWorkflowResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "workflow_id": result.workflow_id,
        "source_id": result.source_id,
        "project_id": result.project_id,
        "steps": [serialize_audio_auto_workflow_step(step) for step in result.steps],
        "audio_asset_id": result.audio_asset_id,
        "transcript_output_id": result.transcript_output_id,
        "transcriber_status": result.transcriber_status,
        "transcriber_model_profile": result.transcriber_model_profile,
        "transcriber_model_name": result.transcriber_model_name,
        "audio_asset_status": result.audio_asset_status,
        "readiness_reason": result.readiness_reason,
        "next_step": result.next_step,
        "memory_publication": result.memory_publication,
        "blocked_operations": list(result.blocked_operations),
        "error": result.error,
    }


def serialize_audio_auto_workflow_step(
    step: AudioAutoWorkflowStep,
) -> dict[str, object]:
    return {
        "name": step.name,
        "status": step.status,
        "reason": step.reason,
        "job_id": step.job_id,
        "output_id": step.output_id,
        "audio_asset_id": step.audio_asset_id,
    }
