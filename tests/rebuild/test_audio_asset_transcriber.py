from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest
from pathlib import Path

from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.product_core import (
    AudioAssetTranscriptionError,
    GetAudioAssetTranscriberSettings,
    SaveAudioAssetTranscriberSettings,
    TranscribeGeneratedAudioAsset,
    serialize_audio_asset_transcriber_settings,
    serialize_audio_asset_transcription_result,
    SaveLocalAsrProviderSettings,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _video_source(object_store: JsonObjectStore) -> dict[str, object]:
    return dict(
        ObjectStoreSourceRegistrar(object_store).register(
            SourceSubmission(
                kind="video",
                title="Video with extracted audio",
                display_name="video.mp4",
                media_type="video/mp4",
                size_bytes=2048,
                video_reference="bilibili/BV1xx411c7mD/p1",
                duration_ms=1000,
            )
        )
    )


def _audio_source(object_store: JsonObjectStore) -> dict[str, object]:
    return dict(
        ObjectStoreSourceRegistrar(object_store).register(
            SourceSubmission(
                kind="audio",
                title="Audio with local asset",
                display_name="meeting.mp3",
                media_type="audio/mpeg",
                size_bytes=2048,
                audio_reference="platform-audio-ref",
                duration_ms=1000,
            )
        )
    )


def _audio_asset(object_store: JsonObjectStore, source_id: str, audio_path: Path) -> str:
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"wav")
    audio_asset_id = f"audio-track-{source_id}"
    object_store.write(
        "audio_asset_refs",
        audio_asset_id,
        {
            "schema_version": "1.0.0",
            "id": audio_asset_id,
            "source_id": source_id,
            "audio_asset_ref": f"crp-ref://default/assets/{audio_asset_id}",
            "path": str(audio_path),
            "media_type": "audio/wav",
            "sample_rate_hz": 16000,
            "channels": 1,
            "duration_seconds": 12.5,
            "size_bytes": audio_path.stat().st_size,
            "status": "available",
            "created_at": "2026-07-02T03:00:00+08:00",
            "path_scope": "local_generated_audio_track",
        },
        expected_revision=None,
    )
    source = object_store.read("sources", source_id)
    assert source is not None
    metadata = dict(source["metadata"])
    metadata["audio_track_extraction"] = {
        "status": "completed",
        "output_id": f"media-output-audio-track-{source_id}",
        "output_ref": f"crp://default/media-processing-outputs/media-output-audio-track-{source_id}.json",
        "asr_state": "not_started",
        "summary_state": "not_started",
        "memory_publication": "not_started",
        "path_stored_in_source": False,
    }
    updated = dict(source)
    updated["metadata"] = metadata
    object_store.write("sources", source_id, updated, expected_revision=None)
    return audio_asset_id


def test_audio_asset_transcriber_settings_are_default_off(tmp_path: Path) -> None:
    payload = serialize_audio_asset_transcriber_settings(GetAudioAssetTranscriberSettings(_store(tmp_path)).execute())

    assert payload["status"] == "disabled"
    assert payload["enabled"] is False
    assert payload["model_profile"] == "large-v3-turbo"
    assert payload["model_name"] == "large-v3-turbo"
    assert any(option["recommended"] is True for option in payload["model_options"])
    assert payload["remote_processing"] is False
    assert payload["memory_publication"] == "not_started"


def test_builtin_local_asr_settings_compose_audio_asset_workflow_and_replay(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("CHRIPTMAS_APP_ROOT", str(tmp_path))
    model_dir = tmp_path / "data" / "models" / "faster-whisper" / "large-v3-turbo"
    model_dir.mkdir(parents=True)
    (model_dir / "model.bin").write_bytes(b"model-v1")
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    from backend.video_summary.infrastructure.faster_whisper_models import FasterWhisperModelManager
    from backend.video_summary.infrastructure.huggingface_model_downloader import write_downloaded_model_manifest
    manager = FasterWhisperModelManager(model_dir.parent)
    write_downloaded_model_manifest(model_dir, manager.download_spec("large-v3-turbo"))
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    audio_path = tmp_path / "audio" / "track.wav"
    audio_asset_id = _audio_asset(object_store, str(source["id"]), audio_path)
    SaveLocalAsrProviderSettings(object_store).execute(
        enabled=True,
        command=("builtin:faster-whisper",),
        confirm_enable=True,
    )
    settings = GetAudioAssetTranscriberSettings(object_store).execute()
    commands: list[tuple[str, ...]] = []

    def runner(command, timeout_seconds):
        commands.append(tuple(command))
        assert timeout_seconds == 7200.0
        return subprocess.CompletedProcess(
            args=list(command),
            returncode=0,
            stdout=json.dumps({
                "language": "zh",
                "segments": [{"start_seconds": 0.0, "end_seconds": 1.0, "text": "内建转写。"}],
            }, ensure_ascii=False),
            stderr="",
        )

    first = TranscribeGeneratedAudioAsset(object_store, runner=runner).execute(
        audio_asset_id=audio_asset_id
    )
    replay = TranscribeGeneratedAudioAsset(
        object_store,
        runner=lambda *_: (_ for _ in ()).throw(AssertionError("replay must not invoke ASR")),
    ).execute(audio_asset_id=audio_asset_id)

    assert settings.command == ("builtin:faster-whisper",)
    assert commands[0][:3] == (
        sys.executable,
        "-m",
        "backend.video_summary.infrastructure.local_asr_cli",
    )
    assert first.status == replay.status == "completed"
    assert first.output_id == replay.output_id
    output = object_store.read("media_processing_outputs", first.output_id)
    assert output is not None
    assert output["metadata"]["remote_processing"] is False
    assert output["metadata"]["input_identity"]["model_bin_sha256"]

    audio_path.write_bytes(b"changed-wav")
    try:
        TranscribeGeneratedAudioAsset(object_store, runner=runner).execute(audio_asset_id=audio_asset_id)
    except AudioAssetTranscriptionError as error:
        assert "changed after transcript completion" in str(error)
    else:
        raise AssertionError("expected completed transcript input drift to fail closed")


def test_chunk_audio_assets_receive_distinct_transcript_outputs(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)
    executable = tmp_path / "asr.exe"
    executable.write_text("", encoding="utf-8")
    SaveAudioAssetTranscriberSettings(object_store).execute(
        enabled=True,
        command=(str(executable), "{audio_path}"),
        confirm_enable=True,
    )
    asset_ids = []
    for index in range(2):
        path = tmp_path / f"chunk-{index}.wav"
        asset_id = _audio_asset(object_store, str(source["id"]), path)
        asset_id = f"{asset_id}-chunk-{index}"
        object_store.write("audio_asset_refs", asset_id, {
            "id": asset_id,
            "source_id": source["id"],
            "audio_asset_ref": f"crp-ref://default/assets/{asset_id}",
            "path": str(path),
            "status": "available",
            "is_chunk": True,
        }, expected_revision=None)
        asset_ids.append(asset_id)

    def runner(command, timeout_seconds):
        return subprocess.CompletedProcess(
            args=list(command), returncode=0,
            stdout=json.dumps({"language": "zh", "segments": [
                {"start_seconds": 0.0, "end_seconds": 1.0, "text": Path(command[-1]).stem}
            ]}), stderr="",
        )

    results = [TranscribeGeneratedAudioAsset(object_store, runner=runner).execute(audio_asset_id=item)
               for item in asset_ids]
    assert results[0].output_id != results[1].output_id


def test_running_local_asr_observes_effect_owned_user_cancel(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)
    audio_asset_id = _audio_asset(object_store, str(source["id"]), tmp_path / "meeting.wav")
    marker = tmp_path / "provider-finished.txt"
    script = tmp_path / "slow_asr.py"
    script.write_text(
        "import pathlib, sys, time\n"
        "time.sleep(10)\n"
        "pathlib.Path(sys.argv[1]).write_text('finished', encoding='utf-8')\n"
        "print('{\"language\":\"zh\",\"segments\":[{\"start_seconds\":0,\"end_seconds\":1,\"text\":\"late\"}]}')\n",
        encoding="utf-8",
    )
    SaveAudioAssetTranscriberSettings(object_store).execute(
        enabled=True,
        command=(sys.executable, str(script), str(marker), "{audio_path}"),
        timeout_seconds=30,
        confirm_enable=True,
    )
    captured = {}
    cancellation = threading.Event()

    def run():
        captured["result"] = TranscribeGeneratedAudioAsset(
            object_store, cancellation_requested=cancellation.is_set,
        ).execute(
            audio_asset_id=audio_asset_id
        )

    worker = threading.Thread(target=run)
    worker.start()
    job_id = f"media-job-transcript-{audio_asset_id}"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = object_store.read("media_processing_jobs", job_id)
        if job is not None and job.get("status") == "running":
            cancellation.set()
            break
        time.sleep(0.05)
    else:
        raise AssertionError("ASR job did not enter running state")
    worker.join(timeout=8)

    assert worker.is_alive() is False
    assert captured["result"].status == "cancelled"
    assert object_store.read("media_processing_jobs", job_id)["status"] == "cancelled"
    assert marker.exists() is False


def test_local_asr_large_json_stdout_does_not_deadlock(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)
    audio_asset_id = _audio_asset(object_store, str(source["id"]), tmp_path / "large-output.wav")
    script = tmp_path / "large_output_asr.py"
    script.write_text(
        "import json\n"
        "print(json.dumps({'language':'zh','segments':[{'start_seconds':0,'end_seconds':1,'text':'字'*200000}]}, ensure_ascii=False))\n",
        encoding="utf-8",
    )
    SaveAudioAssetTranscriberSettings(object_store).execute(
        enabled=True,
        command=(sys.executable, str(script), "{audio_path}"),
        timeout_seconds=10,
        confirm_enable=True,
    )

    result = TranscribeGeneratedAudioAsset(object_store).execute(audio_asset_id=audio_asset_id)

    assert result.status == "completed"
    assert result.char_count == 200000


@pytest.mark.skipif(os.name != "nt", reason="uses Windows taskkill process-tree semantics")
def test_process_kill_then_restart_replays_unfinished_local_asr(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)
    audio_asset_id = _audio_asset(object_store, str(source["id"]), tmp_path / "restart.wav")
    mode = tmp_path / "provider-mode.txt"
    mode.write_text("slow", encoding="utf-8")
    provider = tmp_path / "restart_provider.py"
    provider.write_text(
        "import json, pathlib, sys, time\n"
        "if pathlib.Path(sys.argv[1]).read_text(encoding='utf-8') == 'slow': time.sleep(30)\n"
        "print(json.dumps({'language':'zh','segments':[{'start_seconds':0,'end_seconds':1,'text':'restart recovered'}]}))\n",
        encoding="utf-8",
    )
    SaveAudioAssetTranscriberSettings(object_store).execute(
        enabled=True,
        command=(sys.executable, str(provider), str(mode), "{audio_path}"),
        timeout_seconds=60,
        confirm_enable=True,
    )
    driver = tmp_path / "restart_driver.py"
    driver.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "from core.product_core import TranscribeGeneratedAudioAsset\n"
        "from core.storage_provider import JsonObjectStore\n"
        "root=Path(os.environ['ASR_RESTART_ROOT'])\n"
        "store=JsonObjectStore(root/'.rebuild-data', legacy_root=root/'library')\n"
        "result=TranscribeGeneratedAudioAsset(store).execute(audio_asset_id=os.environ['ASR_ASSET_ID'])\n"
        "print(result.status)\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "ASR_RESTART_ROOT": str(tmp_path),
        "ASR_ASSET_ID": audio_asset_id,
    }
    first = subprocess.Popen([sys.executable, str(driver)], env=env)
    job_id = f"media-job-transcript-{audio_asset_id}"
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = object_store.read("media_processing_jobs", job_id)
        if job is not None and job.get("status") == "running":
            break
        time.sleep(0.05)
    else:
        subprocess.run(["taskkill", "/PID", str(first.pid), "/T", "/F"], capture_output=True)
        raise AssertionError("crash fixture did not enter running state")
    subprocess.run(["taskkill", "/PID", str(first.pid), "/T", "/F"], check=False, capture_output=True)
    first.wait(timeout=8)
    assert object_store.list("media_processing_outputs") == ()

    mode.write_text("ready", encoding="utf-8")
    recovered = subprocess.run(
        [sys.executable, str(driver)], env=env, check=False, capture_output=True, text=True, timeout=15
    )
    outputs = object_store.list("media_processing_outputs")

    assert recovered.returncode == 0
    assert recovered.stdout.strip() == "completed"
    assert len(outputs) == 1
    assert object_store.read("media_processing_jobs", job_id)["status"] == "completed"


def test_audio_asset_transcriber_settings_require_confirmation_and_executable(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    executable = tmp_path / "asr.exe"
    executable.write_text("", encoding="utf-8")
    writer = SaveAudioAssetTranscriberSettings(object_store)

    try:
        writer.execute(enabled=True, command=(str(executable), "{audio_path}"), confirm_enable=False)
    except AudioAssetTranscriptionError as error:
        assert str(error) == "enabling audio asset transcriber requires confirm_enable=true"
    else:
        raise AssertionError("expected explicit enable guard")

    settings = writer.execute(
        enabled=True,
        command=(str(executable), "{audio_path}"),
        provider_name="local-faster-whisper-compatible",
        model_profile="large-v3-turbo",
        confirm_enable=True,
    )

    assert settings.status == "ready"
    assert settings.enabled is True
    assert settings.command == (str(executable), "{audio_path}")
    assert settings.model_profile == "large-v3-turbo"


def test_transcribe_generated_audio_asset_writes_segments_without_followup_processing(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    audio_asset_id = _audio_asset(object_store, str(source["id"]), tmp_path / "audio" / "track.wav")
    executable = tmp_path / "asr.exe"
    executable.write_text("", encoding="utf-8")
    settings = SaveAudioAssetTranscriberSettings(object_store).execute(
        enabled=True,
        command=(str(executable), "--model", "{model_name}", "--json", "{audio_path}"),
        model_profile="large-v3-turbo",
        model_name="large-v3-turbo",
        confirm_enable=True,
    )
    commands: list[tuple[str, ...]] = []

    def runner(command: list[str] | tuple[str, ...], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        clean = tuple(command)
        commands.append(clean)
        assert timeout_seconds == settings.timeout_seconds
        assert clean[2] == "large-v3-turbo"
        assert Path(clean[-1]).exists()
        return subprocess.CompletedProcess(
            args=list(clean),
            returncode=0,
            stdout=json.dumps(
                {
                    "language": "zh",
                    "segments": [
                        {"start_seconds": 0.0, "end_seconds": 1.5, "text": "第一段转写。"},
                        {"start_seconds": 1.5, "end_seconds": 3.0, "text": "第二段转写。"},
                    ],
                },
                ensure_ascii=False,
            ),
            stderr="",
        )

    result = TranscribeGeneratedAudioAsset(object_store, runner=runner).execute(audio_asset_id=audio_asset_id)
    payload = serialize_audio_asset_transcription_result(result)
    output = object_store.read("media_processing_outputs", str(payload["output_id"]))
    job = object_store.read("media_processing_jobs", str(payload["job_id"]))
    updated_source = object_store.read("sources", str(source["id"]))

    assert payload["status"] == "completed"
    assert payload["language"] == "zh"
    assert payload["segment_count"] == 2
    assert payload["char_count"] == len("第一段转写。\n第二段转写。")
    assert payload["starts_summary"] is False
    assert payload["creates_memory_candidate"] is False
    assert payload["publishes_memory"] is False
    assert output is not None
    assert output["output_kind"] == "transcript"
    assert output["text"] == "第一段转写。\n第二段转写。"
    assert output["segments"][0]["start_seconds"] == 0.0
    assert output["metadata"]["audio_asset_id"] == audio_asset_id
    assert output["metadata"]["model_profile"] == "large-v3-turbo"
    assert output["metadata"]["model_name"] == "large-v3-turbo"
    assert output["metadata"]["audio_path_stored_in_output"] is False
    assert output["metadata"]["starts_summary"] is False
    assert "path" not in output["metadata"]
    assert job is not None
    assert job["status"] == "completed"
    assert job["required_capability"] == "audio_asset_transcription"
    assert updated_source is not None
    extraction = updated_source["metadata"]["audio_track_extraction"]
    assert extraction["asr_state"] == "completed"
    assert extraction["summary_state"] == "not_started"
    assert extraction["memory_publication"] == "not_started"
    assert commands[0][0] == settings.command[0]


def test_transcribe_generated_audio_asset_records_failed_job_without_output(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    audio_asset_id = _audio_asset(object_store, str(source["id"]), tmp_path / "audio" / "track.wav")
    executable = tmp_path / "asr.exe"
    executable.write_text("", encoding="utf-8")
    SaveAudioAssetTranscriberSettings(object_store).execute(
        enabled=True,
        command=(str(executable), "{audio_path}"),
        confirm_enable=True,
    )

    def runner(command: list[str] | tuple[str, ...], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=list(command), returncode=1, stdout="", stderr="model missing")

    result = TranscribeGeneratedAudioAsset(object_store, runner=runner).execute(audio_asset_id=audio_asset_id)
    job = object_store.read("media_processing_jobs", result.job_id)
    output = object_store.read("media_processing_outputs", result.output_id)

    assert result.status == "failed"
    assert result.error == "model missing"
    assert result.starts_summary is False
    assert result.creates_memory_candidate is False
    assert result.publishes_memory is False
    assert job is not None
    assert job["status"] == "failed"
    assert output is None


def test_transcribe_generated_audio_asset_marks_audio_source_metadata(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)
    audio_asset_id = _audio_asset(object_store, str(source["id"]), tmp_path / "audio" / "meeting.wav")
    executable = tmp_path / "asr.exe"
    executable.write_text("", encoding="utf-8")
    SaveAudioAssetTranscriberSettings(object_store).execute(
        enabled=True,
        command=(str(executable), "{audio_path}"),
        confirm_enable=True,
    )

    def runner(command: list[str] | tuple[str, ...], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=list(command),
            returncode=0,
            stdout=json.dumps(
                {
                    "language": "zh",
                    "segments": [{"start_seconds": 0.0, "end_seconds": 1.0, "text": "音频转写完成。"}],
                },
                ensure_ascii=False,
            ),
            stderr="",
        )

    result = TranscribeGeneratedAudioAsset(object_store, runner=runner).execute(audio_asset_id=audio_asset_id)
    output = object_store.read("media_processing_outputs", result.output_id)
    job = object_store.read("media_processing_jobs", result.job_id)
    updated_source = object_store.read("sources", str(source["id"]))

    assert result.status == "completed"
    assert output is not None
    assert output["source_type"] == "audio"
    assert job is not None
    assert job["source_type"] == "audio"
    assert updated_source is not None
    assert updated_source["metadata"]["audio_transcription"]["asr_state"] == "completed"
