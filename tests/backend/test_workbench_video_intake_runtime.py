from __future__ import annotations

from backend.api.workbench_video_intake_runtime import build_workbench_video_intake_runtime
from core.product_core.workbench_source_intake_endpoint import ServeWorkbenchVideoSourceIntakeEndpoint
from core.storage_provider import JsonObjectStore
from core.effect_log import EffectLog, EffectRunner
from core.job_runner import SQLiteJobStore


def _runtime(tmp_path):
    store = JsonObjectStore(tmp_path / "objects", legacy_root=tmp_path / "library")
    runtime = build_workbench_video_intake_runtime(
        tmp_path,
        store,
        namespace_id="vault-alpha",
        effect_runner=EffectRunner(
            EffectLog(tmp_path / "jobs.sqlite3"), owner_id="video-workflow-test",
        ),
    )
    return store, runtime


def test_video_runtime_uses_current_source_job_and_workflow_authorities(tmp_path) -> None:
    store, runtime = _runtime(tmp_path)

    result = runtime.flow.execute(
        title="Demo",
        display_name="demo.mp4",
        media_type="video/mp4",
        size_bytes=42,
        video_reference="crp-ref-video",
        duration_ms=1200,
        width_px=1280,
        height_px=720,
        project_id="project-alpha",
    )

    assert result.capture.status == "captured"
    assert result.auto_workflow is not None
    assert result.auto_workflow.status == "blocked"
    assert result.auto_workflow.project_id == "project-alpha"
    assert store.read("sources", result.capture.source_id) is not None
    assert store.read("jobs", result.capture.job_id) is None
    assert (
        SQLiteJobStore(tmp_path / ".rebuild-data" / "jobs.sqlite3")
        .read(result.capture.job_id)
        is not None
    )
    assert store.read("video_auto_workflows", result.auto_workflow.workflow_id) is not None


def test_video_endpoint_delegates_capture_and_workflow_to_application_flow(tmp_path) -> None:
    _store, runtime = _runtime(tmp_path)

    response = ServeWorkbenchVideoSourceIntakeEndpoint().execute(
        method="POST",
        path="/api/rebuild/workbench/video-source-intake",
        body={
            "title": "Demo",
            "display_name": "demo.mp4",
            "media_type": "video/mp4",
            "size_bytes": 42,
            "video_reference": "crp-ref-video",
            "duration_ms": 1200,
            "width_px": 1280,
            "height_px": 720,
            "project_id": "project-alpha",
        },
        intake_flow=runtime.flow.execute,
    )

    assert response.status_code == 201
    assert response.body["status"] == "captured"
    assert response.body["auto_workflow"]["status"] == "blocked"
    assert response.body["auto_workflow"]["project_id"] == "project-alpha"
