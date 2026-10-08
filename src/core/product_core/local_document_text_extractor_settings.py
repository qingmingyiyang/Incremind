from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .ports import ObjectStorePort

from .local_document_text_extractor import (
    BUILTIN_DOCUMENT_TEXT_COMMAND,
    BuiltinDocumentTextExtractor,
    LocalCommandDocumentTextExtractor,
    document_text_extractors_for_allowed_documents,
)
from .source_content_read import ReadSourceTextContent, SourceContentReadResult


class LocalDocumentTextExtractorSettingsError(ValueError):
    """Raised when local document text extractor settings are invalid."""


@dataclass(frozen=True, slots=True)
class LocalDocumentTextExtractorSettings:
    status: str
    enabled: bool
    provider_name: str
    command: tuple[str, ...]
    diagnostic: str
    explicit_enable_required: bool
    remote_processing: bool
    memory_publication: str


class GetLocalDocumentTextExtractorSettings:
    """Read local document text extractor settings without executing the extractor."""

    _COLLECTION = "local_document_text_extractor_settings"
    _SETTINGS_ID = "default"

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._object_store = object_store

    def execute(self) -> LocalDocumentTextExtractorSettings:
        record = self._object_store.read(self._COLLECTION, self._SETTINGS_ID)
        if record is None:
            return _settings_from_record(_default_record())
        return _settings_from_record(record)


class SaveLocalDocumentTextExtractorSettings:
    """Persist local document text extractor settings with an explicit enable guard."""

    _COLLECTION = "local_document_text_extractor_settings"
    _SETTINGS_ID = "default"

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        now: str = "2026-07-01T22:35:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._now = now

    def execute(
        self,
        *,
        enabled: bool,
        command: Sequence[str],
        provider_name: str = "local-command-document-text",
        confirm_enable: bool = False,
    ) -> LocalDocumentTextExtractorSettings:
        clean_provider = _required_str(provider_name, "provider_name")
        clean_command = _command_tuple(command)
        if enabled is True:
            if confirm_enable is not True:
                raise LocalDocumentTextExtractorSettingsError(
                    "enabling local document text extractor requires confirm_enable=true"
                )
            if not clean_command:
                raise LocalDocumentTextExtractorSettingsError(
                    "enabled local document text extractor requires command"
                )
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


class RunConfiguredLocalDocumentTextExtractorForSource:
    """Run document text extraction for a Source using stored extractor settings only."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-01T22:40:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now

    def execute(self, *, source_id: str) -> SourceContentReadResult:
        clean_source_id = _required_str(source_id, "source_id")
        settings = GetLocalDocumentTextExtractorSettings(self._object_store).execute()
        extractor = (
            BuiltinDocumentTextExtractor(provider_name=settings.provider_name)
            if settings.enabled and settings.command == (BUILTIN_DOCUMENT_TEXT_COMMAND,)
            else LocalCommandDocumentTextExtractor(
                command=settings.command,
                enabled=settings.enabled,
                provider_name=settings.provider_name,
            )
        )
        return ReadSourceTextContent(
            self._object_store,
            namespace_id=self._namespace_id,
            now=self._now,
            document_extractors=document_text_extractors_for_allowed_documents(extractor),
        ).execute(source_id=clean_source_id)


def serialize_local_document_text_extractor_settings(
    settings: LocalDocumentTextExtractorSettings,
) -> dict[str, object]:
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


def _settings_from_record(record: Mapping[str, object]) -> LocalDocumentTextExtractorSettings:
    enabled = record.get("enabled") is True
    command = _command_tuple(record.get("command"))
    provider_name = _optional_str(record.get("provider_name")) or "local-command-document-text"
    diagnostic = _diagnostic(enabled=enabled, command=command)
    if not enabled:
        status = "disabled"
    elif diagnostic == "ready":
        status = "ready"
    else:
        status = "degraded"
    return LocalDocumentTextExtractorSettings(
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
        "provider_name": "local-command-document-text",
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
    if command == (BUILTIN_DOCUMENT_TEXT_COMMAND,):
        return "ready"
    executable = command[0]
    if Path(executable).is_absolute():
        return "ready" if Path(executable).exists() else "executable_not_found"
    return "ready" if shutil.which(executable) is not None else "executable_not_found"


def _command_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise LocalDocumentTextExtractorSettingsError("local document text extractor command must be a list")
    command = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    if len(command) != len(value):
        raise LocalDocumentTextExtractorSettingsError(
            "local document text extractor command parts must be non-empty strings"
        )
    return command


def _required_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LocalDocumentTextExtractorSettingsError(f"{field_name} is required")
    return value.strip()


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
