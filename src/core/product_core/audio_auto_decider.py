from __future__ import annotations

from collections.abc import Mapping

from .audio_auto_workflow import AudioAutoWorkflowResult, AudioAutoWorkflowStep


def decide_audio_auto_workflow(
    *,
    source_id: str,
    project_id: str | None,
    audio_asset_id: str | None,
    readiness: Mapping[str, object],
    transcript: object | None,
    failure: str | None = None,
) -> AudioAutoWorkflowResult:
    """Pure decision over immutable readiness and Handler outcome facts."""

    clean_source_id = _required_text(source_id, "source_id")
    clean_project_id = _optional_text(project_id) or "default"
    clean_asset_id = _optional_text(audio_asset_id)
    transcript_status = _optional_text(getattr(transcript, "status", None))
    transcript_output_id = _optional_text(getattr(transcript, "output_id", None))
    error = failure or _optional_text(getattr(transcript, "error", None))
    if transcript is None:
        step = AudioAutoWorkflowStep(
            name="transcribe_audio",
            status="blocked",
            reason=error or _optional_text(readiness.get("readiness_reason")),
            audio_asset_id=clean_asset_id,
        )
    else:
        step = AudioAutoWorkflowStep(
            name="transcribe_audio",
            status=transcript_status or "blocked",
            reason=error,
            job_id=_optional_text(getattr(transcript, "job_id", None)),
            output_id=transcript_output_id,
            audio_asset_id=_optional_text(getattr(transcript, "audio_asset_id", None)) or clean_asset_id,
        )
    completed = transcript_status == "completed" and transcript_output_id is not None
    return AudioAutoWorkflowResult(
        status="completed" if completed else "blocked",
        workflow_id=f"audio-auto-workflow-{clean_source_id}",
        source_id=clean_source_id,
        project_id=clean_project_id,
        steps=(step,),
        audio_asset_id=clean_asset_id,
        transcript_output_id=transcript_output_id if completed else None,
        transcriber_status=_required_result_text(readiness, "transcriber_status"),
        transcriber_model_profile=_required_result_text(readiness, "transcriber_model_profile"),
        transcriber_model_name=_required_result_text(readiness, "transcriber_model_name"),
        audio_asset_status=_required_result_text(readiness, "audio_asset_status"),
        readiness_reason=None if completed else _optional_text(readiness.get("readiness_reason")),
        next_step="transcript_ready" if completed else _required_result_text(readiness, "next_step"),
        memory_publication="not_started",
        blocked_operations=() if completed else ("audio_asset_transcription",),
        error=None if completed else error or _optional_text(readiness.get("readiness_reason")),
    )


def _required_text(value: object, field: str) -> str:
    clean = _optional_text(value)
    if clean is None:
        raise ValueError(f"{field} is required")
    return clean


def _required_result_text(value: Mapping[str, object], field: str) -> str:
    return _required_text(value.get(field), field)


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()
