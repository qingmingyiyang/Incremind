from types import SimpleNamespace

from backend.api import video_auto_effect_runtime as runtime_module
from backend.api.video_auto_effect_runtime import VideoAutoEffectRuntime
from core.effect_log import EffectLog, EffectRunner
from core.storage_provider import JsonObjectStore


def test_deleted_video_projection_rebuilds_from_effect_receipts(tmp_path, monkeypatch) -> None:
    store = JsonObjectStore(tmp_path / "objects", legacy_root=tmp_path / "library")
    store.write("sources", "source-1", {"id": "source-1", "metadata": {}}, expected_revision=None)
    monkeypatch.setattr(runtime_module, "_summary_readiness", lambda _store: {
        "status": "ready", "provider_name": "test", "reason": None, "next_step": None,
    })
    log = EffectLog(tmp_path / "effects.sqlite3")
    runtime = VideoAutoEffectRuntime(
        store, namespace_id="default",
        effect_runner=EffectRunner(log, owner_id="video-effect-test"),
        gate_decision_id="test:v1",
    )
    calls: list[str] = []
    runtime._extractor = SimpleNamespace(execute=lambda **_kwargs: calls.append("extract") or SimpleNamespace(
        status="completed", job_id="job-a", output_id="out-a", source_id="source-1",
        provider="test", audio_asset_id="asset-1", audio_asset_ref="ref-1",
        duration_seconds=1.0, sample_rate_hz=16000, channels=1, output_preview=None,
        starts_asr=True, starts_summary=False, creates_memory_candidate=False,
        publishes_memory=False, error=None,
    ))
    runtime._transcriber = SimpleNamespace(execute=lambda **_kwargs: calls.append("transcribe") or SimpleNamespace(
        status="completed", job_id="job-t", output_id="transcript-1", source_id="source-1",
        audio_asset_id="asset-1", provider="test", language=None, segment_count=1,
        char_count=4, output_preview="test", starts_summary=True,
        creates_memory_candidate=False, publishes_memory=False, error=None,
    ))
    runtime._summarizer = SimpleNamespace(execute=lambda **_kwargs: calls.append("summarize") or SimpleNamespace(
        status="completed", job_id="job-s", output_id="summary-1", source_id="source-1",
        transcript_output_id="transcript-1", provider="test", title=None, chapter_count=1,
        evidence_count=1, output_preview="summary", creates_memory_candidate=False,
        publishes_memory=False, error=None, candidate_ids=(),
    ))
    runtime._candidate_creator = SimpleNamespace(execute_from_media_output=lambda **_kwargs: calls.append("candidate") or SimpleNamespace(
        status="candidate_created", project_id="default", source_id="source-1",
        candidate_id="candidate-1", candidate_status="pending", target_layer="atom",
        evidence_kind="media_output", source_refs_display=("source-1",),
        memory_publication_state="candidate_created_not_published", review_state="pending",
        blocked_operations=(),
    ))

    first = runtime.execute(source_id="source-1", project_id=None)
    assert first.status == "completed"
    assert calls == ["extract", "transcribe", "summarize", "candidate"]
    assert store.delete("video_auto_workflows", first.workflow_id) is True

    replay = runtime.execute(source_id="source-1", project_id=None)
    assert replay == first
    assert calls == ["extract", "transcribe", "summarize", "candidate"]
    assert store.read("video_auto_workflows", first.workflow_id)["projection_source"] == "effect_tree"
