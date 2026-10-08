from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .audio_auto_workflow import (
    AudioAutoWorkflowResult,
    serialize_audio_auto_workflow_result,
)
from .workbench_source_intake import (
    WorkbenchAudioSourceIntakeResult,
    serialize_workbench_audio_source_intake,
)


@dataclass(frozen=True, slots=True)
class WorkbenchAudioIntakeFlowResult:
    capture: WorkbenchAudioSourceIntakeResult
    auto_workflow: AudioAutoWorkflowResult | None


class RunWorkbenchAudioIntakeFlow:
    """Capture audio metadata and start its local post-capture workflow once."""

    def __init__(
        self,
        *,
        capture_audio: Callable[..., WorkbenchAudioSourceIntakeResult],
        run_auto_workflow: Callable[..., AudioAutoWorkflowResult],
    ) -> None:
        self._capture_audio = capture_audio
        self._run_auto_workflow = run_auto_workflow

    def execute(
        self,
        *,
        title: str,
        display_name: str,
        media_type: str,
        size_bytes: int,
        audio_reference: str,
        duration_ms: int | None,
        project_id: str | None,
    ) -> WorkbenchAudioIntakeFlowResult:
        capture = self._capture_audio(
            title=title,
            display_name=display_name,
            media_type=media_type,
            size_bytes=size_bytes,
            audio_reference=audio_reference,
            duration_ms=duration_ms,
        )
        workflow = None
        if capture.status == "captured" and capture.source_id:
            workflow = self._run_auto_workflow(
                source_id=capture.source_id,
                project_id=_optional_text(project_id),
                audio_asset_id=capture.asset_id or None,
            )
        return WorkbenchAudioIntakeFlowResult(capture=capture, auto_workflow=workflow)


def serialize_workbench_audio_intake_flow(
    result: WorkbenchAudioIntakeFlowResult,
) -> dict[str, object]:
    payload = serialize_workbench_audio_source_intake(result.capture)
    if result.auto_workflow is not None:
        payload["auto_workflow"] = serialize_audio_auto_workflow_result(result.auto_workflow)
    return payload


def _optional_text(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean or None
