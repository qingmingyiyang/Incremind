from types import SimpleNamespace

from core.product_core.audio_auto_decider import decide_audio_auto_workflow


def _readiness(**overrides):
    return {
        "transcriber_status": "ready",
        "transcriber_model_profile": "large-v3-turbo",
        "transcriber_model_name": "whisper-large-v3-turbo",
        "audio_asset_status": "available",
        "readiness_reason": None,
        "next_step": "ready_to_transcribe",
    } | overrides


def test_audio_decider_maps_handler_fact_without_calling_or_persisting() -> None:
    result = decide_audio_auto_workflow(
        source_id="source-audio-1",
        project_id="project-1",
        audio_asset_id="asset-1",
        readiness=_readiness(),
        transcript=SimpleNamespace(
            status="completed",
            job_id="job-1",
            output_id="output-1",
            audio_asset_id="asset-1",
            error=None,
        ),
    )

    assert result.status == "completed"
    assert result.transcript_output_id == "output-1"
    assert result.next_step == "transcript_ready"
    assert result.blocked_operations == ()


def test_audio_decider_blocks_from_readiness_fact_without_handler_outcome() -> None:
    result = decide_audio_auto_workflow(
        source_id="source-audio-1",
        project_id=None,
        audio_asset_id=None,
        readiness=_readiness(
            transcriber_status="disabled",
            audio_asset_status="missing",
            readiness_reason="audio asset id is missing",
            next_step="save_or_authorize_audio_asset",
        ),
        transcript=None,
    )

    assert result.status == "blocked"
    assert result.project_id == "default"
    assert result.steps[0].status == "blocked"
    assert result.error == "audio asset id is missing"
