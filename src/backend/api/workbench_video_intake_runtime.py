from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.api.job_runtime import build_rebuild_job_repository
from backend.api.video_auto_effect_runtime import VideoAutoEffectRuntime
from core.effect_log import EffectRunner
from core.ingestion_core import ObjectStoreSourceRegistrar
from core.product_core.ports import ObjectStorePort
from core.product_core.workbench_source_intake import CaptureWorkbenchVideoSource
from core.product_core.workbench_video_intake_flow import RunWorkbenchVideoIntakeFlow


@dataclass(frozen=True, slots=True)
class WorkbenchVideoIntakeRuntime:
    flow: RunWorkbenchVideoIntakeFlow


def build_workbench_video_intake_runtime(
    runtime_root: Path,
    object_store: ObjectStorePort,
    *,
    namespace_id: str,
    effect_runner: EffectRunner,
) -> WorkbenchVideoIntakeRuntime:
    capture = CaptureWorkbenchVideoSource(
        source_registrar=ObjectStoreSourceRegistrar(
            object_store, namespace_id=namespace_id,
        ),
        job_repository=build_rebuild_job_repository(runtime_root, object_store),
        namespace_id=namespace_id,
    )
    workflow = VideoAutoEffectRuntime(
        object_store,
        namespace_id=namespace_id,
        effect_runner=effect_runner,
        gate_decision_id="workbench-video-intake:v1",
    )
    return WorkbenchVideoIntakeRuntime(
        flow=RunWorkbenchVideoIntakeFlow(
            capture_video=capture.execute,
            run_auto_workflow=workflow.execute,
        )
    )
