from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


WorkbenchInputModality = Literal["link", "file", "image", "audio", "video"]
WorkbenchInputPlanStatus = Literal["planned"]
WorkbenchInputCandidateStatus = Literal["selected", "deferred"]


@dataclass(frozen=True, slots=True)
class WorkbenchInputTypeCandidate:
    modality: WorkbenchInputModality
    status: WorkbenchInputCandidateStatus
    priority: int
    product_value: str
    implementation_risk: str
    next_smoke_package: str
    required_guards: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkbenchInputTypePlan:
    status: WorkbenchInputPlanStatus
    selected_modality: WorkbenchInputModality
    selected_package: str
    selection_reason: str
    candidates: tuple[WorkbenchInputTypeCandidate, ...]
    held_boundaries: tuple[str, ...]
    next_step: str


class PlanWorkbenchNextInputType:
    """Select the next non-text workbench intake modality after text Source trace is stable."""

    def execute(
        self,
        *,
        text_source_selection_ready: bool,
        library_bridge_ready: bool,
    ) -> WorkbenchInputTypePlan:
        if not text_source_selection_ready:
            raise ValueError("broader input planning requires text Source/Job selection readiness")
        if not library_bridge_ready:
            raise ValueError("broader input planning requires the library bridge readiness")

        candidates = _candidate_plan()
        selected = _selected_candidate(candidates)

        return WorkbenchInputTypePlan(
            status="planned",
            selected_modality=selected.modality,
            selected_package=selected.next_smoke_package,
            selection_reason=(
                "link is the next non-text intake because it proves external-reference Source capture "
                "without adding binary storage, OS file picker, OCR, transcription or remote fetch risk"
            ),
            candidates=candidates,
            held_boundaries=(
                "source_job_trace_required",
                "library_bridge_selection_required",
                "no_remote_fetch_in_next_smoke",
                "no_memory_publication",
                "no_full_shell_expansion",
                "no_old_intelligence_app_expansion",
            ),
            next_step="workbench_link_source_intake_vertical_smoke",
        )


def serialize_workbench_input_type_plan(plan: WorkbenchInputTypePlan) -> dict[str, object]:
    return {
        "status": plan.status,
        "selected_modality": plan.selected_modality,
        "selected_package": plan.selected_package,
        "selection_reason": plan.selection_reason,
        "candidates": [
            {
                "modality": candidate.modality,
                "status": candidate.status,
                "priority": candidate.priority,
                "product_value": candidate.product_value,
                "implementation_risk": candidate.implementation_risk,
                "next_smoke_package": candidate.next_smoke_package,
                "required_guards": list(candidate.required_guards),
            }
            for candidate in plan.candidates
        ],
        "held_boundaries": list(plan.held_boundaries),
        "next_step": plan.next_step,
    }


def _candidate_plan() -> tuple[WorkbenchInputTypeCandidate, ...]:
    return (
        WorkbenchInputTypeCandidate(
            modality="link",
            status="selected",
            priority=1,
            product_value=(
                "captures a user-supplied URL as a traceable Source and Job before richer extraction"
            ),
            implementation_risk="low if the smoke records URL metadata only and avoids remote fetch",
            next_smoke_package="workbench_link_source_intake_vertical_smoke",
            required_guards=(
                "persist_url_as_source_metadata",
                "no_remote_fetch",
                "capture_job_trace_required",
                "library_selection_required",
            ),
        ),
        WorkbenchInputTypeCandidate(
            modality="file",
            status="deferred",
            priority=2,
            product_value="validates local file handoff into Source intake",
            implementation_risk="requires file picker boundary, path safety and asset handoff rules",
            next_smoke_package="workbench_file_source_intake_vertical_smoke",
            required_guards=("path_safety", "asset_handoff_policy", "source_job_trace_required"),
        ),
        WorkbenchInputTypeCandidate(
            modality="image",
            status="deferred",
            priority=3,
            product_value="validates visual Source intake for later OCR or annotation flows",
            implementation_risk="requires binary asset persistence and extractor policy",
            next_smoke_package="workbench_image_source_intake_vertical_smoke",
            required_guards=("binary_asset_policy", "extractor_disabled_until_audit"),
        ),
        WorkbenchInputTypeCandidate(
            modality="audio",
            status="deferred",
            priority=4,
            product_value="validates spoken material as a future Source input",
            implementation_risk="requires media storage and transcription boundary decisions",
            next_smoke_package="workbench_audio_source_intake_vertical_smoke",
            required_guards=("media_storage_policy", "transcription_disabled_until_audit"),
        ),
        WorkbenchInputTypeCandidate(
            modality="video",
            status="deferred",
            priority=5,
            product_value="validates richer media intake after simpler media paths are stable",
            implementation_risk="requires large-file, frame/audio extraction and progress policy",
            next_smoke_package="workbench_video_source_intake_vertical_smoke",
            required_guards=("large_media_policy", "extractor_disabled_until_audit"),
        ),
    )


def _selected_candidate(
    candidates: tuple[WorkbenchInputTypeCandidate, ...],
) -> WorkbenchInputTypeCandidate:
    selected = [candidate for candidate in candidates if candidate.status == "selected"]
    if len(selected) != 1:
        raise ValueError("workbench input type plan requires exactly one selected candidate")
    return selected[0]
