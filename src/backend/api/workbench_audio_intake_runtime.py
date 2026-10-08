from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.api.audio_auto_effect_runtime import AudioAutoEffectRuntime
from backend.api.job_runtime import build_rebuild_job_repository
from core.ingestion_core import ObjectStoreSourceRegistrar
from core.product_core.ports import ObjectStorePort
from core.product_core.workbench_audio_intake_flow import RunWorkbenchAudioIntakeFlow
from core.product_core.workbench_source_intake import CaptureWorkbenchAudioSource
from core.effect_log import EffectRunner


@dataclass(frozen=True, slots=True)
class WorkbenchAudioIntakeRuntime:
    flow: RunWorkbenchAudioIntakeFlow


def build_workbench_audio_intake_runtime(
    runtime_root: Path,
    object_store: ObjectStorePort,
    *,
    namespace_id: str,
    effect_runner: EffectRunner,
) -> WorkbenchAudioIntakeRuntime:
    capture = CaptureWorkbenchAudioSource(
        source_registrar=ObjectStoreSourceRegistrar(object_store, namespace_id=namespace_id),
        job_repository=build_rebuild_job_repository(runtime_root, object_store),
        namespace_id=namespace_id,
    )
    workflow = AudioAutoEffectRuntime(
        object_store,
        namespace_id=namespace_id,
        effect_runner=effect_runner,
        gate_decision_id="workbench-audio-intake:v1",
    )

    def run_auto_workflow(**kwargs: object):
        source_id = str(kwargs.get("source_id", "unknown"))
        project_id = kwargs.get("project_id")
        asset_id = kwargs.get("audio_asset_id")
        return workflow.execute(
            source_id=source_id,
            project_id=project_id if isinstance(project_id, str) else None,
            audio_asset_id=asset_id if isinstance(asset_id, str) and asset_id else None,
        )

    return WorkbenchAudioIntakeRuntime(
        flow=RunWorkbenchAudioIntakeFlow(
            capture_audio=capture.execute,
            run_auto_workflow=run_auto_workflow,
        )
    )
