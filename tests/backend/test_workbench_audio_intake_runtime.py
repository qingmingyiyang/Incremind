from __future__ import annotations

from backend.api.workbench_audio_intake_runtime import build_workbench_audio_intake_runtime
from core.product_core.workbench_source_intake_endpoint import ServeWorkbenchAudioSourceIntakeEndpoint
from core.storage_provider import JsonObjectStore
from core.effect_log import EffectLog, EffectRunner
from core.job_runner import SQLiteJobStore


def _runner(tmp_path) -> EffectRunner:
    return EffectRunner(
        EffectLog(tmp_path / "jobs.sqlite3"), owner_id="audio-workflow-test",
    )


def test_audio_runtime_uses_current_source_job_and_workflow_authorities(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / "objects", legacy_root=tmp_path / "library")
    runtime = build_workbench_audio_intake_runtime(
        tmp_path,
        store,
        namespace_id="vault-alpha",
        effect_runner=_runner(tmp_path),
    )

    result = runtime.flow.execute(
        title="Meeting",
        display_name="meeting.wav",
        media_type="audio/wav",
        size_bytes=42,
        audio_reference="crp-ref-audio",
        duration_ms=1200,
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
    assert store.read("audio_auto_workflows", result.auto_workflow.workflow_id) is not None


def test_audio_endpoint_delegates_capture_and_workflow_to_application_flow(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / "objects", legacy_root=tmp_path / "library")
    runtime = build_workbench_audio_intake_runtime(
        tmp_path,
        store,
        namespace_id="vault-alpha",
        effect_runner=_runner(tmp_path),
    )

    response = ServeWorkbenchAudioSourceIntakeEndpoint().execute(
        method="POST",
        path="/api/rebuild/workbench/audio-source-intake",
        body={
            "title": "Meeting",
            "display_name": "meeting.wav",
            "media_type": "audio/wav",
            "size_bytes": 42,
            "audio_reference": "crp-ref-audio",
            "duration_ms": 1200,
            "project_id": "project-alpha",
        },
        intake_flow=runtime.flow.execute,
    )

    assert response.status_code == 201
    assert response.body["status"] == "captured"
    assert response.body["auto_workflow"]["status"] == "blocked"
    assert response.body["auto_workflow"]["project_id"] == "project-alpha"
