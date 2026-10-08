from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .ports import ObjectStorePort


TOKENHUB_ASR_PROVIDER_ID = "tokenhub-asr"
TOKENHUB_ASR_PROVIDER_NAME = "tencent-tokenhub-hy-asr"
TOKENHUB_ASR_MODEL = "hy-asr-3.0-preview"
TOKENHUB_ASR_ENDPOINT = "https://tokenhub.tencentmaas.com/v1/wand/asrproxy/sync_transcribe"
TOKENHUB_ASR_SECRET_REF = "asr:tokenhub-hy-asr"
TOKENHUB_ASR_MAX_AUDIO_BYTES = 2 * 1024 * 1024
TOKENHUB_ASR_MAX_REQUEST_BYTES = 4 * 1024 * 1024


class CloudAsrProviderSettingsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CloudAsrProviderSettings:
    enabled: bool
    provider_id: str
    provider_name: str
    model: str
    endpoint: str
    max_audio_bytes: int
    max_request_bytes: int
    timeout_seconds: float
    remote_processing: bool
    settings_revision: int


class GetCloudAsrProviderSettings:
    _COLLECTION = "cloud_asr_provider_settings"
    _SETTINGS_ID = "tokenhub-hy-asr"

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._object_store = object_store

    def execute(self) -> CloudAsrProviderSettings:
        record = self._object_store.read(self._COLLECTION, self._SETTINGS_ID)
        revision = self._object_store.revision(self._COLLECTION, self._SETTINGS_ID) if record else 0
        return _settings(record or {}, revision=revision)


class SaveCloudAsrProviderSettings:
    _COLLECTION = "cloud_asr_provider_settings"
    _SETTINGS_ID = "tokenhub-hy-asr"

    def __init__(self, object_store: ObjectStorePort, *, now: str) -> None:
        self._object_store = object_store
        self._now = now

    def execute(
        self,
        *,
        enabled: bool,
        confirm_enable: bool,
        timeout_seconds: float = 180.0,
    ) -> CloudAsrProviderSettings:
        if enabled and confirm_enable is not True:
            raise CloudAsrProviderSettingsError("enabling cloud ASR requires confirm_enable=true")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError) as error:
            raise CloudAsrProviderSettingsError("timeout_seconds must be positive") from error
        if not 5 <= timeout <= 600:
            raise CloudAsrProviderSettingsError("timeout_seconds must be between 5 and 600")
        record = {
            "schema_version": "1.0.0",
            "id": self._SETTINGS_ID,
            "enabled": enabled is True,
            "provider_id": TOKENHUB_ASR_PROVIDER_ID,
            "provider_name": TOKENHUB_ASR_PROVIDER_NAME,
            "model": TOKENHUB_ASR_MODEL,
            "endpoint": TOKENHUB_ASR_ENDPOINT,
            "max_audio_bytes": TOKENHUB_ASR_MAX_AUDIO_BYTES,
            "max_request_bytes": TOKENHUB_ASR_MAX_REQUEST_BYTES,
            "timeout_seconds": timeout,
            "remote_processing": True,
            "updated_at": self._now,
        }
        self._object_store.write(self._COLLECTION, self._SETTINGS_ID, record, expected_revision=None)
        return GetCloudAsrProviderSettings(self._object_store).execute()


def _settings(record: Mapping[str, object], *, revision: int) -> CloudAsrProviderSettings:
    return CloudAsrProviderSettings(
        enabled=record.get("enabled") is True,
        provider_id=TOKENHUB_ASR_PROVIDER_ID,
        provider_name=TOKENHUB_ASR_PROVIDER_NAME,
        model=TOKENHUB_ASR_MODEL,
        endpoint=TOKENHUB_ASR_ENDPOINT,
        max_audio_bytes=TOKENHUB_ASR_MAX_AUDIO_BYTES,
        max_request_bytes=TOKENHUB_ASR_MAX_REQUEST_BYTES,
        timeout_seconds=float(record.get("timeout_seconds", 180.0)),
        remote_processing=True,
        settings_revision=revision,
    )
