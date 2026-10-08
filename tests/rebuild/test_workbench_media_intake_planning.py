from __future__ import annotations

import pytest

from core.product_core import (
    PlanWorkbenchAudioVideoSourceIntake,
    serialize_workbench_media_intake_plan,
)


def test_workbench_media_intake_plan_selects_audio_before_video() -> None:
    plan = PlanWorkbenchAudioVideoSourceIntake().execute(
        image_trace_ready=True,
        media_asset_boundary_ready=True,
        platform_media_boundary_ready=True,
    )

    assert plan.status == "planned"
    assert plan.selected_modality == "audio"
    assert plan.selected_package == "workbench_audio_source_intake_vertical_smoke"
    assert plan.capture_mode == "reference"
    assert plan.media_content_policy == "metadata_only_no_media_read"
    assert plan.path_policy == "no_os_absolute_path_in_product_core"
    assert plan.asset_handoff_policy == (
        "media_asset_reference_required_before_transcription_or_extraction"
    )
    assert plan.transcription_policy == "transcription_disabled_until_explicit_audio_slice"
    assert plan.waveform_policy == "waveform_generation_disabled_until_media_processing_slice"
    assert plan.frame_extraction_policy == "frame_extraction_disabled_until_video_source_slice"
    assert plan.remote_processing_policy == "remote_media_processing_not_performed"
    assert plan.job_policy == "capture_job_publishes_source_and_media_asset_reference_only"
    assert plan.library_selection_policy == "source_job_media_asset_reference_only"
    assert plan.next_step == "workbench_audio_source_intake_vertical_smoke"

    candidates = {candidate.modality: candidate for candidate in plan.candidates}
    assert candidates["audio"].status == "selected"
    assert candidates["audio"].priority == 1
    assert candidates["video"].status == "deferred"
    assert candidates["video"].priority == 2
    assert "transcription_disabled" in candidates["audio"].required_guards
    assert "frame_extraction_disabled" in candidates["video"].required_guards


def test_workbench_media_intake_plan_requires_media_trace_guards() -> None:
    plan = PlanWorkbenchAudioVideoSourceIntake().execute(
        image_trace_ready=True,
        media_asset_boundary_ready=True,
        platform_media_boundary_ready=True,
    )

    guard_names = {guard.name for guard in plan.required_guards}
    assert "image_trace_baseline_ready" in guard_names
    assert "media_asset_reference_policy" in guard_names
    assert "platform_media_boundary_required" in guard_names
    assert "metadata_only_capture" in guard_names
    assert "transcription_disabled" in guard_names
    assert "waveform_disabled" in guard_names
    assert "frame_extraction_deferred" in guard_names
    assert "no_remote_processing" in guard_names
    assert "no_memory_publication" in guard_names
    assert "no_full_shell_expansion" in guard_names

    assert "read_audio_bytes" in plan.blocked_capabilities
    assert "read_video_bytes" in plan.blocked_capabilities
    assert "copy_user_media_to_library" in plan.blocked_capabilities
    assert "persist_os_absolute_path" in plan.blocked_capabilities
    assert "run_transcription" in plan.blocked_capabilities
    assert "generate_waveform" in plan.blocked_capabilities
    assert "extract_video_frames" in plan.blocked_capabilities
    assert "extract_audio_track_from_video" in plan.blocked_capabilities
    assert "run_remote_media_processing" in plan.blocked_capabilities
    assert "publish_memory" in plan.blocked_capabilities
    assert "expand_full_workbench_shell" in plan.blocked_capabilities

    assert "video_source_intake_vertical_smoke" in plan.deferred_capabilities
    assert "audio_transcription_slice" in plan.deferred_capabilities
    assert "waveform_preview_slice" in plan.deferred_capabilities
    assert "video_frame_extraction_slice" in plan.deferred_capabilities
    assert "media_memory_candidate_slice" in plan.deferred_capabilities


def test_workbench_media_intake_plan_blocks_without_image_trace_readiness() -> None:
    with pytest.raises(ValueError, match="image trace readiness"):
        PlanWorkbenchAudioVideoSourceIntake().execute(
            image_trace_ready=False,
            media_asset_boundary_ready=True,
            platform_media_boundary_ready=True,
        )


def test_workbench_media_intake_plan_blocks_without_media_asset_boundary() -> None:
    with pytest.raises(ValueError, match="media Asset boundary readiness"):
        PlanWorkbenchAudioVideoSourceIntake().execute(
            image_trace_ready=True,
            media_asset_boundary_ready=False,
            platform_media_boundary_ready=True,
        )


def test_workbench_media_intake_plan_blocks_without_platform_media_boundary() -> None:
    with pytest.raises(ValueError, match="platform media boundary readiness"):
        PlanWorkbenchAudioVideoSourceIntake().execute(
            image_trace_ready=True,
            media_asset_boundary_ready=True,
            platform_media_boundary_ready=False,
        )


def test_workbench_media_intake_plan_serializer_returns_json_ready_payload() -> None:
    plan = PlanWorkbenchAudioVideoSourceIntake().execute(
        image_trace_ready=True,
        media_asset_boundary_ready=True,
        platform_media_boundary_ready=True,
    )

    payload = serialize_workbench_media_intake_plan(plan)

    assert payload["status"] == "planned"
    assert payload["selected_modality"] == "audio"
    assert payload["selected_package"] == "workbench_audio_source_intake_vertical_smoke"
    assert payload["media_content_policy"] == "metadata_only_no_media_read"
    assert payload["transcription_policy"] == "transcription_disabled_until_explicit_audio_slice"
    assert payload["waveform_policy"] == "waveform_generation_disabled_until_media_processing_slice"
    assert payload["frame_extraction_policy"] == "frame_extraction_disabled_until_video_source_slice"
    assert payload["remote_processing_policy"] == "remote_media_processing_not_performed"
    assert payload["blocked_capabilities"] == list(plan.blocked_capabilities)
    assert payload["deferred_capabilities"] == list(plan.deferred_capabilities)
    assert payload["next_step"] == "workbench_audio_source_intake_vertical_smoke"
    assert payload["candidates"][0]["modality"] == "audio"
    assert payload["candidates"][0]["status"] == "selected"
    assert payload["candidates"][1]["modality"] == "video"
    assert payload["candidates"][1]["status"] == "deferred"
    assert payload["required_guards"][0] == {
        "name": "image_trace_baseline_ready",
        "status": "required",
        "reason": (
            "media intake must reuse the Source, Asset, Job and library trace shape hardened by image intake"
        ),
        "next_smoke_check": (
            "audio result exposes source_id, media Asset reference, job_id, trace refs and library bridge item"
        ),
    }
