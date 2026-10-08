from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .ports import ObjectStorePort

from .local_video_provider import LocalCommandVideoFrameExtractionAdapter
from .media_processing_queue import (
    MediaProcessingQueueResult,
    RunVideoFrameExtractionAdapterForMediaJob,
)


class LocalVideoProviderSettingsError(ValueError):
    """Raised when local video Provider settings or execution are invalid."""


@dataclass(frozen=True, slots=True)
class LocalVideoProviderSettings:
    status: str
    enabled: bool
    provider_name: str
    command: tuple[str, ...]
    diagnostic: str
    explicit_enable_required: bool
    remote_processing: bool
    memory_publication: str


class GetLocalVideoProviderSettings:
    """Read local video Provider settings without executing the Provider."""

    _COLLECTION = "local_video_provider_settings"
    _SETTINGS_ID = "default"

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._object_store = object_store

    def execute(self) -> LocalVideoProviderSettings:
        record = self._object_store.read(self._COLLECTION, self._SETTINGS_ID)
        if record is None:
            return _settings_from_record(_default_record())
        return _settings_from_record(record)


class SaveLocalVideoProviderSettings:
    """Persist local video Provider settings with an explicit enable guard."""

    _COLLECTION = "local_video_provider_settings"
    _SETTINGS_ID = "default"

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        now: str = "2026-07-01T22:30:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._now = now

    def execute(
        self,
        *,
        enabled: bool,
        command: Sequence[str],
        provider_name: str = "local-command-video",
        confirm_enable: bool = False,
    ) -> LocalVideoProviderSettings:
        clean_provider = _required_str(provider_name, "provider_name")
        clean_command = _command_tuple(command)
        if enabled is True:
            if confirm_enable is not True:
                raise LocalVideoProviderSettingsError("enabling local video provider requires confirm_enable=true")
            if not clean_command:
                raise LocalVideoProviderSettingsError("enabled local video provider requires command")
        record = {
            "schema_version": "1.0.0",
            "id": self._SETTINGS_ID,
            "enabled": bool(enabled),
            "provider_name": clean_provider,
            "command": list(clean_command),
            "explicit_enable_confirmed": bool(enabled and confirm_enable),
            "remote_processing": False,
            "memory_publication": "not_started",
            "updated_at": self._now,
        }
        self._object_store.write(self._COLLECTION, self._SETTINGS_ID, record, expected_revision=None)
        return _settings_from_record(record)


class RunConfiguredLocalVideoProviderForSource:
    """Run frame extraction for a queued video job using stored Provider settings only."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-01T22:35:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now

    def execute(self, *, source_id: str) -> MediaProcessingQueueResult:
        clean_source_id = _required_str(source_id, "source_id")
        source = self._object_store.read("sources", clean_source_id)
        if source is None:
            raise LocalVideoProviderSettingsError("source not found")
        metadata = source.get("metadata")
        if not isinstance(metadata, Mapping):
            raise LocalVideoProviderSettingsError("source metadata is required")
        media_processing = metadata.get("media_processing")
        if not isinstance(media_processing, Mapping):
            raise LocalVideoProviderSettingsError("source has no media processing job")
        job_id = _job_id_from_media_processing(media_processing)
        settings = GetLocalVideoProviderSettings(self._object_store).execute()
        adapter = LocalCommandVideoFrameExtractionAdapter(
            object_store=self._object_store,
            command=settings.command,
            enabled=settings.enabled,
            provider_name=settings.provider_name,
        )
        return RunVideoFrameExtractionAdapterForMediaJob(
            self._object_store,
            namespace_id=self._namespace_id,
            now=self._now,
        ).execute(job_id=job_id, adapter=adapter)


def serialize_local_video_provider_settings(settings: LocalVideoProviderSettings) -> dict[str, object]:
    return {
        "status": settings.status,
        "enabled": settings.enabled,
        "provider_name": settings.provider_name,
        "command": list(settings.command),
        "diagnostic": settings.diagnostic,
        "explicit_enable_required": settings.explicit_enable_required,
        "remote_processing": settings.remote_processing,
        "memory_publication": settings.memory_publication,
    }


def _settings_from_record(record: Mapping[str, object]) -> LocalVideoProviderSettings:
    enabled = record.get("enabled") is True
    command = _command_tuple(record.get("command"))
    provider_name = _optional_str(record.get("provider_name")) or "local-command-video"
    diagnostic = _diagnostic(enabled=enabled, command=command)
    if not enabled:
        status = "disabled"
    elif diagnostic == "ready":
        status = "ready"
    else:
        status = "degraded"
    return LocalVideoProviderSettings(
        status=status,
        enabled=enabled,
        provider_name=provider_name,
        command=command,
        diagnostic=diagnostic,
        explicit_enable_required=True,
        remote_processing=False,
        memory_publication="not_started",
    )


def _default_record() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": "default",
        "enabled": False,
        "provider_name": "local-command-video",
        "command": [],
        "explicit_enable_confirmed": False,
        "remote_processing": False,
        "memory_publication": "not_started",
    }


def _diagnostic(*, enabled: bool, command: tuple[str, ...]) -> str:
    if not enabled:
        return "disabled_until_explicit_enable"
    if not command:
        return "command_not_configured"
    executable = command[0]
    if Path(executable).is_absolute():
        return "ready" if Path(executable).exists() else "executable_not_found"
    return "ready" if shutil.which(executable) is not None else "executable_not_found"


def _job_id_from_media_processing(media_processing: Mapping[str, object]) -> str:
    job_id = _optional_str(media_processing.get("job_id"))
    if job_id is not None:
        return job_id
    job_ref = _optional_str(media_processing.get("job_ref"))
    if job_ref is None:
        raise LocalVideoProviderSettingsError("source media processing job ref is required")
    marker = "/media-processing-jobs/"
    if marker not in job_ref or not job_ref.endswith(".json"):
        raise LocalVideoProviderSettingsError("source media processing job ref is invalid")
    return job_ref.split(marker, 1)[1][:-5]


def _command_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise LocalVideoProviderSettingsError("local video provider command must be a list")
    command = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    if len(command) != len(value):
        raise LocalVideoProviderSettingsError("local video provider command parts must be non-empty strings")
    return command


def _required_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LocalVideoProviderSettingsError(f"{field_name} is required")
    return value.strip()


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
