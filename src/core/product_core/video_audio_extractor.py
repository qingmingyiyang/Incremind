from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .ports import ObjectStorePort


BUILTIN_PYAV_PROBE = "builtin:pyav-probe"


class VideoAudioExtractionError(ValueError):
    """Raised when video audio extraction cannot run or persist traceable output."""


@dataclass(frozen=True, slots=True)
class VideoAudioExtractorSettings:
    status: str
    enabled: bool
    provider_name: str
    ffmpeg_path: str
    ffprobe_path: str
    output_root: str
    explicit_enable_required: bool
    remote_processing: bool
    memory_publication: str


@dataclass(frozen=True, slots=True)
class VideoAudioExtractionResult:
    status: str
    job_id: str
    output_id: str
    source_id: str
    provider: str
    audio_asset_id: str | None
    audio_asset_ref: str | None
    duration_seconds: float | None
    sample_rate_hz: int
    channels: int
    output_preview: str | None
    starts_asr: bool
    starts_summary: bool
    creates_memory_candidate: bool
    publishes_memory: bool
    error: str | None


class GetVideoAudioExtractorSettings:
    _COLLECTION = "video_audio_extractor_settings"
    _SETTINGS_ID = "default"

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._object_store = object_store

    def execute(self) -> VideoAudioExtractorSettings:
        record = self._object_store.read(self._COLLECTION, self._SETTINGS_ID)
        if record is None:
            return _settings_from_record(_default_settings_record())
        return _settings_from_record(record)


class SaveVideoAudioExtractorSettings:
    _COLLECTION = "video_audio_extractor_settings"
    _SETTINGS_ID = "default"

    def __init__(self, object_store: ObjectStorePort, *, now: str = "2026-07-02T02:10:00+08:00") -> None:
        self._object_store = object_store
        self._now = now

    def execute(
        self,
        *,
        enabled: bool,
        ffmpeg_path: str,
        ffprobe_path: str,
        output_root: str,
        provider_name: str = "local-ffmpeg-audio-extractor",
        confirm_enable: bool = False,
    ) -> VideoAudioExtractorSettings:
        clean_provider = _required_text(provider_name, "provider_name")
        clean_ffmpeg = _required_existing_file(ffmpeg_path, "ffmpeg_path") if enabled else _required_text(
            ffmpeg_path, "ffmpeg_path"
        )
        clean_ffprobe = (
            BUILTIN_PYAV_PROBE
            if enabled and ffprobe_path == BUILTIN_PYAV_PROBE
            else _required_existing_file(ffprobe_path, "ffprobe_path")
            if enabled
            else _required_text(ffprobe_path, "ffprobe_path")
        )
        clean_output_root = _required_text(output_root, "output_root")
        if enabled and confirm_enable is not True:
            raise VideoAudioExtractionError("enabling video audio extractor requires confirm_enable=true")
        record = {
            "schema_version": "1.0.0",
            "id": self._SETTINGS_ID,
            "enabled": bool(enabled),
            "provider_name": clean_provider,
            "ffmpeg_path": clean_ffmpeg,
            "ffprobe_path": clean_ffprobe,
            "output_root": clean_output_root,
            "remote_processing": False,
            "memory_publication": "not_started",
            "updated_at": self._now,
        }
        self._object_store.write(self._COLLECTION, self._SETTINGS_ID, record, expected_revision=None)
        return _settings_from_record(record)


class ExtractAudioTrackFromAuthorizedVideoSource:
    """Extract a local 16k mono wav audio asset from an authorized video Source."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-02T02:20:00+08:00",
        runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
        settings_override: VideoAudioExtractorSettings | None = None,
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now
        self._runner = runner or _run_command
        self._settings_override = settings_override

    def execute(self, *, source_id: str) -> VideoAudioExtractionResult:
        clean_source_id = _required_text(source_id, "source_id")
        source = self._object_store.read("sources", clean_source_id)
        if source is None:
            raise VideoAudioExtractionError("source not found")
        if source.get("type") != "video":
            raise VideoAudioExtractionError("audio extraction requires video Source")
        settings = self._settings_override or GetVideoAudioExtractorSettings(
            self._object_store,
        ).execute()
        if settings.enabled is not True:
            raise VideoAudioExtractionError("video audio extractor is disabled")
        input_path, authorization = _authorized_video_path(self._object_store, source)
        job_id = f"media-job-audio-track-{clean_source_id}"
        output_id = f"media-output-audio-track-{clean_source_id}"
        audio_asset_id = f"audio-track-{clean_source_id}"
        audio_path = Path(settings.output_root).expanduser().resolve(strict=False) / clean_source_id / f"{audio_asset_id}.wav"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_job(job_id, source, status="running", error=None, output_refs=())
        try:
            duration = _probe_duration(self._runner, settings.ffprobe_path, input_path)
            command = (
                settings.ffmpeg_path,
                "-y",
                "-i",
                str(input_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(audio_path),
            )
            completed = self._runner(command)
            if completed.returncode != 0:
                raise VideoAudioExtractionError(_command_error(completed, "ffmpeg audio extraction failed"))
            if not audio_path.exists() or not audio_path.is_file():
                raise VideoAudioExtractionError("ffmpeg completed but audio output was not found")
            asset_ref = f"crp-ref://{self._namespace_id}/assets/{audio_asset_id}"
            output_ref = f"crp://{self._namespace_id}/media-processing-outputs/{output_id}.json"
            self._write_audio_asset(
                audio_asset_id,
                source_id=clean_source_id,
                audio_path=audio_path,
                audio_asset_ref=asset_ref,
                duration_seconds=duration,
            )
            preview = f"已抽取 16k mono wav 音频，时长 {duration:.2f} 秒。"
            self._object_store.write(
                "media_processing_outputs",
                output_id,
                {
                    "schema_version": "1.0.0",
                    "id": output_id,
                    "job_id": job_id,
                    "source_id": clean_source_id,
                    "source_type": "video",
                    "output_kind": "audio_track",
                    "status": "completed",
                    "provider": settings.provider_name,
                    "audio_asset_id": audio_asset_id,
                    "audio_asset_ref": asset_ref,
                    "duration_seconds": duration,
                    "sample_rate_hz": 16000,
                    "channels": 1,
                    "preview": preview,
                    "metadata": {
                        "local_processing": True,
                        "remote_processing": False,
                        "video_reference": _video_reference(source),
                        "authorization_id": _required_str(authorization, "id"),
                        "source_path_stored_in_output": False,
                        "audio_path_stored_in_output": False,
                    },
                    "memory_publication": "not_started",
                    "created_at": self._now,
                    "ref": output_ref,
                },
                expected_revision=None,
            )
            self._write_job(job_id, source, status="completed", error=None, output_refs=(output_ref,), preview=preview)
            self._mark_source(
                source,
                output_id=output_id,
                output_ref=output_ref,
                preview=preview,
                audio_asset_id=audio_asset_id,
                audio_asset_ref=asset_ref,
            )
            return VideoAudioExtractionResult(
                status="completed",
                job_id=job_id,
                output_id=output_id,
                source_id=clean_source_id,
                provider=settings.provider_name,
                audio_asset_id=audio_asset_id,
                audio_asset_ref=asset_ref,
                duration_seconds=duration,
                sample_rate_hz=16000,
                channels=1,
                output_preview=preview,
                starts_asr=False,
                starts_summary=False,
                creates_memory_candidate=False,
                publishes_memory=False,
                error=None,
            )
        except Exception as error:  # noqa: BLE001 - extraction failures must be traceable.
            reason = str(error) or error.__class__.__name__
            self._write_job(job_id, source, status="failed", error=reason, output_refs=())
            return VideoAudioExtractionResult(
                status="failed",
                job_id=job_id,
                output_id=output_id,
                source_id=clean_source_id,
                provider=settings.provider_name,
                audio_asset_id=None,
                audio_asset_ref=None,
                duration_seconds=None,
                sample_rate_hz=16000,
                channels=1,
                output_preview=None,
                starts_asr=False,
                starts_summary=False,
                creates_memory_candidate=False,
                publishes_memory=False,
                error=reason,
            )

    def _write_job(
        self,
        job_id: str,
        source: Mapping[str, object],
        *,
        status: str,
        error: str | None,
        output_refs: Sequence[str],
        preview: str | None = None,
    ) -> None:
        source_id = _required_str(source, "id")
        self._object_store.write(
            "media_processing_jobs",
            job_id,
            {
                "schema_version": "1.0.0",
                "id": job_id,
                "source_id": source_id,
                "source_type": "video",
                "required_capability": "video_audio_extraction",
                "projection_source": "effect_tree",
                "execution_state_owner": "core_effect_log",
                "status": status,
                "disabled_reason": None,
                "input_refs": [_required_str(source, "storage_uri")],
                "expected_output_refs": [
                    f"crp://{self._namespace_id}/media-processing/{source_id}/audio_track.json"
                ],
                "adapter_contract": {
                    "capability": "video_audio_extraction",
                    "provider": "local_ffmpeg",
                    "binary_content_read": True,
                    "remote_processing": False,
                    "memory_publication": "not_started",
                },
                "error": error,
                "activity_refs": [],
                "output_refs": list(output_refs),
                "output_preview": preview,
                "created_at": self._now,
                "updated_at": self._now,
            },
            expected_revision=None,
        )

    def _write_audio_asset(
        self,
        audio_asset_id: str,
        *,
        source_id: str,
        audio_path: Path,
        audio_asset_ref: str,
        duration_seconds: float,
    ) -> None:
        self._object_store.write(
            "audio_asset_refs",
            audio_asset_id,
            {
                "schema_version": "1.0.0",
                "id": audio_asset_id,
                "source_id": source_id,
                "audio_asset_ref": audio_asset_ref,
                "path": str(audio_path),
                "media_type": "audio/wav",
                "sample_rate_hz": 16000,
                "channels": 1,
                "duration_seconds": duration_seconds,
                "size_bytes": audio_path.stat().st_size,
                "status": "available",
                "created_at": self._now,
                "path_scope": "local_generated_audio_track",
            },
            expected_revision=None,
        )

    def _mark_source(
        self,
        source: Mapping[str, object],
        *,
        output_id: str,
        output_ref: str,
        preview: str,
        audio_asset_id: str,
        audio_asset_ref: str,
    ) -> None:
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        metadata["audio_track_extraction"] = {
            "status": "completed",
            "output_id": output_id,
            "output_ref": output_ref,
            "output_preview": preview,
            "audio_asset_id": audio_asset_id,
            "audio_asset_ref": audio_asset_ref,
            "asr_state": "not_started",
            "summary_state": "not_started",
            "memory_publication": "not_started",
            "path_stored_in_source": False,
        }
        updated = dict(source)
        updated["metadata"] = metadata
        self._object_store.write("sources", _required_str(source, "id"), updated, expected_revision=None)


def serialize_video_audio_extractor_settings(settings: VideoAudioExtractorSettings) -> dict[str, object]:
    return {
        "status": settings.status,
        "enabled": settings.enabled,
        "provider_name": settings.provider_name,
        "ffmpeg_path": settings.ffmpeg_path,
        "ffprobe_path": settings.ffprobe_path,
        "output_root": settings.output_root,
        "explicit_enable_required": settings.explicit_enable_required,
        "remote_processing": settings.remote_processing,
        "memory_publication": settings.memory_publication,
    }


def serialize_video_audio_extraction_result(result: VideoAudioExtractionResult) -> dict[str, object]:
    return {
        "status": result.status,
        "job_id": result.job_id,
        "output_id": result.output_id,
        "source_id": result.source_id,
        "provider": result.provider,
        "audio_asset_id": result.audio_asset_id,
        "audio_asset_ref": result.audio_asset_ref,
        "duration_seconds": result.duration_seconds,
        "sample_rate_hz": result.sample_rate_hz,
        "channels": result.channels,
        "output_preview": result.output_preview,
        "starts_asr": result.starts_asr,
        "starts_summary": result.starts_summary,
        "creates_memory_candidate": result.creates_memory_candidate,
        "publishes_memory": result.publishes_memory,
        "error": result.error,
    }


def _settings_from_record(record: Mapping[str, object]) -> VideoAudioExtractorSettings:
    enabled = record.get("enabled") is True
    return VideoAudioExtractorSettings(
        status="ready" if enabled else "disabled",
        enabled=enabled,
        provider_name=_optional_str(record.get("provider_name")) or "local-ffmpeg-audio-extractor",
        ffmpeg_path=_optional_str(record.get("ffmpeg_path")) or "ffmpeg",
        ffprobe_path=_optional_str(record.get("ffprobe_path")) or "ffprobe",
        output_root=_optional_str(record.get("output_root")) or "tmp/video-audio-extraction",
        explicit_enable_required=True,
        remote_processing=False,
        memory_publication="not_started",
    )


def _default_settings_record() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": "default",
        "enabled": False,
        "provider_name": "local-ffmpeg-audio-extractor",
        "ffmpeg_path": "ffmpeg",
        "ffprobe_path": "ffprobe",
        "output_root": "tmp/video-audio-extraction",
        "remote_processing": False,
        "memory_publication": "not_started",
    }


def _authorized_video_path(
    object_store: ObjectStorePort,
    source: Mapping[str, object],
) -> tuple[Path, Mapping[str, object]]:
    source_id = _required_str(source, "id")
    authorization_id = None
    metadata = source.get("metadata")
    if isinstance(metadata, Mapping):
        authorization = metadata.get("video_authorization")
        if isinstance(authorization, Mapping):
            authorization_id = _optional_str(authorization.get("authorization_id"))
    record = object_store.read("authorized_file_refs", authorization_id) if authorization_id else None
    if record is None:
        raise VideoAudioExtractionError("authorized video reference not found")
    if record.get("status") != "authorized" or record.get("source_id") != source_id:
        raise VideoAudioExtractionError("authorized video reference does not match source")
    path = Path(_required_str(record, "path")).expanduser().resolve(strict=False)
    if not path.exists() or not path.is_file():
        raise VideoAudioExtractionError("authorized video path is not a file")
    return path, record


def _probe_duration(
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    ffprobe_path: str,
    input_path: Path,
) -> float:
    if ffprobe_path == BUILTIN_PYAV_PROBE:
        try:
            import av

            with av.open(str(input_path)) as container:
                if container.duration is not None:
                    duration = float(container.duration / av.time_base)
                else:
                    durations = [
                        float(stream.duration * stream.time_base)
                        for stream in container.streams
                        if stream.duration is not None and stream.time_base is not None
                    ]
                    duration = max(durations, default=0.0)
        except Exception as exc:  # noqa: BLE001 - optional local decoder boundary.
            raise VideoAudioExtractionError("PyAV could not probe video duration") from exc
        if duration <= 0:
            raise VideoAudioExtractionError("PyAV returned non-positive duration")
        return duration
    completed = runner(
        (
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(input_path),
        )
    )
    if completed.returncode != 0:
        raise VideoAudioExtractionError(_command_error(completed, "ffprobe failed"))
    try:
        duration = float((completed.stdout or "").strip())
    except ValueError as exc:
        raise VideoAudioExtractionError("ffprobe did not return numeric duration") from exc
    if duration <= 0:
        raise VideoAudioExtractionError("ffprobe returned non-positive duration")
    return duration


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )


def _command_error(completed: subprocess.CompletedProcess[str], fallback: str) -> str:
    detail = (completed.stderr or completed.stdout or fallback).strip()
    return " ".join(detail.split())[:240] or fallback


def _video_reference(source: Mapping[str, object]) -> str:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        raise VideoAudioExtractionError("source metadata is required")
    return _required_str(metadata, "video_reference")


def _required_existing_file(value: str, field_name: str) -> str:
    clean = _required_text(value, field_name)
    path = Path(clean).expanduser().resolve(strict=False)
    if not path.exists() or not path.is_file():
        raise VideoAudioExtractionError(f"{field_name} does not exist")
    return str(path)


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VideoAudioExtractionError(f"{field_name} is required")
    return value.strip()


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise VideoAudioExtractionError(f"{key} is required")
    return value


def _optional_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
