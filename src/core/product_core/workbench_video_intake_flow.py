from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .video_auto_workflow import (
    VideoAutoWorkflowResult,
    serialize_video_auto_workflow_result,
)
from .workbench_source_intake import (
    WorkbenchVideoSourceIntakeResult,
    serialize_workbench_video_source_intake,
)


@dataclass(frozen=True, slots=True)
class WorkbenchVideoIntakeFlowResult:
    capture: WorkbenchVideoSourceIntakeResult
    auto_workflow: VideoAutoWorkflowResult | None


class RunWorkbenchVideoIntakeFlow:
    """Capture video metadata and start its local post-capture workflow once."""

    def __init__(
        self,
        *,
        capture_video: Callable[..., WorkbenchVideoSourceIntakeResult],
        run_auto_workflow: Callable[..., VideoAutoWorkflowResult],
    ) -> None:
        self._capture_video = capture_video
        self._run_auto_workflow = run_auto_workflow

    def execute(
        self,
        *,
        title: str,
        display_name: str,
        media_type: str,
        size_bytes: int,
        video_reference: str,
        duration_ms: int | None,
        width_px: int | None,
        height_px: int | None,
        project_id: str | None,
    ) -> WorkbenchVideoIntakeFlowResult:
        capture = self._capture_video(
            title=title,
            display_name=display_name,
            media_type=media_type,
            size_bytes=size_bytes,
            video_reference=video_reference,
            duration_ms=duration_ms,
            width_px=width_px,
            height_px=height_px,
        )
        workflow = None
        if capture.status == "captured" and capture.source_id:
            workflow = self._run_auto_workflow(
                source_id=capture.source_id,
                project_id=_optional_text(project_id),
            )
        return WorkbenchVideoIntakeFlowResult(capture=capture, auto_workflow=workflow)


def serialize_workbench_video_intake_flow(
    result: WorkbenchVideoIntakeFlowResult,
) -> dict[str, object]:
    payload = serialize_workbench_video_source_intake(result.capture)
    if result.auto_workflow is not None:
        payload["auto_workflow"] = serialize_video_auto_workflow_result(result.auto_workflow)
    return payload


def _optional_text(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean or None
