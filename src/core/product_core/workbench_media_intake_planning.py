from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


WorkbenchMediaIntakePlanStatus = Literal["planned"]
WorkbenchMediaIntakeModality = Literal["audio", "video"]
WorkbenchMediaIntakeCaptureMode = Literal["reference"]
WorkbenchMediaIntakeGuardStatus = Literal["required"]
WorkbenchMediaIntakeCandidateStatus = Literal["selected", "deferred"]


@dataclass(frozen=True, slots=True)
class WorkbenchMediaIntakeCandidate:
    modality: WorkbenchMediaIntakeModality
    status: WorkbenchMediaIntakeCandidateStatus
    priority: int
    selected_package: str
    product_value: str
    implementation_risk: str
    required_guards: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkbenchMediaIntakeGuard:
    name: str
    status: WorkbenchMediaIntakeGuardStatus
    reason: str
    next_smoke_check: str


@dataclass(frozen=True, slots=True)
class WorkbenchMediaIntakePlan:
    status: WorkbenchMediaIntakePlanStatus
    selected_modality: WorkbenchMediaIntakeModality
    selected_package: str
    selection_reason: str
    capture_mode: WorkbenchMediaIntakeCaptureMode
    media_content_policy: str
    path_policy: str
    asset_handoff_policy: str
    transcription_policy: str
    waveform_policy: str
    frame_extraction_policy: str
    remote_processing_policy: str
    job_policy: str
    library_selection_policy: str
    candidates: tuple[WorkbenchMediaIntakeCandidate, ...]
    required_guards: tuple[WorkbenchMediaIntakeGuard, ...]
    blocked_capabilities: tuple[str, ...]
    deferred_capabilities: tuple[str, ...]
    next_step: str


class PlanWorkbenchAudioVideoSourceIntake:
    """Plan the first media workbench intake slice before audio or video capture."""

    def execute(
        self,
        *,
        image_trace_ready: bool,
        media_asset_boundary_ready: bool,
        platform_media_boundary_ready: bool,
    ) -> WorkbenchMediaIntakePlan:
        if not image_trace_ready:
            raise ValueError("audio/video Source intake planning requires image trace readiness")
        if not media_asset_boundary_ready:
            raise ValueError("audio/video Source intake planning requires media Asset boundary readiness")
        if not platform_media_boundary_ready:
            raise ValueError("audio/video Source intake planning requires platform media boundary readiness")

        candidates = _candidate_plan()
        selected = _selected_candidate(candidates)

        return WorkbenchMediaIntakePlan(
            status="planned",
            selected_modality=selected.modality,
            selected_package=selected.selected_package,
            selection_reason=(
                "audio is the first media intake smoke because it proves media Source, Asset reference "
                "and capture Job traceability while avoiding video frame extraction and large multi-track processing"
            ),
            capture_mode="reference",
            media_content_policy="metadata_only_no_media_read",
            path_policy="no_os_absolute_path_in_product_core",
            asset_handoff_policy="media_asset_reference_required_before_transcription_or_extraction",
            transcription_policy="transcription_disabled_until_explicit_audio_slice",
            waveform_policy="waveform_generation_disabled_until_media_processing_slice",
            frame_extraction_policy="frame_extraction_disabled_until_video_source_slice",
            remote_processing_policy="remote_media_processing_not_performed",
            job_policy="capture_job_publishes_source_and_media_asset_reference_only",
            library_selection_policy="source_job_media_asset_reference_only",
            candidates=candidates,
            required_guards=_required_guards(),
            blocked_capabilities=(
                "read_audio_bytes",
                "read_video_bytes",
                "copy_user_media_to_library",
                "persist_os_absolute_path",
                "run_transcription",
                "generate_waveform",
                "extract_video_frames",
                "extract_audio_track_from_video",
                "run_remote_media_processing",
                "publish_memory",
                "expand_full_workbench_shell",
            ),
            deferred_capabilities=(
                "video_source_intake_vertical_smoke",
                "audio_transcription_slice",
                "waveform_preview_slice",
                "video_frame_extraction_slice",
                "media_memory_candidate_slice",
            ),
            next_step="workbench_audio_source_intake_vertical_smoke",
        )


def serialize_workbench_media_intake_plan(plan: WorkbenchMediaIntakePlan) -> dict[str, object]:
    return {
        "status": plan.status,
        "selected_modality": plan.selected_modality,
        "selected_package": plan.selected_package,
        "selection_reason": plan.selection_reason,
        "capture_mode": plan.capture_mode,
        "media_content_policy": plan.media_content_policy,
        "path_policy": plan.path_policy,
        "asset_handoff_policy": plan.asset_handoff_policy,
        "transcription_policy": plan.transcription_policy,
        "waveform_policy": plan.waveform_policy,
        "frame_extraction_policy": plan.frame_extraction_policy,
        "remote_processing_policy": plan.remote_processing_policy,
        "job_policy": plan.job_policy,
        "library_selection_policy": plan.library_selection_policy,
        "candidates": [
            {
                "modality": candidate.modality,
                "status": candidate.status,
                "priority": candidate.priority,
                "selected_package": candidate.selected_package,
                "product_value": candidate.product_value,
                "implementation_risk": candidate.implementation_risk,
                "required_guards": list(candidate.required_guards),
            }
            for candidate in plan.candidates
        ],
        "required_guards": [
            {
                "name": guard.name,
                "status": guard.status,
                "reason": guard.reason,
                "next_smoke_check": guard.next_smoke_check,
            }
            for guard in plan.required_guards
        ],
        "blocked_capabilities": list(plan.blocked_capabilities),
        "deferred_capabilities": list(plan.deferred_capabilities),
        "next_step": plan.next_step,
    }


def _candidate_plan() -> tuple[WorkbenchMediaIntakeCandidate, ...]:
    return (
        WorkbenchMediaIntakeCandidate(
            modality="audio",
            status="selected",
            priority=1,
            selected_package="workbench_audio_source_intake_vertical_smoke",
            product_value=(
                "captures spoken or recorded material as traceable Source and Asset reference before transcription"
            ),
            implementation_risk=(
                "manageable if the first smoke records metadata only and blocks transcription, waveform and Memory"
            ),
            required_guards=(
                "metadata_only_capture",
                "media_asset_handoff_required",
                "transcription_disabled",
                "no_waveform_generation",
                "no_memory_publication",
            ),
        ),
        WorkbenchMediaIntakeCandidate(
            modality="video",
            status="deferred",
            priority=2,
            selected_package="workbench_video_source_intake_vertical_smoke",
            product_value="captures richer screen or camera material after the audio media path is stable",
            implementation_risk=(
                "higher because video adds frame extraction, audio track extraction, large-file and progress boundaries"
            ),
            required_guards=(
                "metadata_only_capture",
                "media_asset_handoff_required",
                "frame_extraction_disabled",
                "audio_track_extraction_disabled",
                "no_memory_publication",
            ),
        ),
    )


def _required_guards() -> tuple[WorkbenchMediaIntakeGuard, ...]:
    return (
        WorkbenchMediaIntakeGuard(
            name="image_trace_baseline_ready",
            status="required",
            reason="media intake must reuse the Source, Asset, Job and library trace shape hardened by image intake",
            next_smoke_check="audio result exposes source_id, media Asset reference, job_id, trace refs and library bridge item",
        ),
        WorkbenchMediaIntakeGuard(
            name="media_asset_reference_policy",
            status="required",
            reason="audio and video Sources must hand off reference Assets before any processing can run",
            next_smoke_check="audio smoke publishes Source and Asset reference only",
        ),
        WorkbenchMediaIntakeGuard(
            name="platform_media_boundary_required",
            status="required",
            reason="Product Core receives platform-neutral media metadata rather than bytes or OS handles",
            next_smoke_check="audio display name, media type, size, duration and media reference are explicit submission fields",
        ),
        WorkbenchMediaIntakeGuard(
            name="metadata_only_capture",
            status="required",
            reason="the first media smoke proves traceability without reading or copying user media bytes",
            next_smoke_check="content snapshot, transcript, waveform and frame outputs are absent",
        ),
        WorkbenchMediaIntakeGuard(
            name="transcription_disabled",
            status="required",
            reason="transcription belongs to a later explicit audio processing slice after Asset verification",
            next_smoke_check="transcription_state is disabled and no transcript is published",
        ),
        WorkbenchMediaIntakeGuard(
            name="waveform_disabled",
            status="required",
            reason="waveform generation is media processing and is outside the first audio Source smoke",
            next_smoke_check="waveform_state is not_generated and no waveform Asset is published",
        ),
        WorkbenchMediaIntakeGuard(
            name="frame_extraction_deferred",
            status="required",
            reason="video frame extraction belongs to a later video-specific slice",
            next_smoke_check="audio smoke contains no frame output and video remains deferred",
        ),
        WorkbenchMediaIntakeGuard(
            name="no_remote_processing",
            status="required",
            reason="media capture must stay local metadata-only before model or cloud processing is introduced",
            next_smoke_check="remote_processing_state is not_performed",
        ),
        WorkbenchMediaIntakeGuard(
            name="no_memory_publication",
            status="required",
            reason="Workbench intake stops at Source, Asset reference and Job trace evidence",
            next_smoke_check="published output kinds exclude memory and atom contracts",
        ),
        WorkbenchMediaIntakeGuard(
            name="no_full_shell_expansion",
            status="required",
            reason="the next slice should stay a narrow vertical smoke instead of rebuilding the full Workbench shell",
            next_smoke_check="UI changes remain limited to audio Source intake and trace display",
        ),
    )


def _selected_candidate(
    candidates: tuple[WorkbenchMediaIntakeCandidate, ...],
) -> WorkbenchMediaIntakeCandidate:
    selected = [candidate for candidate in candidates if candidate.status == "selected"]
    if len(selected) != 1:
        raise ValueError("workbench media intake plan requires exactly one selected candidate")
    return selected[0]
