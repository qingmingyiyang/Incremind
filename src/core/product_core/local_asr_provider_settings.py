from __future__ import annotations

import shutil
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .ports import ObjectStorePort

from .local_asr_provider import LocalCommandAudioTranscriptionAdapter
from .audio_asset_transcriber import BUILTIN_FASTER_WHISPER_COMMAND
from .local_asr_defaults import (
    DEFAULT_ASR_MODEL_NAME,
    DEFAULT_ASR_MODEL_PROFILE,
    DEFAULT_ASR_TIMEOUT_SECONDS,
    LOCAL_ASR_MODEL_OPTIONS,
    LOCAL_ASR_PROVIDER_NAME,
)
from .media_processing_queue import (
    MediaProcessingQueueResult,
    RunAudioTranscriptionAdapterForMediaJob,
)


class LocalAsrProviderSettingsError(ValueError):
    """Raised when local ASR Provider settings or execution are invalid."""


@dataclass(frozen=True, slots=True)
class LocalAsrProviderSettings:
    status: str
    enabled: bool
    provider_name: str
    command: tuple[str, ...]
    model_profile: str
    model_name: str
    model_options: tuple[Mapping[str, object], ...]
    diagnostic: str
    model_status: str
    timeout_seconds: float
    explicit_enable_required: bool
    remote_processing: bool
    memory_publication: str


class GetLocalAsrProviderSettings:
    """Read local ASR Provider settings without executing the Provider."""

    _COLLECTION = "local_asr_provider_settings"
    _SETTINGS_ID = "default"

    def __init__(self, object_store: ObjectStorePort, *, model_root: Path | None = None) -> None:
        self._object_store = object_store
        self._model_root = model_root

    def execute(self) -> LocalAsrProviderSettings:
        record = self._object_store.read(self._COLLECTION, self._SETTINGS_ID)
        if record is None:
            return _settings_from_record(_default_record(), self._model_root)
        return _settings_from_record(record, self._model_root)


class SaveLocalAsrProviderSettings:
    """Persist local ASR Provider settings with an explicit enable guard."""

    _COLLECTION = "local_asr_provider_settings"
    _SETTINGS_ID = "default"

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        now: str = "2026-07-01T21:55:00+08:00",
        model_root: Path | None = None,
    ) -> None:
        self._object_store = object_store
        self._now = now
        self._model_root = model_root

    def execute(
        self,
        *,
        enabled: bool,
        command: Sequence[str],
        provider_name: str = LOCAL_ASR_PROVIDER_NAME,
        model_profile: str = DEFAULT_ASR_MODEL_PROFILE,
        model_name: str | None = None,
        timeout_seconds: float = DEFAULT_ASR_TIMEOUT_SECONDS,
        confirm_enable: bool = False,
    ) -> LocalAsrProviderSettings:
        clean_provider = _required_str(provider_name, "provider_name")
        clean_command = _command_tuple(command)
        clean_model_profile = _model_profile(model_profile)
        clean_model_name = _optional_str(model_name) or clean_model_profile
        clean_timeout = _positive_float(timeout_seconds, "timeout_seconds")
        if enabled is True:
            if confirm_enable is not True:
                raise LocalAsrProviderSettingsError("enabling local ASR provider requires confirm_enable=true")
            if not clean_command:
                raise LocalAsrProviderSettingsError("enabled local ASR provider requires command")
        record = {
            "schema_version": "1.0.0",
            "id": self._SETTINGS_ID,
            "enabled": bool(enabled),
            "provider_name": clean_provider,
            "command": list(clean_command),
            "model_profile": clean_model_profile,
            "model_name": clean_model_name,
            "model_options": list(LOCAL_ASR_MODEL_OPTIONS),
            "timeout_seconds": clean_timeout,
            "explicit_enable_confirmed": bool(enabled and confirm_enable),
            "remote_processing": False,
            "memory_publication": "not_started",
            "updated_at": self._now,
        }
        self._object_store.write(self._COLLECTION, self._SETTINGS_ID, record, expected_revision=None)
        return _settings_from_record(record, self._model_root)


class RunConfiguredLocalAsrProviderForSource:
    """Run transcription for a queued audio job using stored Provider settings only."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-01T22:00:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now

    def execute(self, *, source_id: str) -> MediaProcessingQueueResult:
        clean_source_id = _required_str(source_id, "source_id")
        source = self._object_store.read("sources", clean_source_id)
        if source is None:
            raise LocalAsrProviderSettingsError("source not found")
        metadata = source.get("metadata")
        if not isinstance(metadata, Mapping):
            raise LocalAsrProviderSettingsError("source metadata is required")
        media_processing = metadata.get("media_processing")
        if not isinstance(media_processing, Mapping):
            raise LocalAsrProviderSettingsError("source has no media processing job")
        job_id = _job_id_from_media_processing(media_processing)
        settings = GetLocalAsrProviderSettings(self._object_store).execute()
        adapter = LocalCommandAudioTranscriptionAdapter(
            object_store=self._object_store,
            command=settings.command,
            enabled=settings.enabled,
            provider_name=settings.provider_name,
            model_profile=settings.model_profile,
            model_name=settings.model_name,
            timeout_seconds=settings.timeout_seconds,
        )
        return RunAudioTranscriptionAdapterForMediaJob(
            self._object_store,
            namespace_id=self._namespace_id,
            now=self._now,
        ).execute(job_id=job_id, adapter=adapter)


def serialize_local_asr_provider_settings(settings: LocalAsrProviderSettings) -> dict[str, object]:
    return {
        "status": settings.status,
        "enabled": settings.enabled,
        "provider_name": settings.provider_name,
        "command": list(settings.command),
        "model_profile": settings.model_profile,
        "model_name": settings.model_name,
        "model_options": [dict(item) for item in settings.model_options],
        "diagnostic": settings.diagnostic,
        "model_status": settings.model_status,
        "timeout_seconds": settings.timeout_seconds,
        "explicit_enable_required": settings.explicit_enable_required,
        "remote_processing": settings.remote_processing,
        "memory_publication": settings.memory_publication,
    }


def _settings_from_record(record: Mapping[str, object], model_root: Path | None = None) -> LocalAsrProviderSettings:
    enabled = record.get("enabled") is True
    command = _command_tuple(record.get("command"))
    provider_name = _optional_str(record.get("provider_name")) or LOCAL_ASR_PROVIDER_NAME
    model_profile = _model_profile(record.get("model_profile"))
    model_name = _optional_str(record.get("model_name")) or model_profile
    model_status = _model_status(model_name, model_root)
    diagnostic = _diagnostic(enabled=enabled, command=command, model_status=model_status)
    if not enabled:
        status = "disabled"
    elif diagnostic == "ready":
        status = "ready"
    else:
        status = "degraded"
    return LocalAsrProviderSettings(
        status=status,
        enabled=enabled,
        provider_name=provider_name,
        command=command,
        model_profile=model_profile,
        model_name=model_name,
        model_options=LOCAL_ASR_MODEL_OPTIONS,
        diagnostic=diagnostic,
        model_status=model_status,
        timeout_seconds=_positive_float(record.get("timeout_seconds", DEFAULT_ASR_TIMEOUT_SECONDS), "timeout_seconds"),
        explicit_enable_required=True,
        remote_processing=False,
        memory_publication="not_started",
    )


def _default_record() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": "default",
        "enabled": False,
        "provider_name": "local-command-asr",
        "command": [],
        "model_profile": DEFAULT_ASR_MODEL_PROFILE,
        "model_name": DEFAULT_ASR_MODEL_NAME,
        "model_options": list(LOCAL_ASR_MODEL_OPTIONS),
        "timeout_seconds": DEFAULT_ASR_TIMEOUT_SECONDS,
        "explicit_enable_confirmed": False,
        "remote_processing": False,
        "memory_publication": "not_started",
    }


def _diagnostic(*, enabled: bool, command: tuple[str, ...], model_status: str) -> str:
    if not enabled:
        return "disabled_until_explicit_enable"
    if not command:
        return "command_not_configured"
    if command == (BUILTIN_FASTER_WHISPER_COMMAND,):
        return "ready" if model_status == "ready" else "model_missing"
    executable = command[0]
    if Path(executable).is_absolute():
        return "ready" if Path(executable).exists() else "executable_not_found"
    return "ready" if shutil.which(executable) is not None else "executable_not_found"


def _model_status(model_name: str, model_root: Path | None = None) -> str:
    configured = os.environ.get("CHRIPTMAS_APP_ROOT", "").strip()
    app_root = Path(configured).expanduser() if configured else Path(__file__).resolve().parents[3]
    model_dir = (Path(model_root) if model_root is not None else app_root.resolve(strict=False) / "data" / "models" / "faster-whisper") / model_name
    required = (model_dir / "model.bin", model_dir / "config.json")
    return "ready" if all(path.is_file() and path.stat().st_size > 0 for path in required) else "missing"


def _positive_float(value: object, field_name: str) -> float:
    try:
        clean = float(value)
    except (TypeError, ValueError) as exc:
        raise LocalAsrProviderSettingsError(f"{field_name} must be positive") from exc
    if clean <= 0:
        raise LocalAsrProviderSettingsError(f"{field_name} must be positive")
    return clean


def _job_id_from_media_processing(media_processing: Mapping[str, object]) -> str:
    job_id = _optional_str(media_processing.get("job_id"))
    if job_id is not None:
        return job_id
    job_ref = _optional_str(media_processing.get("job_ref"))
    if job_ref is None:
        raise LocalAsrProviderSettingsError("source media processing job ref is required")
    marker = "/media-processing-jobs/"
    if marker not in job_ref or not job_ref.endswith(".json"):
        raise LocalAsrProviderSettingsError("source media processing job ref is invalid")
    return job_ref.split(marker, 1)[1][:-5]


def _command_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise LocalAsrProviderSettingsError("local ASR provider command must be a list")
    command = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    if len(command) != len(value):
        raise LocalAsrProviderSettingsError("local ASR provider command parts must be non-empty strings")
    return command


def _required_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LocalAsrProviderSettingsError(f"{field_name} is required")
    return value.strip()


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _model_profile(value: object) -> str:
    if value is None:
        return DEFAULT_ASR_MODEL_PROFILE
    clean = _required_str(value, "model_profile")
    if not any(option["profile"] == clean for option in LOCAL_ASR_MODEL_OPTIONS):
        raise LocalAsrProviderSettingsError(f"unsupported local ASR model profile: {clean}")
    return clean
