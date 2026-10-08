from __future__ import annotations

import pytest

from core.product_core import (
    PlanWorkbenchFileSourceIntake,
    serialize_workbench_file_intake_plan,
)


def test_workbench_file_intake_plan_selects_metadata_only_file_smoke() -> None:
    plan = PlanWorkbenchFileSourceIntake().execute(
        link_trace_ready=True,
        storage_boundary_ready=True,
        platform_boundary_ready=True,
    )

    assert plan.status == "planned"
    assert plan.selected_package == "workbench_file_source_intake_vertical_smoke"
    assert plan.source_type == "file"
    assert plan.capture_mode == "reference"
    assert plan.file_content_policy == "metadata_only_no_content_read"
    assert plan.path_policy == "no_os_absolute_path_in_product_core"
    assert plan.asset_handoff_policy == "asset_reference_record_required_before_parser"
    assert plan.parser_policy == "parser_disabled_until_asset_handoff_smoke"
    assert plan.job_policy == "capture_job_publishes_source_and_asset_reference_only"
    assert plan.library_selection_policy == "source_job_file_reference_only"
    assert plan.next_step == "workbench_file_source_intake_vertical_smoke"

    guard_names = {guard.name for guard in plan.required_guards}
    assert "link_trace_baseline_ready" in guard_names
    assert "storage_namespace_required" in guard_names
    assert "platform_file_boundary_required" in guard_names
    assert "metadata_only_capture" in guard_names
    assert "asset_handoff_required" in guard_names
    assert "parser_disabled" in guard_names
    assert "no_memory_publication" in guard_names
    assert "no_full_shell_expansion" in guard_names

    assert "read_file_content" in plan.blocked_capabilities
    assert "parse_file_content" in plan.blocked_capabilities
    assert "copy_user_file_to_library" in plan.blocked_capabilities
    assert "persist_os_absolute_path" in plan.blocked_capabilities
    assert "publish_memory" in plan.blocked_capabilities
    assert "expand_full_workbench_shell" in plan.blocked_capabilities


def test_workbench_file_intake_plan_blocks_without_link_trace_readiness() -> None:
    with pytest.raises(ValueError, match="link Source trace readiness"):
        PlanWorkbenchFileSourceIntake().execute(
            link_trace_ready=False,
            storage_boundary_ready=True,
            platform_boundary_ready=True,
        )


def test_workbench_file_intake_plan_blocks_without_storage_boundary() -> None:
    with pytest.raises(ValueError, match="storage namespace readiness"):
        PlanWorkbenchFileSourceIntake().execute(
            link_trace_ready=True,
            storage_boundary_ready=False,
            platform_boundary_ready=True,
        )


def test_workbench_file_intake_plan_blocks_without_platform_boundary() -> None:
    with pytest.raises(ValueError, match="platform file boundary readiness"):
        PlanWorkbenchFileSourceIntake().execute(
            link_trace_ready=True,
            storage_boundary_ready=True,
            platform_boundary_ready=False,
        )


def test_workbench_file_intake_plan_serializer_returns_json_ready_payload() -> None:
    plan = PlanWorkbenchFileSourceIntake().execute(
        link_trace_ready=True,
        storage_boundary_ready=True,
        platform_boundary_ready=True,
    )

    payload = serialize_workbench_file_intake_plan(plan)

    assert payload["status"] == "planned"
    assert payload["selected_package"] == "workbench_file_source_intake_vertical_smoke"
    assert payload["source_type"] == "file"
    assert payload["capture_mode"] == "reference"
    assert payload["file_content_policy"] == "metadata_only_no_content_read"
    assert payload["path_policy"] == "no_os_absolute_path_in_product_core"
    assert payload["asset_handoff_policy"] == "asset_reference_record_required_before_parser"
    assert payload["parser_policy"] == "parser_disabled_until_asset_handoff_smoke"
    assert payload["job_policy"] == "capture_job_publishes_source_and_asset_reference_only"
    assert payload["library_selection_policy"] == "source_job_file_reference_only"
    assert payload["blocked_capabilities"] == list(plan.blocked_capabilities)
    assert payload["next_step"] == "workbench_file_source_intake_vertical_smoke"
    assert payload["required_guards"][0] == {
        "name": "link_trace_baseline_ready",
        "status": "required",
        "reason": (
            "file intake must reuse the Source, Job and library trace shape proven by link intake"
        ),
        "next_smoke_check": (
            "file result exposes source_id, job_id, trace_refs and library bridge item"
        ),
    }
