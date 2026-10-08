from __future__ import annotations

from types import SimpleNamespace

from core.product_core.workbench_audio_intake_flow import RunWorkbenchAudioIntakeFlow


def _execute(flow: RunWorkbenchAudioIntakeFlow, *, project_id: str | None = " project-alpha "):
    return flow.execute(
        title="Meeting",
        display_name="meeting.wav",
        media_type="audio/wav",
        size_bytes=42,
        audio_reference="crp-ref-audio",
        duration_ms=1200,
        project_id=project_id,
    )


def test_captured_audio_runs_auto_workflow_once_with_normalized_project() -> None:
    calls: list[dict[str, object]] = []
    capture = SimpleNamespace(status="captured", source_id="source-audio", asset_id="asset-audio")
    workflow = SimpleNamespace(status="blocked")

    result = _execute(
        RunWorkbenchAudioIntakeFlow(
            capture_audio=lambda **_kwargs: capture,
            run_auto_workflow=lambda **kwargs: calls.append(kwargs) or workflow,
        )
    )

    assert result.capture is capture
    assert result.auto_workflow is workflow
    assert calls == [
        {
            "source_id": "source-audio",
            "project_id": "project-alpha",
            "audio_asset_id": "asset-audio",
        }
    ]


def test_non_captured_audio_never_starts_auto_workflow() -> None:
    capture = SimpleNamespace(status="rejected", source_id="source-audio", asset_id="asset-audio")

    result = _execute(
        RunWorkbenchAudioIntakeFlow(
            capture_audio=lambda **_kwargs: capture,
            run_auto_workflow=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
        )
    )

    assert result.capture is capture
    assert result.auto_workflow is None
