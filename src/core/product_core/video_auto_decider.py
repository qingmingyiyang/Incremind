from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .video_auto_workflow import VideoAutoWorkflowResult, VideoAutoWorkflowStep


@dataclass(frozen=True, slots=True)
class VideoAutoFacts:
    source_id: str
    project_id: str | None
    summary_readiness: Mapping[str, object]
    audio: object | None = None
    transcript: object | None = None
    summary: object | None = None
    candidate: object | None = None
    failed_step: str | None = None
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class VideoAutoDecision:
    next_step: str | None
    result: VideoAutoWorkflowResult | None


def decide_video_auto_workflow(facts: VideoAutoFacts) -> VideoAutoDecision:
    """Pure decider. It performs no Handler call, retry, recovery, or write."""

    source_id = _required(facts.source_id, "source_id")
    project_id = _optional(facts.project_id) or "default"
    readiness = facts.summary_readiness
    steps: list[VideoAutoWorkflowStep] = []

    decision = _await_or_block("extract_audio", facts.audio, facts, steps)
    if decision is not None:
        return _decision_or_result(source_id, project_id, readiness, facts, steps, decision)
    audio_asset_id = _attr(facts.audio, "audio_asset_id")
    steps.append(_outcome_step("extract_audio", facts.audio))
    if _attr(facts.audio, "status") != "completed":
        return _final(source_id, project_id, readiness, steps, "video_audio_extraction", _attr(facts.audio, "error"))

    decision = _await_or_block("transcribe_audio", facts.transcript, facts, steps)
    if decision is not None:
        return _decision_or_result(source_id, project_id, readiness, facts, steps, decision, audio_asset_id=audio_asset_id)
    transcript_output_id = _attr(facts.transcript, "output_id")
    steps.append(_outcome_step("transcribe_audio", facts.transcript))
    if _attr(facts.transcript, "status") != "completed":
        return _final(source_id, project_id, readiness, steps, "audio_asset_transcription", _attr(facts.transcript, "error"), audio_asset_id=audio_asset_id)

    if readiness.get("status") != "ready":
        reason = _mapping_text(readiness, "reason")
        steps.append(VideoAutoWorkflowStep(
            name="summarize_transcript", status="blocked", reason=reason,
            output_id=transcript_output_id,
        ))
        return _final(source_id, project_id, readiness, steps, "transcript_summary_provider_readiness", reason, audio_asset_id=audio_asset_id, transcript_output_id=transcript_output_id)

    decision = _await_or_block("summarize_transcript", facts.summary, facts, steps)
    if decision is not None:
        return _decision_or_result(source_id, project_id, readiness, facts, steps, decision, audio_asset_id=audio_asset_id, transcript_output_id=transcript_output_id)
    summary_output_id = _attr(facts.summary, "output_id")
    steps.append(_outcome_step("summarize_transcript", facts.summary))
    if _attr(facts.summary, "status") != "completed":
        return _final(source_id, project_id, readiness, steps, "transcript_summary", _attr(facts.summary, "error"), audio_asset_id=audio_asset_id, transcript_output_id=transcript_output_id)

    candidate_ids = tuple(getattr(facts.summary, "candidate_ids", ()))
    if bool(getattr(facts.summary, "creates_memory_candidate", False)) and candidate_ids:
        candidate_id = str(candidate_ids[0])
        steps.append(VideoAutoWorkflowStep(
            name="create_memory_candidate", status="candidates_created",
            output_id=summary_output_id, candidate_id=candidate_id,
        ))
        return _final(source_id, project_id, readiness, steps, None, None, audio_asset_id=audio_asset_id, transcript_output_id=transcript_output_id, summary_output_id=summary_output_id, memory_candidate_id=candidate_id, auto_publication_status="manual_review_required")

    decision = _await_or_block("create_memory_candidate", facts.candidate, facts, steps)
    if decision is not None:
        return _decision_or_result(source_id, project_id, readiness, facts, steps, decision, audio_asset_id=audio_asset_id, transcript_output_id=transcript_output_id, summary_output_id=summary_output_id)
    candidate_id = _attr(facts.candidate, "candidate_id")
    candidate_status = _attr(facts.candidate, "status") or "blocked"
    steps.append(VideoAutoWorkflowStep(
        name="create_memory_candidate", status=candidate_status,
        output_id=summary_output_id, candidate_id=candidate_id,
    ))
    blocked = None if candidate_status == "candidate_created" and candidate_id else "video_summary_memory_candidate"
    return _final(source_id, project_id, readiness, steps, blocked, None, audio_asset_id=audio_asset_id, transcript_output_id=transcript_output_id, summary_output_id=summary_output_id, memory_candidate_id=candidate_id, auto_publication_status="manual_review_required" if blocked is None else None)


def _await_or_block(step: str, outcome: object | None, facts: VideoAutoFacts, steps: list[VideoAutoWorkflowStep]) -> str | None:
    if outcome is not None:
        return None
    if facts.failed_step == step:
        steps.append(VideoAutoWorkflowStep(name=step, status="blocked", reason=facts.failure))
        return "blocked"
    return "next"


def _decision_or_result(source_id, project_id, readiness, facts, steps, decision, **ids):
    if decision == "next":
        next_step = ("extract_audio", "transcribe_audio", "summarize_transcript", "create_memory_candidate")[len(steps)]
        return VideoAutoDecision(next_step, None)
    return _final(source_id, project_id, readiness, steps, facts.failed_step, facts.failure, **ids)


def _final(source_id, project_id, readiness, steps, blocked, error, *, audio_asset_id=None, transcript_output_id=None, summary_output_id=None, memory_candidate_id=None, auto_publication_status=None):
    result = VideoAutoWorkflowResult(
        status="blocked" if blocked else "completed",
        workflow_id=f"video-auto-workflow-{source_id}", source_id=source_id,
        project_id=project_id, steps=tuple(steps), audio_asset_id=audio_asset_id,
        transcript_output_id=transcript_output_id, summary_output_id=summary_output_id,
        memory_candidate_id=memory_candidate_id,
        summary_provider_status=str(readiness.get("status") or "unknown"),
        summary_provider_name=_mapping_text(readiness, "provider_name"),
        summary_readiness_reason=_mapping_text(readiness, "reason"),
        summary_next_step=_mapping_text(readiness, "next_step"),
        auto_publication_status=auto_publication_status, publication_id=None,
        published_ref=None, rollback_ref=None,
        memory_publication="candidate_created_not_published" if memory_candidate_id else "not_started",
        blocked_operations=(blocked,) if blocked else (), error=error,
    )
    return VideoAutoDecision(None, result)


def _outcome_step(name: str, outcome: object) -> VideoAutoWorkflowStep:
    return VideoAutoWorkflowStep(
        name=name, status=_attr(outcome, "status") or "blocked",
        reason=_attr(outcome, "error"), job_id=_attr(outcome, "job_id"),
        output_id=_attr(outcome, "output_id"),
        audio_asset_id=_attr(outcome, "audio_asset_id"),
        candidate_id=_attr(outcome, "candidate_id"),
    )


def _attr(value: object, name: str) -> str | None:
    return _optional(getattr(value, name, None))


def _mapping_text(value: Mapping[str, object], name: str) -> str | None:
    return _optional(value.get(name))


def _required(value: object, name: str) -> str:
    clean = _optional(value)
    if clean is None:
        raise ValueError(f"{name} is required")
    return clean


def _optional(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()
