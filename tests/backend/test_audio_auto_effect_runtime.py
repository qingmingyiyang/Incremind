from dataclasses import replace
from types import SimpleNamespace

from backend.api import audio_auto_effect_runtime as runtime_module
from backend.api.audio_auto_effect_runtime import AudioAutoEffectRuntime
from core.effect_log import EffectLog, EffectRunner, EffectState
from core.product_core.audio_asset_transcriber import AudioAssetTranscriptionResult
from core.storage_provider import JsonObjectStore


def _result(asset_id: str) -> AudioAssetTranscriptionResult:
    return AudioAssetTranscriptionResult(
        status="completed",
        job_id=f"job-{asset_id}",
        output_id=f"output-{asset_id}",
        source_id="source-1",
        audio_asset_id=asset_id,
        provider="test",
        language=None,
        segment_count=1,
        char_count=4,
        output_preview="test",
        starts_summary=False,
        creates_memory_candidate=False,
        publishes_memory=False,
        error=None,
    )


def test_chunk_transcriptions_are_distinct_effects_under_one_source_root(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / "objects", legacy_root=tmp_path / "library")
    log = EffectLog(tmp_path / "effects.sqlite3")
    runtime = AudioAutoEffectRuntime(
        store,
        namespace_id="default",
        effect_runner=EffectRunner(log, owner_id="audio-effect-test"),
        gate_decision_id="test:v1",
    )
    calls: list[str] = []
    runtime._transcriber = SimpleNamespace(  # type: ignore[attr-defined]
        execute=lambda **kwargs: calls.append(str(kwargs["audio_asset_id"]))
        or _result(str(kwargs["audio_asset_id"]))
    )

    runtime.transcribe("source-1", audio_asset_id="chunk-1")
    runtime.transcribe("source-1", audio_asset_id="chunk-2")

    first = log.get("audio-auto:source-1:chunk-1:transcribe")
    second = log.get("audio-auto:source-1:chunk-2:transcribe")
    assert calls == ["chunk-1", "chunk-2"]
    assert first.root_id == second.root_id == "audio-auto:source-1"
    assert first.state is second.state is EffectState.SETTLED_OK


def test_deleted_audio_projection_rebuilds_from_effect_receipt(tmp_path, monkeypatch) -> None:
    store = JsonObjectStore(tmp_path / "objects", legacy_root=tmp_path / "library")
    store.write("sources", "source-1", {"id": "source-1", "metadata": {}}, expected_revision=None)
    monkeypatch.setattr(runtime_module, "_audio_readiness", lambda _store, _asset: {
        "transcriber_status": "ready",
        "transcriber_model_profile": "test",
        "transcriber_model_name": "test",
        "audio_asset_status": "available",
        "readiness_reason": None,
        "next_step": "ready_to_transcribe",
    })
    runtime = AudioAutoEffectRuntime(
        store,
        namespace_id="default",
        effect_runner=EffectRunner(EffectLog(tmp_path / "effects.sqlite3"), owner_id="audio-rebuild-test"),
        gate_decision_id="test:v1",
    )
    calls: list[str] = []
    runtime._transcriber = SimpleNamespace(  # type: ignore[attr-defined]
        execute=lambda **kwargs: calls.append(str(kwargs["audio_asset_id"]))
        or _result(str(kwargs["audio_asset_id"]))
    )

    first = runtime.execute(source_id="source-1", project_id="project-alpha", audio_asset_id="asset-1")
    assert store.delete("audio_auto_workflows", first.workflow_id) is True
    replay = runtime.execute(source_id="source-1", project_id="project-alpha", audio_asset_id="asset-1")

    assert replay == first
    assert replay.project_id == "project-alpha"
    assert calls == ["asset-1"]
    assert store.read("audio_auto_workflows", first.workflow_id)["projection_source"] == "effect_tree"
    assert store.read("audio_auto_workflows", first.workflow_id)["project_id"] == "project-alpha"
    assert store.read("sources", "source-1")["metadata"]["audio_auto_workflow"]["project_id"] == "project-alpha"


def test_cancelled_handler_abandons_effect_without_receipt(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / "objects", legacy_root=tmp_path / "library")
    log = EffectLog(tmp_path / "effects.sqlite3")
    runtime = AudioAutoEffectRuntime(
        store,
        namespace_id="default",
        effect_runner=EffectRunner(log, owner_id="audio-cancel-test"),
        gate_decision_id="test:v1",
    )
    cancelled = _result("asset-1")
    runtime._transcriber = SimpleNamespace(  # type: ignore[attr-defined]
        execute=lambda **_kwargs: replace(
            cancelled, status="cancelled", error="cancelled by user",
        )
    )

    try:
        runtime.transcribe("source-1", audio_asset_id="asset-1")
    except Exception as error:
        assert "ABANDONED" in str(error)
    else:
        raise AssertionError("cancelled Handler must not return a successful result")

    effect = log.get("audio-auto:source-1:asset-1:transcribe")
    assert effect.state is EffectState.ABANDONED
    assert effect.error_ref == "workflow.user_cancelled"
    assert effect.result_ref is None
