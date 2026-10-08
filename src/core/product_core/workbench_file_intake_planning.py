from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


WorkbenchFileIntakePlanStatus = Literal["planned"]
WorkbenchFileIntakeSourceType = Literal["file"]
WorkbenchFileIntakeCaptureMode = Literal["reference"]
WorkbenchFileIntakeGuardStatus = Literal["required"]


@dataclass(frozen=True, slots=True)
class WorkbenchFileIntakeGuard:
    name: str
    status: WorkbenchFileIntakeGuardStatus
    reason: str
    next_smoke_check: str


@dataclass(frozen=True, slots=True)
class WorkbenchFileIntakePlan:
    status: WorkbenchFileIntakePlanStatus
    selected_package: str
    source_type: WorkbenchFileIntakeSourceType
    capture_mode: WorkbenchFileIntakeCaptureMode
    file_content_policy: str
    path_policy: str
    asset_handoff_policy: str
    parser_policy: str
    job_policy: str
    library_selection_policy: str
    required_guards: tuple[WorkbenchFileIntakeGuard, ...]
    blocked_capabilities: tuple[str, ...]
    next_step: str


class PlanWorkbenchFileSourceIntake:
    """Plan the first local-file workbench intake slice before implementing file capture."""

    def execute(
        self,
        *,
        link_trace_ready: bool,
        storage_boundary_ready: bool,
        platform_boundary_ready: bool,
    ) -> WorkbenchFileIntakePlan:
        if not link_trace_ready:
            raise ValueError("file Source intake planning requires link Source trace readiness")
        if not storage_boundary_ready:
            raise ValueError("file Source intake planning requires storage namespace readiness")
        if not platform_boundary_ready:
            raise ValueError("file Source intake planning requires platform file boundary readiness")

        return WorkbenchFileIntakePlan(
            status="planned",
            selected_package="workbench_file_source_intake_vertical_smoke",
            source_type="file",
            capture_mode="reference",
            file_content_policy="metadata_only_no_content_read",
            path_policy="no_os_absolute_path_in_product_core",
            asset_handoff_policy="asset_reference_record_required_before_parser",
            parser_policy="parser_disabled_until_asset_handoff_smoke",
            job_policy="capture_job_publishes_source_and_asset_reference_only",
            library_selection_policy="source_job_file_reference_only",
            required_guards=_required_guards(),
            blocked_capabilities=(
                "read_file_content",
                "parse_file_content",
                "copy_user_file_to_library",
                "persist_os_absolute_path",
                "publish_memory",
                "write_legacy_library",
                "expand_full_workbench_shell",
            ),
            next_step="workbench_file_source_intake_vertical_smoke",
        )


def serialize_workbench_file_intake_plan(plan: WorkbenchFileIntakePlan) -> dict[str, object]:
    return {
        "status": plan.status,
        "selected_package": plan.selected_package,
        "source_type": plan.source_type,
        "capture_mode": plan.capture_mode,
        "file_content_policy": plan.file_content_policy,
        "path_policy": plan.path_policy,
        "asset_handoff_policy": plan.asset_handoff_policy,
        "parser_policy": plan.parser_policy,
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


def _required_guards() -> tuple[WorkbenchFileIntakeGuard, ...]:
    return (
        WorkbenchFileIntakeGuard(
            name="link_trace_baseline_ready",
            status="required",
            reason="file intake must reuse the Source, Job and library trace shape proven by link intake",
            next_smoke_check="file result exposes source_id, job_id, trace_refs and library bridge item",
        ),
        WorkbenchFileIntakeGuard(
            name="storage_namespace_required",
            status="required",
            reason="Source and Asset URIs must use controlled crp:// or crp-ref:// namespaces",
            next_smoke_check="no Source or Asset field stores a Windows or macOS absolute path",
        ),
        WorkbenchFileIntakeGuard(
            name="platform_file_boundary_required",
            status="required",
            reason="Product Core receives platform-neutral file metadata rather than an OS file handle",
            next_smoke_check="file name, media type and size are explicit submission fields",
        ),
        WorkbenchFileIntakeGuard(
            name="metadata_only_capture",
            status="required",
            reason="the first file smoke proves traceability without reading or copying user file content",
            next_smoke_check="content snapshot is absent and file_content_policy stays metadata only",
        ),
        WorkbenchFileIntakeGuard(
            name="asset_handoff_required",
            status="required",
            reason="file Source must hand off a reference Asset before any parser can run",
            next_smoke_check="result includes an Asset reference state tied to the Source",
        ),
        WorkbenchFileIntakeGuard(
            name="parser_disabled",
            status="required",
            reason="parsing belongs to a later verified Asset slice, not the first file intake smoke",
            next_smoke_check="parser_version is unset and no extracted text is published",
        ),
        WorkbenchFileIntakeGuard(
            name="no_memory_publication",
            status="required",
            reason="Workbench intake still stops at Source, Asset reference and Job trace evidence",
            next_smoke_check="published output kinds exclude memory and atom contracts",
        ),
        WorkbenchFileIntakeGuard(
            name="no_full_shell_expansion",
            status="required",
            reason="the next slice should stay a narrow vertical smoke instead of rebuilding the old shell",
            next_smoke_check="UI changes remain limited to file Source intake and trace display",
        ),
    )
