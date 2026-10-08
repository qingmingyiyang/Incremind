from types import SimpleNamespace

from backend.api.audio_auto_effect_runtime import AudioAutoEffectRuntime
from backend.api.long_audio_effect_runtime import LongAudioEffectRuntime
from core.effect_log import EffectLog, EffectRunner, EffectState
from core.product_core.audio_asset_transcriber import AudioAssetTranscriptionResult
from core.storage_provider import JsonObjectStore


def _transcript(asset_id: str) -> AudioAssetTranscriptionResult:
    return AudioAssetTranscriptionResult(
        status="completed", job_id=f"job-{asset_id}", output_id=f"out-{asset_id}",
        source_id="source-1", audio_asset_id=asset_id, provider="test",
        language=None, segment_count=1, char_count=1, output_preview="x",
        starts_summary=False, creates_memory_candidate=False,
        publishes_memory=False, error=None,
    )


def test_long_audio_chunks_are_effect_owned_without_private_retry_state(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / "objects", legacy_root=tmp_path / "library")
    store.write("audio_asset_refs", "asset-1", {
        "id": "asset-1", "source_id": "source-1", "status": "available",
        "duration_seconds": 1800.0, "path": str(tmp_path / "audio.wav"),
        "media_type": "audio/wav",
    }, expected_revision=None)
    log = EffectLog(tmp_path / "effects.sqlite3")
    runner = EffectRunner(log, owner_id="long-audio-test")
    audio = AudioAutoEffectRuntime(
        store, namespace_id="default", effect_runner=runner,
        gate_decision_id="test:v1",
    )
    audio._transcriber = SimpleNamespace(  # type: ignore[attr-defined]
        execute=lambda **kwargs: _transcript(str(kwargs["audio_asset_id"]))
    )
    splits: list[int] = []
    runtime = LongAudioEffectRuntime(
        store, namespace_id="default", effect_runner=runner,
        audio_runtime=audio,
        split_runner=lambda _source, output, start, _end: splits.append(int(start)) or output,
    )

    result = runtime.execute(
        source_id="source-1", project_id="project-alpha", audio_asset_id="asset-1",
    )

    assert result.status == "completed"
    assert result.project_id == "project-alpha"
    assert result.chunk_count == 2
    assert splits == [0, 900]
    assert all(chunk.retry_count == 0 for chunk in result.chunks)
    assert log.get("long-audio:source-1:split:0").state is EffectState.SETTLED_OK
    assert log.get("audio-auto:source-1:asset-1-chunk-0:transcribe").state is EffectState.SETTLED_OK
