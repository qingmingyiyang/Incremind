from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .ports import ObjectStorePort
from .local_asr_defaults import (
    DEFAULT_ASR_MODEL_NAME,
    DEFAULT_ASR_MODEL_PROFILE,
    DEFAULT_ASR_TIMEOUT_SECONDS,
    LOCAL_ASR_MODEL_OPTIONS,
)

DEFAULT_AUDIO_ASSET_ASR_MODEL_PROFILE = DEFAULT_ASR_MODEL_PROFILE
DEFAULT_AUDIO_ASSET_ASR_MODEL_NAME = DEFAULT_ASR_MODEL_NAME
BUILTIN_FASTER_WHISPER_COMMAND = "builtin:faster-whisper"
_MODEL_FINGERPRINT_CACHE: dict[tuple[str, int, int, int, int], tuple[str, str]] = {}
AUDIO_ASSET_ASR_MODEL_OPTIONS: tuple[dict[str, object], ...] = LOCAL_ASR_MODEL_OPTIONS


class AudioAssetTranscriptionError(ValueError):
    """Raised when generated audio asset transcription cannot run safely."""


class AudioAssetTranscriptionCancelled(AudioAssetTranscriptionError):
    """Raised when a user cancellation is observed while local ASR is running."""


@dataclass(frozen=True, slots=True)
class AudioAssetTranscriberSettings:
    status: str
    enabled: bool
    provider_name: str
    command: tuple[str, ...]
    model_profile: str
    model_name: str
    model_options: tuple[Mapping[str, object], ...]
    timeout_seconds: float
    explicit_enable_required: bool
    remote_processing: bool
    memory_publication: str


@dataclass(frozen=True, slots=True)
class AudioTranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str


@dataclass(frozen=True, slots=True)
class AudioAssetTranscriptionResult:
    status: str
    job_id: str
    output_id: str
    source_id: str
    audio_asset_id: str
    provider: str
    language: str | None
    segment_count: int
    char_count: int
    output_preview: str | None
    starts_summary: bool
    creates_memory_candidate: bool
    publishes_memory: bool
    error: str | None


class GetAudioAssetTranscriberSettings:
    _COLLECTION = "audio_asset_transcriber_settings"
    _SETTINGS_ID = "default"

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._object_store = object_store

    def execute(self) -> AudioAssetTranscriberSettings:
        record = self._object_store.read(self._COLLECTION, self._SETTINGS_ID)
        if record is None:
            shared = self._object_store.read("local_asr_provider_settings", self._SETTINGS_ID)
            if shared is not None:
                return _settings_from_record(shared)
            return _settings_from_record(_default_settings_record())
        return _settings_from_record(record)


class SaveAudioAssetTranscriberSettings:
    _COLLECTION = "audio_asset_transcriber_settings"
    _SETTINGS_ID = "default"

    def __init__(self, object_store: ObjectStorePort, *, now: str = "2026-07-02T03:05:00+08:00") -> None:
        self._object_store = object_store
        self._now = now

    def execute(
        self,
        *,
        enabled: bool,
        command: Sequence[str],
        provider_name: str = "local-faster-whisper-audio-asset-asr",
        model_profile: str = DEFAULT_AUDIO_ASSET_ASR_MODEL_PROFILE,
        model_name: str | None = None,
        timeout_seconds: float = DEFAULT_ASR_TIMEOUT_SECONDS,
        confirm_enable: bool = False,
    ) -> AudioAssetTranscriberSettings:
        clean_provider = _required_text(provider_name, "provider_name")
        clean_command = _command_tuple(command)
        clean_model_profile = _model_profile(model_profile)
        clean_model_name = _optional_str(model_name) or clean_model_profile
        clean_timeout = _positive_float(timeout_seconds, "timeout_seconds")
        if enabled:
            if confirm_enable is not True:
                raise AudioAssetTranscriptionError("enabling audio asset transcriber requires confirm_enable=true")
            if not clean_command:
                clean_command = (BUILTIN_FASTER_WHISPER_COMMAND,)
            _validate_command_executable(clean_command)
        record = {
            "schema_version": "1.0.0",
            "id": self._SETTINGS_ID,
            "enabled": bool(enabled),
            "provider_name": clean_provider,
            "command": list(clean_command),
            "model_profile": clean_model_profile,
            "model_name": clean_model_name,
            "model_options": list(AUDIO_ASSET_ASR_MODEL_OPTIONS),
            "timeout_seconds": clean_timeout,
            "remote_processing": False,
            "memory_publication": "not_started",
            "updated_at": self._now,
        }
        self._object_store.write(self._COLLECTION, self._SETTINGS_ID, record, expected_revision=None)
        return _settings_from_record(record)


class TranscribeGeneratedAudioAsset:
    """Run local ASR over a generated audio asset from video audio extraction."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-02T03:10:00+08:00",
        runner: Callable[[Sequence[str], float], subprocess.CompletedProcess[str]] | None = None,
        cancellation_requested: Callable[[], bool] | None = None,
        settings_override: AudioAssetTranscriberSettings | None = None,
        model_root: Path | None = None,
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now
        self._runner = runner
        self._cancellation_requested = cancellation_requested
        self._settings_override = settings_override
        self._model_root = model_root

    def execute(self, *, audio_asset_id: str) -> AudioAssetTranscriptionResult:
        clean_audio_asset_id = _required_text(audio_asset_id, "audio_asset_id")
        settings = self._settings_override or GetAudioAssetTranscriberSettings(
            self._object_store,
        ).execute()
        if settings.enabled is not True:
            raise AudioAssetTranscriptionError("audio asset transcriber is disabled")
        asset = self._object_store.read("audio_asset_refs", clean_audio_asset_id)
        if asset is None:
            raise AudioAssetTranscriptionError("audio asset not found")
        if asset.get("status") != "available":
            raise AudioAssetTranscriptionError("audio asset is not available")
        source_id = _required_str(asset, "source_id")
        source = self._object_store.read("sources", source_id)
        if source is None:
            raise AudioAssetTranscriptionError("source not found")
        source_type = _source_type(source)
        audio_path = Path(_required_str(asset, "path")).expanduser().resolve(strict=False)
        if not audio_path.exists() or not audio_path.is_file():
            raise AudioAssetTranscriptionError("audio asset path is not a file")
        job_id = f"media-job-transcript-{clean_audio_asset_id}"
        output_id = (
            f"media-output-transcript-{clean_audio_asset_id}"
            if asset.get("is_chunk") is True
            else f"media-output-transcript-{source_id}"
        )
        input_identity = _input_identity(audio_path, settings.model_name, settings.command, self._model_root)
        existing_output = self._object_store.read("media_processing_outputs", output_id)
        if existing_output is not None:
            return self._replay_existing(
                existing_output,
                input_identity=input_identity,
                job_id=job_id,
                output_id=output_id,
                source_id=source_id,
                audio_asset_id=clean_audio_asset_id,
                provider=settings.provider_name,
            )
        self._write_job(
            job_id,
            source_id=source_id,
            source_type=source_type,
            audio_asset_id=clean_audio_asset_id,
            status="running",
            error=None,
        )
        try:
            command = _command_for_audio(
                    settings.command,
                    audio_path,
                    model_profile=settings.model_profile,
                    model_name=settings.model_name,
                )
            completed = (
                self._runner(command, settings.timeout_seconds)
                if self._runner is not None
                else _run_command(
                    command,
                    settings.timeout_seconds,
                    cancel_requested=self._cancellation_requested,
                )
            )
            if completed.returncode != 0:
                raise AudioAssetTranscriptionError(_command_error(completed, "audio asset transcription failed"))
            transcript = _transcript_from_stdout(completed.stdout)
            text = _segments_text(transcript.segments)
            if not text:
                raise AudioAssetTranscriptionError("audio asset transcriber returned empty transcript")
            output_ref = f"crp://{self._namespace_id}/media-processing-outputs/{output_id}.json"
            preview = _preview(text)
            self._object_store.write(
                "media_processing_outputs",
                output_id,
                {
                    "schema_version": "1.0.0",
                    "id": output_id,
                    "job_id": job_id,
                    "source_id": source_id,
                    "source_type": source_type,
                    "output_kind": "transcript",
                    "status": "completed",
                    "provider": settings.provider_name,
                    "language": transcript.language,
                    "segment_count": len(transcript.segments),
                    "char_count": len(text),
                    "byte_count": len(text.encode("utf-8")),
                    "preview": preview,
                    "text": text,
                    "segments": [
                        {
                            "start_seconds": segment.start_seconds,
                            "end_seconds": segment.end_seconds,
                            "text": segment.text,
                        }
                        for segment in transcript.segments
                    ],
                    "metadata": {
                        "local_processing": True,
                        "remote_processing": False,
                        "audio_asset_id": clean_audio_asset_id,
                        "audio_asset_ref": _required_str(asset, "audio_asset_ref"),
                        "audio_path_stored_in_output": False,
                        "model_profile": settings.model_profile,
                        "model_name": settings.model_name,
                        "input_identity": input_identity,
                        "starts_summary": False,
                        "memory_publication": "not_started",
                    },
                    "memory_publication": "not_started",
                    "created_at": self._now,
                    "ref": output_ref,
                },
                expected_revision=None,
            )
            self._write_job(
                job_id,
                source_id=source_id,
                source_type=source_type,
                audio_asset_id=clean_audio_asset_id,
                status="completed",
                error=None,
                output_refs=(output_ref,),
                preview=preview,
            )
            self._mark_source(source, source_type=source_type, output_id=output_id, output_ref=output_ref, preview=preview)
            return AudioAssetTranscriptionResult(
                status="completed",
                job_id=job_id,
                output_id=output_id,
                source_id=source_id,
                audio_asset_id=clean_audio_asset_id,
                provider=settings.provider_name,
                language=transcript.language,
                segment_count=len(transcript.segments),
                char_count=len(text),
                output_preview=preview,
                starts_summary=False,
                creates_memory_candidate=False,
                publishes_memory=False,
                error=None,
            )
        except Exception as error:  # noqa: BLE001 - provider failures must be traceable.
            reason = str(error) or error.__class__.__name__
            terminal_status = "cancelled" if isinstance(error, AudioAssetTranscriptionCancelled) else "failed"
            self._write_job(
                job_id,
                source_id=source_id,
                source_type=source_type,
                audio_asset_id=clean_audio_asset_id,
                status=terminal_status,
                error=reason,
            )
            return AudioAssetTranscriptionResult(
                status=terminal_status,
                job_id=job_id,
                output_id=output_id,
                source_id=source_id,
                audio_asset_id=clean_audio_asset_id,
                provider=settings.provider_name,
                language=None,
                segment_count=0,
                char_count=0,
                output_preview=None,
                starts_summary=False,
                creates_memory_candidate=False,
                publishes_memory=False,
                error=reason,
            )

    def _replay_existing(
        self,
        output: Mapping[str, object],
        *,
        input_identity: Mapping[str, object],
        job_id: str,
        output_id: str,
        source_id: str,
        audio_asset_id: str,
        provider: str,
    ) -> AudioAssetTranscriptionResult:
        metadata = output.get("metadata")
        recorded = metadata.get("input_identity") if isinstance(metadata, Mapping) else None
        if recorded != input_identity:
            raise AudioAssetTranscriptionError("audio asset or local ASR model changed after transcript completion")
        if output.get("status") != "completed" or output.get("output_kind") != "transcript":
            raise AudioAssetTranscriptionError("existing transcript output is not replayable")
        text = _required_text(output.get("text"), "transcript text")
        return AudioAssetTranscriptionResult(
            status="completed",
            job_id=job_id,
            output_id=output_id,
            source_id=source_id,
            audio_asset_id=audio_asset_id,
            provider=_optional_str(output.get("provider")) or provider,
            language=_optional_str(output.get("language")),
            segment_count=int(output.get("segment_count", 0)),
            char_count=len(text),
            output_preview=_optional_str(output.get("preview")) or _preview(text),
            starts_summary=False,
            creates_memory_candidate=False,
            publishes_memory=False,
            error=None,
        )
    def _write_job(
        self,
        job_id: str,
        *,
        source_id: str,
        source_type: str,
        audio_asset_id: str,
        status: str,
        error: str | None,
        output_refs: Sequence[str] = (),
        preview: str | None = None,
    ) -> None:
        self._object_store.write(
            "media_processing_jobs",
            job_id,
            {
                "schema_version": "1.0.0",
                "id": job_id,
                "source_id": source_id,
                "source_type": source_type,
                "required_capability": "audio_asset_transcription",
                "projection_source": "effect_tree",
                "execution_state_owner": "core_effect_log",
                "status": status,
                "disabled_reason": None,
                "input_refs": [f"crp-ref://{self._namespace_id}/assets/{audio_asset_id}"],
                "expected_output_refs": [
                    f"crp://{self._namespace_id}/media-processing/{source_id}/transcript.json"
                ],
                "adapter_contract": {
                    "capability": "audio_asset_transcription",
                        "provider": "local_command_asr",
                        "model_profile": GetAudioAssetTranscriberSettings(self._object_store).execute().model_profile,
                        "binary_content_read": True,
                    "remote_processing": False,
                    "summary_generation": "not_started",
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

    def _mark_source(
        self,
        source: Mapping[str, object],
        *,
        source_type: str,
        output_id: str,
        output_ref: str,
        preview: str,
    ) -> None:
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        metadata_key = "audio_transcription" if source_type == "audio" else "audio_track_extraction"
        extraction = dict(
            metadata.get(metadata_key)
            if isinstance(metadata.get(metadata_key), Mapping)
            else {}
        )
        extraction.update(
            {
                "asr_state": "completed",
                "transcript_output_id": output_id,
                "transcript_output_ref": output_ref,
                "transcript_preview": preview,
                "summary_state": "not_started",
                "memory_publication": "not_started",
                "path_stored_in_source": False,
            }
        )
        metadata[metadata_key] = extraction
        updated = dict(source)
        updated["metadata"] = metadata
        self._object_store.write("sources", _required_str(source, "id"), updated, expected_revision=None)


def serialize_audio_asset_transcriber_settings(settings: AudioAssetTranscriberSettings) -> dict[str, object]:
    return {
        "status": settings.status,
        "enabled": settings.enabled,
        "provider_name": settings.provider_name,
        "command": list(settings.command),
        "model_profile": settings.model_profile,
        "model_name": settings.model_name,
        "model_options": [dict(item) for item in settings.model_options],
        "timeout_seconds": settings.timeout_seconds,
        "explicit_enable_required": settings.explicit_enable_required,
        "remote_processing": settings.remote_processing,
        "memory_publication": settings.memory_publication,
    }


def serialize_audio_asset_transcription_result(result: AudioAssetTranscriptionResult) -> dict[str, object]:
    return {
        "status": result.status,
        "job_id": result.job_id,
        "output_id": result.output_id,
        "source_id": result.source_id,
        "audio_asset_id": result.audio_asset_id,
        "provider": result.provider,
        "language": result.language,
        "segment_count": result.segment_count,
        "char_count": result.char_count,
        "output_preview": result.output_preview,
        "starts_summary": result.starts_summary,
        "creates_memory_candidate": result.creates_memory_candidate,
        "publishes_memory": result.publishes_memory,
        "error": result.error,
    }


@dataclass(frozen=True, slots=True)
class _Transcript:
    language: str | None
    segments: tuple[AudioTranscriptSegment, ...]


def _settings_from_record(record: Mapping[str, object]) -> AudioAssetTranscriberSettings:
    enabled = record.get("enabled") is True
    command = _command_tuple(record.get("command"))
    model_profile = _model_profile(record.get("model_profile"))
    model_name = _optional_str(record.get("model_name")) or model_profile
    return AudioAssetTranscriberSettings(
        status="ready" if enabled else "disabled",
        enabled=enabled,
        provider_name=_optional_str(record.get("provider_name")) or "local-faster-whisper-audio-asset-asr",
        command=command,
        model_profile=model_profile,
        model_name=model_name,
        model_options=AUDIO_ASSET_ASR_MODEL_OPTIONS,
        timeout_seconds=_positive_float(record.get("timeout_seconds", 7200.0), "timeout_seconds"),
        explicit_enable_required=True,
        remote_processing=False,
        memory_publication="not_started",
    )


def _default_settings_record() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": "default",
        "enabled": False,
        "provider_name": "local-faster-whisper-audio-asset-asr",
        "command": [],
        "model_profile": DEFAULT_AUDIO_ASSET_ASR_MODEL_PROFILE,
        "model_name": DEFAULT_AUDIO_ASSET_ASR_MODEL_NAME,
        "model_options": list(AUDIO_ASSET_ASR_MODEL_OPTIONS),
        "timeout_seconds": DEFAULT_ASR_TIMEOUT_SECONDS,
        "remote_processing": False,
        "memory_publication": "not_started",
    }


def _transcript_from_stdout(stdout: str) -> _Transcript:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AudioAssetTranscriptionError("audio asset transcriber must return transcript JSON") from exc
    if not isinstance(payload, Mapping):
        raise AudioAssetTranscriptionError("audio asset transcriber JSON must be an object")
    segments_value = payload.get("segments")
    if not isinstance(segments_value, Sequence) or isinstance(segments_value, (str, bytes)):
        raise AudioAssetTranscriptionError("audio asset transcriber JSON requires segments")
    segments: list[AudioTranscriptSegment] = []
    for segment in segments_value:
        if not isinstance(segment, Mapping):
            raise AudioAssetTranscriptionError("audio asset transcript segment must be an object")
        text = _required_text(segment.get("text"), "segment.text")
        segments.append(
            AudioTranscriptSegment(
                start_seconds=_non_negative_float(segment.get("start_seconds"), "segment.start_seconds"),
                end_seconds=_positive_float(segment.get("end_seconds"), "segment.end_seconds"),
                text=text,
            )
        )
        if segments[-1].end_seconds < segments[-1].start_seconds:
            raise AudioAssetTranscriptionError("audio asset transcript segment end must be after start")
    if not segments:
        raise AudioAssetTranscriptionError("audio asset transcriber returned no transcript segments")
    language = _optional_str(payload.get("language"))
    return _Transcript(language=language, segments=tuple(segments))


def _command_for_audio(
    command: Sequence[str],
    audio_path: Path,
    *,
    model_profile: str,
    model_name: str,
) -> tuple[str, ...]:
    clean_command = _command_tuple(command)
    if not clean_command:
        raise AudioAssetTranscriptionError("audio asset transcriber command is not configured")
    if clean_command == (BUILTIN_FASTER_WHISPER_COMMAND,):
        return (
            sys.executable,
            "-m",
            "backend.video_summary.infrastructure.local_asr_cli",
            "--audio",
            str(audio_path),
            "--model",
            model_name,
            "--mode",
            "balanced",
            "--language",
            "zh",
        )
    replaced: list[str] = []
    has_placeholder = False
    for part in clean_command:
        if "{audio_path}" in part:
            has_placeholder = True
        replaced.append(
            part
            .replace("{audio_path}", str(audio_path))
            .replace("{model_profile}", model_profile)
            .replace("{model_name}", model_name)
        )
    if not has_placeholder:
        replaced.append(str(audio_path))
    return tuple(replaced)


def _run_command(
    command: Sequence[str],
    timeout_seconds: float,
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[str]:
    with (
        tempfile.TemporaryFile(mode="w+b") as stdout_file,
        tempfile.TemporaryFile(mode="w+b") as stderr_file,
    ):
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            child_env = os.environ.copy()
            child_env["PYTHONIOENCODING"] = "utf-8"
            child_env["PYTHONUTF8"] = "1"
            process = subprocess.Popen(
                list(command),
                stdout=stdout_file,
                stderr=stderr_file,
                creationflags=creationflags,
                env=child_env,
            )
        except FileNotFoundError as exc:
            raise AudioAssetTranscriptionError("audio asset transcriber executable not found") from exc
        deadline = time.monotonic() + timeout_seconds
        while process.poll() is None:
            if cancel_requested is not None and cancel_requested():
                _terminate_process(process)
                raise AudioAssetTranscriptionCancelled("audio asset transcription cancelled by user")
            if time.monotonic() >= deadline:
                _terminate_process(process)
                raise AudioAssetTranscriptionError("audio asset transcriber timed out")
            time.sleep(0.2)
        stdout_file.seek(0)
        stderr_file.seek(0)
        return subprocess.CompletedProcess(
            list(command),
            process.returncode,
            stdout_file.read().decode("utf-8", errors="replace"),
            stderr_file.read().decode("utf-8", errors="replace"),
        )


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


def _validate_command_executable(command: Sequence[str]) -> None:
    if tuple(command) == (BUILTIN_FASTER_WHISPER_COMMAND,):
        return
    executable = command[0]
    if Path(executable).is_absolute():
        if not Path(executable).exists() or not Path(executable).is_file():
            raise AudioAssetTranscriptionError("audio asset transcriber executable not found")
        return
    if shutil.which(executable) is None:
        raise AudioAssetTranscriptionError("audio asset transcriber executable not found")


def _input_identity(
    audio_path: Path,
    model_name: str,
    command: Sequence[str],
    model_root: Path | None = None,
) -> dict[str, object]:
    identity: dict[str, object] = {
        "audio_sha256": _sha256(audio_path),
        "audio_size": audio_path.stat().st_size,
        "model_name": model_name,
    }
    if tuple(command) != (BUILTIN_FASTER_WHISPER_COMMAND,):
        identity["provider_command"] = list(command)
        return identity
    model_root = model_root if model_root is not None else _app_root() / "data" / "models" / "faster-whisper"
    model_dir = (model_root / model_name).resolve(strict=False)
    if not model_dir.is_relative_to(model_root.resolve(strict=False)):
        raise AudioAssetTranscriptionError("local ASR model escaped app data root")
    required = (model_dir / "model.bin", model_dir / "config.json")
    if not all(path.is_file() and path.stat().st_size > 0 for path in required):
        raise AudioAssetTranscriptionError("local ASR model is missing or incomplete")
    model_stat = required[0].stat()
    config_stat = required[1].stat()
    cache_key = (
        str(model_dir),
        model_stat.st_size,
        model_stat.st_mtime_ns,
        config_stat.st_size,
        config_stat.st_mtime_ns,
    )
    fingerprints = _MODEL_FINGERPRINT_CACHE.get(cache_key)
    if fingerprints is None:
        fingerprints = (_sha256(required[0]), _sha256(required[1]))
        _MODEL_FINGERPRINT_CACHE.clear()
        _MODEL_FINGERPRINT_CACHE[cache_key] = fingerprints
    identity["model_bin_sha256"], identity["config_sha256"] = fingerprints
    return identity


def _app_root() -> Path:
    configured = os.environ.get("CHRIPTMAS_APP_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return Path(__file__).resolve().parents[3]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _command_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise AudioAssetTranscriptionError("audio asset transcriber command must be a list")
    command = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    if len(command) != len(value):
        raise AudioAssetTranscriptionError("audio asset transcriber command parts must be non-empty strings")
    return command


def _segments_text(segments: Sequence[AudioTranscriptSegment]) -> str:
    return "\n".join(segment.text.strip() for segment in segments if segment.text.strip())


def _preview(text: str, limit: int = 240) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else f"{compact[: limit - 1]}..."


def _command_error(completed: subprocess.CompletedProcess[str], fallback: str) -> str:
    detail = (completed.stderr or completed.stdout or fallback).strip()
    return " ".join(detail.split())[:240] or fallback


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AudioAssetTranscriptionError(f"{field_name} is required")
    return value.strip()


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise AudioAssetTranscriptionError(f"{key} is required")
    return value


def _source_type(source: Mapping[str, object]) -> str:
    value = source.get("type")
    return value.strip() if isinstance(value, str) and value.strip() else "audio"


def _optional_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _model_profile(value: object) -> str:
    if value is None:
        return DEFAULT_AUDIO_ASSET_ASR_MODEL_PROFILE
    clean = _required_text(value, "model_profile")
    if not any(option["profile"] == clean for option in AUDIO_ASSET_ASR_MODEL_OPTIONS):
        raise AudioAssetTranscriptionError(f"unsupported audio asset ASR model profile: {clean}")
    return clean


def _positive_float(value: object, field_name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) <= 0:
        raise AudioAssetTranscriptionError(f"{field_name} must be positive")
    return float(value)


def _non_negative_float(value: object, field_name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) < 0:
        raise AudioAssetTranscriptionError(f"{field_name} must be non-negative")
    return float(value)
