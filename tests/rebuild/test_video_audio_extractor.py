from __future__ import annotations

import subprocess
import wave
from pathlib import Path

from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.product_core import (
    AuthorizeLocalVideoFileForSource,
    ExtractAudioTrackFromAuthorizedVideoSource,
    GetVideoAudioExtractorSettings,
    SaveVideoAudioExtractorSettings,
    VideoAudioExtractionError,
    serialize_video_audio_extraction_result,
    serialize_video_audio_extractor_settings,
)
from core.product_core.video_audio_extractor import BUILTIN_PYAV_PROBE
from core.storage_provider import JsonObjectStore


ROOT = Path(__file__).resolve().parents[2]


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _video_source(object_store: JsonObjectStore) -> dict[str, object]:
    return dict(
        ObjectStoreSourceRegistrar(object_store).register(
            SourceSubmission(
                kind="video",
                title="Downloaded Bilibili video",
                display_name="BV1xx411c7mD.mp4",
                media_type="video/mp4",
                size_bytes=1024,
                video_reference="bilibili/BV1xx411c7mD/p1",
                duration_ms=1000,
            )
        )
    )


def test_video_audio_extractor_settings_are_default_off(tmp_path: Path) -> None:
    settings = GetVideoAudioExtractorSettings(_store(tmp_path)).execute()
    payload = serialize_video_audio_extractor_settings(settings)

    assert payload["status"] == "disabled"
    assert payload["enabled"] is False
    assert payload["remote_processing"] is False
    assert payload["memory_publication"] == "not_started"


def test_video_audio_extractor_settings_require_confirmation_and_existing_tools(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_text("", encoding="utf-8")
    ffprobe.write_text("", encoding="utf-8")
    writer = SaveVideoAudioExtractorSettings(object_store)

    try:
        writer.execute(
            enabled=True,
            ffmpeg_path=str(ffmpeg),
            ffprobe_path=str(ffprobe),
            output_root=str(tmp_path / "audio"),
            confirm_enable=False,
        )
    except VideoAudioExtractionError as error:
        assert str(error) == "enabling video audio extractor requires confirm_enable=true"
    else:
        raise AssertionError("expected explicit enable guard")

    settings = writer.execute(
        enabled=True,
        ffmpeg_path=str(ffmpeg),
        ffprobe_path=str(ffprobe),
        output_root=str(tmp_path / "audio"),
        confirm_enable=True,
    )

    assert settings.status == "ready"
    assert settings.enabled is True
    assert settings.ffmpeg_path == str(ffmpeg.resolve(strict=False))
    assert settings.ffprobe_path == str(ffprobe.resolve(strict=False))


def test_extract_audio_track_writes_traceable_audio_asset_without_followup_processing(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    input_video = tmp_path / "downloaded.mp4"
    input_video.write_bytes(b"fake media")
    AuthorizeLocalVideoFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(input_video),
    )
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_text("", encoding="utf-8")
    ffprobe.write_text("", encoding="utf-8")
    settings = SaveVideoAudioExtractorSettings(object_store).execute(
        enabled=True,
        ffmpeg_path=str(ffmpeg),
        ffprobe_path=str(ffprobe),
        output_root=str(tmp_path / "audio"),
        confirm_enable=True,
    )
    commands: list[tuple[str, ...]] = []

    def runner(command: list[str] | tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        clean = tuple(command)
        commands.append(clean)
        if clean[0] == settings.ffprobe_path:
            return subprocess.CompletedProcess(args=list(clean), returncode=0, stdout="12.5\n", stderr="")
        output_path = Path(clean[-1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"wav")
        return subprocess.CompletedProcess(args=list(clean), returncode=0, stdout="", stderr="")

    result = ExtractAudioTrackFromAuthorizedVideoSource(object_store, runner=runner).execute(
        source_id=str(source["id"])
    )
    payload = serialize_video_audio_extraction_result(result)
    output = object_store.read("media_processing_outputs", str(payload["output_id"]))
    asset = object_store.read("audio_asset_refs", str(payload["audio_asset_id"]))
    updated_source = object_store.read("sources", str(source["id"]))

    assert payload["status"] == "completed"
    assert payload["duration_seconds"] == 12.5
    assert payload["sample_rate_hz"] == 16000
    assert payload["channels"] == 1
    assert payload["starts_asr"] is False
    assert payload["starts_summary"] is False
    assert payload["creates_memory_candidate"] is False
    assert payload["publishes_memory"] is False
    assert output is not None
    assert output["output_kind"] == "audio_track"
    assert output["audio_asset_ref"] == payload["audio_asset_ref"]
    assert output["metadata"]["source_path_stored_in_output"] is False
    assert output["metadata"]["audio_path_stored_in_output"] is False
    assert "path" not in output["metadata"]
    assert asset is not None
    assert asset["path_scope"] == "local_generated_audio_track"
    assert Path(str(asset["path"])).exists()
    assert updated_source is not None
    assert updated_source["metadata"]["audio_track_extraction"]["status"] == "completed"
    assert updated_source["metadata"]["audio_track_extraction"]["audio_asset_id"] == payload["audio_asset_id"]
    assert updated_source["metadata"]["audio_track_extraction"]["audio_asset_ref"] == payload["audio_asset_ref"]
    assert updated_source["metadata"]["audio_track_extraction"]["asr_state"] == "not_started"
    assert commands[1][commands[1].index("-ar") + 1] == "16000"
    assert commands[1][commands[1].index("-ac") + 1] == "1"
    assert "-vn" in commands[1]


def test_extract_audio_track_records_failed_job_without_memory_output(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    input_video = tmp_path / "downloaded.mp4"
    input_video.write_bytes(b"fake media")
    AuthorizeLocalVideoFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(input_video),
    )
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_text("", encoding="utf-8")
    ffprobe.write_text("", encoding="utf-8")
    SaveVideoAudioExtractorSettings(object_store).execute(
        enabled=True,
        ffmpeg_path=str(ffmpeg),
        ffprobe_path=str(ffprobe),
        output_root=str(tmp_path / "audio"),
        confirm_enable=True,
    )

    def runner(command: list[str] | tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        clean = tuple(command)
        if clean[0] == str(ffprobe.resolve(strict=False)):
            return subprocess.CompletedProcess(args=list(clean), returncode=0, stdout="12.5\n", stderr="")
        return subprocess.CompletedProcess(args=list(clean), returncode=1, stdout="", stderr="no audio")

    result = ExtractAudioTrackFromAuthorizedVideoSource(object_store, runner=runner).execute(
        source_id=str(source["id"])
    )
    job = object_store.read("media_processing_jobs", result.job_id)
    output = object_store.read("media_processing_outputs", result.output_id)

    assert result.status == "failed"
    assert result.error == "no audio"
    assert result.starts_asr is False
    assert result.starts_summary is False
    assert result.creates_memory_candidate is False
    assert result.publishes_memory is False
    assert job is not None
    assert job["status"] == "failed"
    assert output is None


def test_bundled_ffmpeg_extracts_real_generated_video_without_memory_publication(tmp_path: Path) -> None:
    ffmpeg = ROOT / "runtime" / "Library" / "bin" / "ffmpeg.exe"
    assert ffmpeg.is_file()
    input_video = tmp_path / "generated-real-video.mp4"
    generated = subprocess.run(
        (
            str(ffmpeg),
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x1f6feb:s=160x90:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100:duration=1",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(input_video),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert generated.returncode == 0, generated.stderr
    assert input_video.stat().st_size > 0

    object_store = _store(tmp_path)
    source = _video_source(object_store)
    authorization = AuthorizeLocalVideoFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(input_video),
    )
    SaveVideoAudioExtractorSettings(object_store).execute(
        enabled=True,
        ffmpeg_path=str(ffmpeg),
        ffprobe_path=BUILTIN_PYAV_PROBE,
        output_root=str(tmp_path / "generated-audio"),
        confirm_enable=True,
    )

    result = ExtractAudioTrackFromAuthorizedVideoSource(object_store).execute(source_id=str(source["id"]))
    output = object_store.read("media_processing_outputs", result.output_id)
    asset = object_store.read("audio_asset_refs", str(result.audio_asset_id))
    job = object_store.read("media_processing_jobs", result.job_id)

    assert result.status == "completed"
    assert result.duration_seconds is not None
    assert 0.9 <= result.duration_seconds <= 1.1
    assert result.audio_asset_ref == f"crp-ref://default/assets/audio-track-{source['id']}"
    assert result.starts_asr is False
    assert result.starts_summary is False
    assert result.creates_memory_candidate is False
    assert result.publishes_memory is False
    assert job is not None and job["status"] == "completed"
    assert output is not None
    assert output["status"] == "completed"
    assert output["metadata"]["authorization_id"] == authorization.authorization_id
    assert output["metadata"]["source_path_stored_in_output"] is False
    assert output["metadata"]["audio_path_stored_in_output"] is False
    assert "path" not in output["metadata"]
    assert asset is not None
    audio_path = Path(str(asset["path"]))
    with wave.open(str(audio_path), "rb") as reader:
        assert reader.getframerate() == 16000
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getnframes() > 0
    assert not object_store.list("memory_candidates")
    assert not object_store.list("memory_atoms")
    assert not object_store.list("staging_atoms")
    assert not object_store.list("memory_publications")
