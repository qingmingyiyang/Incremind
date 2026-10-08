from __future__ import annotations

import pytest

from core.product_core import PlanWorkbenchNextInputType, serialize_workbench_input_type_plan


def test_workbench_input_type_plan_selects_link_as_next_non_text_smoke() -> None:
    plan = PlanWorkbenchNextInputType().execute(
        text_source_selection_ready=True,
        library_bridge_ready=True,
    )

    assert plan.status == "planned"
    assert plan.selected_modality == "link"
    assert plan.selected_package == "workbench_link_source_intake_vertical_smoke"
    assert plan.next_step == "workbench_link_source_intake_vertical_smoke"
    assert "no_remote_fetch_in_next_smoke" in plan.held_boundaries
    assert "source_job_trace_required" in plan.held_boundaries
    assert "library_bridge_selection_required" in plan.held_boundaries
    assert "no_memory_publication" in plan.held_boundaries
    assert "no_full_shell_expansion" in plan.held_boundaries
    assert "no_old_intelligence_app_expansion" in plan.held_boundaries

    candidates = {candidate.modality: candidate for candidate in plan.candidates}
    assert candidates["link"].status == "selected"
    assert candidates["link"].priority == 1
    assert candidates["link"].required_guards == (
        "persist_url_as_source_metadata",
        "no_remote_fetch",
        "capture_job_trace_required",
        "library_selection_required",
    )
    assert candidates["file"].status == "deferred"
    assert candidates["image"].status == "deferred"
    assert candidates["audio"].status == "deferred"
    assert candidates["video"].status == "deferred"


def test_workbench_input_type_plan_blocks_before_text_selection_is_ready() -> None:
    with pytest.raises(ValueError, match="text Source/Job selection readiness"):
        PlanWorkbenchNextInputType().execute(
            text_source_selection_ready=False,
            library_bridge_ready=True,
        )


def test_workbench_input_type_plan_blocks_before_library_bridge_is_ready() -> None:
    with pytest.raises(ValueError, match="library bridge readiness"):
        PlanWorkbenchNextInputType().execute(
            text_source_selection_ready=True,
            library_bridge_ready=False,
        )


def test_workbench_input_type_plan_serializer_returns_json_ready_payload() -> None:
    plan = PlanWorkbenchNextInputType().execute(
        text_source_selection_ready=True,
        library_bridge_ready=True,
    )

    payload = serialize_workbench_input_type_plan(plan)

    assert payload["status"] == "planned"
    assert payload["selected_modality"] == "link"
    assert payload["selected_package"] == "workbench_link_source_intake_vertical_smoke"
    assert payload["next_step"] == "workbench_link_source_intake_vertical_smoke"
    assert payload["held_boundaries"] == list(plan.held_boundaries)
    assert payload["candidates"][0] == {
        "modality": "link",
        "status": "selected",
        "priority": 1,
        "product_value": (
            "captures a user-supplied URL as a traceable Source and Job before richer extraction"
        ),
        "implementation_risk": (
            "low if the smoke records URL metadata only and avoids remote fetch"
        ),
        "next_smoke_package": "workbench_link_source_intake_vertical_smoke",
        "required_guards": [
            "persist_url_as_source_metadata",
            "no_remote_fetch",
            "capture_job_trace_required",
            "library_selection_required",
        ],
    }
