from __future__ import annotations

from types import SimpleNamespace

from core.product_core.workbench_video_intake_flow import RunWorkbenchVideoIntakeFlow


def _execute(flow: RunWorkbenchVideoIntakeFlow, *, project_id: str | None = " project-alpha "):
    return flow.execute(
        title="Demo",
        display_name="demo.mp4",
        media_type="video/mp4",
        size_bytes=42,
        video_reference="crp-ref-video",
        duration_ms=1200,
        width_px=1280,
        height_px=720,
        project_id=project_id,
    )


def test_captured_video_runs_auto_workflow_once_with_normalized_project() -> None:
    calls: list[dict[str, object]] = []
    capture = SimpleNamespace(status="captured", source_id="source-video")
    workflow = SimpleNamespace(status="blocked")

    result = _execute(
        RunWorkbenchVideoIntakeFlow(
            capture_video=lambda **_kwargs: capture,
            run_auto_workflow=lambda **kwargs: calls.append(kwargs) or workflow,
        )
    )

    assert result.capture is capture
    assert result.auto_workflow is workflow
    assert calls == [{"source_id": "source-video", "project_id": "project-alpha"}]


def test_non_captured_video_never_starts_auto_workflow() -> None:
    capture = SimpleNamespace(status="rejected", source_id="source-video")

    result = _execute(
        RunWorkbenchVideoIntakeFlow(
            capture_video=lambda **_kwargs: capture,
            run_auto_workflow=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
        )
    )

    assert result.capture is capture
    assert result.auto_workflow is None
