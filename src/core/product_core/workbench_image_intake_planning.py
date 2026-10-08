from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


WorkbenchImageIntakePlanStatus = Literal["planned"]
WorkbenchImageIntakeSourceType = Literal["image"]
WorkbenchImageIntakeCaptureMode = Literal["reference"]
WorkbenchImageIntakeGuardStatus = Literal["required"]


@dataclass(frozen=True, slots=True)
class WorkbenchImageIntakeGuard:
    name: str
    status: WorkbenchImageIntakeGuardStatus
    reason: str
    next_smoke_check: str


@dataclass(frozen=True, slots=True)
class WorkbenchImageIntakePlan:
    status: WorkbenchImageIntakePlanStatus
    selected_package: str
    source_type: WorkbenchImageIntakeSourceType
    capture_mode: WorkbenchImageIntakeCaptureMode
    binary_content_policy: str
    asset_handoff_policy: str
    image_preview_policy: str
    ocr_policy: str
    extractor_policy: str
    job_policy: str
    library_selection_policy: str
    required_guards: tuple[WorkbenchImageIntakeGuard, ...]
    blocked_capabilities: tuple[str, ...]
    next_step: str


class PlanWorkbenchImageSourceIntake:
    """Plan the first image workbench intake slice before implementing image capture."""

    def execute(
        self,
        *,
        file_trace_ready: bool,
        binary_asset_boundary_ready: bool,
        platform_image_boundary_ready: bool,
    ) -> WorkbenchImageIntakePlan:
        if not file_trace_ready:
            raise ValueError("image Source intake planning requires file trace readiness")
        if not binary_asset_boundary_ready:
            raise ValueError("image Source intake planning requires binary Asset boundary readiness")
        if not platform_image_boundary_ready:
            raise ValueError("image Source intake planning requires platform image boundary readiness")

        return WorkbenchImageIntakePlan(
            status="planned",
            selected_package="workbench_image_source_intake_vertical_smoke",
            source_type="image",
            capture_mode="reference",
            binary_content_policy="metadata_only_no_binary_read",
            asset_handoff_policy="image_asset_reference_required_before_ocr",
            image_preview_policy="preview_metadata_only_no_thumbnail_generation",
            ocr_policy="ocr_disabled_until_explicit_image_extractor_slice",
            extractor_policy="extractor_disabled_until_asset_verification",
            job_policy="capture_job_publishes_source_and_image_asset_reference_only",
            library_selection_policy="source_job_image_asset_reference_only",
            required_guards=_required_guards(),
            blocked_capabilities=(
                "read_image_bytes",
                "generate_thumbnail",
                "run_ocr",
                "extract_visual_features",
                "copy_user_image_to_library",
                "persist_os_absolute_path",
                "publish_memory",
                "expand_full_workbench_shell",
            ),
            next_step="workbench_image_source_intake_vertical_smoke",
        )


def serialize_workbench_image_intake_plan(plan: WorkbenchImageIntakePlan) -> dict[str, object]:
    return {
        "status": plan.status,
        "selected_package": plan.selected_package,
        "source_type": plan.source_type,
        "capture_mode": plan.capture_mode,
        "binary_content_policy": plan.binary_content_policy,
        "asset_handoff_policy": plan.asset_handoff_policy,
        "image_preview_policy": plan.image_preview_policy,
        "ocr_policy": plan.ocr_policy,
        "extractor_policy": plan.extractor_policy,
        "job_policy": plan.job_policy,
        "library_selection_policy": plan.library_selection_policy,
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
        "next_step": plan.next_step,
    }


def _required_guards() -> tuple[WorkbenchImageIntakeGuard, ...]:
    return (
        WorkbenchImageIntakeGuard(
            name="file_trace_baseline_ready",
            status="required",
            reason="image intake must reuse the Source, Asset, Job and library trace shape proven by file intake",
            next_smoke_check="image result exposes source_id, asset reference, job_id, trace refs and library bridge item",
        ),
        WorkbenchImageIntakeGuard(
            name="binary_asset_reference_policy",
            status="required",
            reason="image Source must hand off a reference Asset before any binary processing can run",
            next_smoke_check="result includes an image Asset reference state tied to the Source",
        ),
        WorkbenchImageIntakeGuard(
            name="platform_image_boundary_required",
            status="required",
            reason="Product Core receives platform-neutral image metadata rather than bytes or an OS image handle",
            next_smoke_check="image name, media type, size and optional dimensions are explicit submission fields",
        ),
        WorkbenchImageIntakeGuard(
            name="metadata_only_capture",
            status="required",
            reason="the first image smoke proves traceability without reading or copying user image bytes",
            next_smoke_check="content snapshot, thumbnail and extracted visual data are absent",
        ),
        WorkbenchImageIntakeGuard(
            name="image_asset_handoff_required",
            status="required",
            reason="library selection must point to Source, Job and image Asset reference evidence",
            next_smoke_check="library bridge item exposes image Source and Asset reference refs only",
        ),
        WorkbenchImageIntakeGuard(
            name="ocr_disabled",
            status="required",
            reason="OCR belongs to a later explicit extractor slice after Asset verification",
            next_smoke_check="ocr_state is disabled and no extracted text is published",
        ),
        WorkbenchImageIntakeGuard(
            name="extractor_disabled",
            status="required",
            reason="visual feature extraction is outside the first image intake smoke",
            next_smoke_check="extractor_version is unset and no visual features are published",
        ),
        WorkbenchImageIntakeGuard(
            name="no_memory_publication",
            status="required",
            reason="Workbench intake still stops at Source, Asset reference and Job trace evidence",
            next_smoke_check="published output kinds exclude memory and atom contracts",
        ),
        WorkbenchImageIntakeGuard(
            name="no_full_shell_expansion",
            status="required",
            reason="the next slice should stay a narrow vertical smoke instead of rebuilding the old shell",
            next_smoke_check="UI changes remain limited to image Source intake and trace display",
        ),
    )
