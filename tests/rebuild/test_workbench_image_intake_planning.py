from __future__ import annotations

import pytest

from core.product_core import (
    PlanWorkbenchImageSourceIntake,
    serialize_workbench_image_intake_plan,
)


def test_workbench_image_intake_plan_selects_metadata_only_image_smoke() -> None:
    plan = PlanWorkbenchImageSourceIntake().execute(
        file_trace_ready=True,
        binary_asset_boundary_ready=True,
        platform_image_boundary_ready=True,
    )

    assert plan.status == "planned"
    assert plan.selected_package == "workbench_image_source_intake_vertical_smoke"
    assert plan.source_type == "image"
    assert plan.capture_mode == "reference"
    assert plan.binary_content_policy == "metadata_only_no_binary_read"
    assert plan.asset_handoff_policy == "image_asset_reference_required_before_ocr"
    assert plan.image_preview_policy == "preview_metadata_only_no_thumbnail_generation"
    assert plan.ocr_policy == "ocr_disabled_until_explicit_image_extractor_slice"
    assert plan.extractor_policy == "extractor_disabled_until_asset_verification"
    assert plan.job_policy == "capture_job_publishes_source_and_image_asset_reference_only"
    assert plan.library_selection_policy == "source_job_image_asset_reference_only"
    assert plan.next_step == "workbench_image_source_intake_vertical_smoke"

    guard_names = {guard.name for guard in plan.required_guards}
    assert "file_trace_baseline_ready" in guard_names
    assert "binary_asset_reference_policy" in guard_names
    assert "platform_image_boundary_required" in guard_names
    assert "metadata_only_capture" in guard_names
    assert "image_asset_handoff_required" in guard_names
    assert "ocr_disabled" in guard_names
    assert "extractor_disabled" in guard_names
    assert "no_memory_publication" in guard_names
    assert "no_full_shell_expansion" in guard_names

    assert "read_image_bytes" in plan.blocked_capabilities
    assert "generate_thumbnail" in plan.blocked_capabilities
    assert "run_ocr" in plan.blocked_capabilities
    assert "extract_visual_features" in plan.blocked_capabilities
    assert "copy_user_image_to_library" in plan.blocked_capabilities
    assert "persist_os_absolute_path" in plan.blocked_capabilities
    assert "publish_memory" in plan.blocked_capabilities
    assert "expand_full_workbench_shell" in plan.blocked_capabilities


def test_workbench_image_intake_plan_blocks_without_file_trace_readiness() -> None:
    with pytest.raises(ValueError, match="file trace readiness"):
        PlanWorkbenchImageSourceIntake().execute(
            file_trace_ready=False,
            binary_asset_boundary_ready=True,
            platform_image_boundary_ready=True,
        )


def test_workbench_image_intake_plan_blocks_without_binary_asset_boundary() -> None:
    with pytest.raises(ValueError, match="binary Asset boundary readiness"):
        PlanWorkbenchImageSourceIntake().execute(
            file_trace_ready=True,
            binary_asset_boundary_ready=False,
            platform_image_boundary_ready=True,
        )


def test_workbench_image_intake_plan_blocks_without_platform_image_boundary() -> None:
    with pytest.raises(ValueError, match="platform image boundary readiness"):
        PlanWorkbenchImageSourceIntake().execute(
            file_trace_ready=True,
            binary_asset_boundary_ready=True,
            platform_image_boundary_ready=False,
        )


def test_workbench_image_intake_plan_serializer_returns_json_ready_payload() -> None:
    plan = PlanWorkbenchImageSourceIntake().execute(
        file_trace_ready=True,
        binary_asset_boundary_ready=True,
        platform_image_boundary_ready=True,
    )

    payload = serialize_workbench_image_intake_plan(plan)

    assert payload["status"] == "planned"
    assert payload["selected_package"] == "workbench_image_source_intake_vertical_smoke"
    assert payload["source_type"] == "image"
    assert payload["capture_mode"] == "reference"
    assert payload["binary_content_policy"] == "metadata_only_no_binary_read"
    assert payload["asset_handoff_policy"] == "image_asset_reference_required_before_ocr"
    assert payload["image_preview_policy"] == "preview_metadata_only_no_thumbnail_generation"
    assert payload["ocr_policy"] == "ocr_disabled_until_explicit_image_extractor_slice"
    assert payload["extractor_policy"] == "extractor_disabled_until_asset_verification"
    assert payload["job_policy"] == "capture_job_publishes_source_and_image_asset_reference_only"
    assert payload["library_selection_policy"] == "source_job_image_asset_reference_only"
    assert payload["blocked_capabilities"] == list(plan.blocked_capabilities)
    assert payload["next_step"] == "workbench_image_source_intake_vertical_smoke"
    assert payload["required_guards"][0] == {
        "name": "file_trace_baseline_ready",
        "status": "required",
        "reason": (
            "image intake must reuse the Source, Asset, Job and library trace shape proven by file intake"
        ),
        "next_smoke_check": (
            "image result exposes source_id, asset reference, job_id, trace refs and library bridge item"
        ),
    }
