from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from .ports import ObjectStorePort


QWEN_REALTIME_ASR_PROVIDER_ID = "qwen-realtime-asr"
QWEN_REALTIME_ASR_PROVIDER_NAME = "aliyun-qwen-realtime-asr"
QWEN_REALTIME_ASR_MODEL = "qwen-audio-3.0-asr-flash-streaming"
QWEN_REALTIME_ASR_DEFAULT_REGION = "cn-beijing"
QWEN_REALTIME_ASR_REGIONS = {
    "cn-beijing": ".cn-beijing.maas.aliyuncs.com",
    "ap-southeast-1": ".ap-southeast-1.maas.aliyuncs.com",
}
# No account workspace is a product default.  This public endpoint is valid for
# the provider, while workspace-specific execution requires saved configuration.
QWEN_REALTIME_ASR_ENDPOINT = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
QWEN_REALTIME_ASR_SECRET_REF = "asr:qwen-realtime"
QWEN_REALTIME_ASR_SAMPLE_RATE = 16_000
QWEN_REALTIME_ASR_MAX_SESSION_SECONDS = 300
QWEN_REALTIME_ASR_MAX_AUDIO_BYTES = (
    QWEN_REALTIME_ASR_SAMPLE_RATE * 2 * QWEN_REALTIME_ASR_MAX_SESSION_SECONDS
)


class RealtimeAsrProviderSettingsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RealtimeAsrProviderSettings:
    enabled: bool
    provider_id: str
    provider_name: str
    model: str
    endpoint: str
    region: str | None
    workspace_id: str | None
    sample_rate: int
    max_session_seconds: int
    max_audio_bytes: int
    remote_processing: bool
    settings_revision: int


class GetRealtimeAsrProviderSettings:
    _COLLECTION = "realtime_asr_provider_settings"
    _SETTINGS_ID = "qwen-realtime"

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._object_store = object_store

    def execute(self) -> RealtimeAsrProviderSettings:
        record = self._object_store.read(self._COLLECTION, self._SETTINGS_ID)
        revision = self._object_store.revision(self._COLLECTION, self._SETTINGS_ID) if record else 0
        return _settings(record or {}, revision=revision)


class SaveRealtimeAsrProviderSettings:
    _COLLECTION = "realtime_asr_provider_settings"
    _SETTINGS_ID = "qwen-realtime"

    def __init__(self, object_store: ObjectStorePort, *, now: str) -> None:
        self._object_store = object_store
        self._now = now

    def execute(
        self,
        *,
        enabled: bool,
        confirm_enable: bool,
        endpoint: str = QWEN_REALTIME_ASR_ENDPOINT,
        region: str | None = None,
        workspace_id: str | None = None,
    ) -> RealtimeAsrProviderSettings:
        if enabled and confirm_enable is not True:
            raise RealtimeAsrProviderSettingsError(
                "enabling realtime ASR requires confirm_enable=true"
            )
        normalized_endpoint, normalized_region, normalized_workspace = _connection(
            endpoint=endpoint, region=region, workspace_id=workspace_id,
        )
        record = {
            "schema_version": "1.0.0",
            "id": self._SETTINGS_ID,
            "enabled": enabled is True,
            "provider_id": QWEN_REALTIME_ASR_PROVIDER_ID,
            "provider_name": QWEN_REALTIME_ASR_PROVIDER_NAME,
            "model": QWEN_REALTIME_ASR_MODEL,
            "endpoint": normalized_endpoint,
            "region": normalized_region,
            "workspace_id": normalized_workspace,
            "sample_rate": QWEN_REALTIME_ASR_SAMPLE_RATE,
            "max_session_seconds": QWEN_REALTIME_ASR_MAX_SESSION_SECONDS,
            "max_audio_bytes": QWEN_REALTIME_ASR_MAX_AUDIO_BYTES,
            "remote_processing": True,
            "updated_at": self._now,
        }
        self._object_store.write(
            self._COLLECTION,
            self._SETTINGS_ID,
            record,
            expected_revision=None,
        )
        return GetRealtimeAsrProviderSettings(self._object_store).execute()


def _settings(
    record: Mapping[str, object], *, revision: int
) -> RealtimeAsrProviderSettings:
    endpoint, region, workspace_id = _connection(
        endpoint=str(record.get("endpoint") or QWEN_REALTIME_ASR_ENDPOINT),
        region=record.get("region") if isinstance(record.get("region"), str) else None,
        workspace_id=record.get("workspace_id") if isinstance(record.get("workspace_id"), str) else None,
    )
    return RealtimeAsrProviderSettings(
        enabled=record.get("enabled") is True,
        provider_id=QWEN_REALTIME_ASR_PROVIDER_ID,
        provider_name=QWEN_REALTIME_ASR_PROVIDER_NAME,
        model=QWEN_REALTIME_ASR_MODEL,
        endpoint=endpoint,
        region=region,
        workspace_id=workspace_id,
        sample_rate=QWEN_REALTIME_ASR_SAMPLE_RATE,
        max_session_seconds=QWEN_REALTIME_ASR_MAX_SESSION_SECONDS,
        max_audio_bytes=QWEN_REALTIME_ASR_MAX_AUDIO_BYTES,
        remote_processing=True,
        settings_revision=revision,
    )


def _connection(*, endpoint: object, region: str | None, workspace_id: str | None) -> tuple[str, str | None, str | None]:
    if region is not None or workspace_id is not None:
        if region not in QWEN_REALTIME_ASR_REGIONS or not _workspace_id(workspace_id):
            raise RealtimeAsrProviderSettingsError("realtime ASR workspace configuration is invalid")
        return (
            f"wss://{workspace_id}{QWEN_REALTIME_ASR_REGIONS[region]}/api-ws/v1/inference",
            region,
            workspace_id,
        )
    normalized = _endpoint(endpoint)
    parsed = urlsplit(normalized)
    if parsed.hostname == "dashscope.aliyuncs.com":
        return normalized, None, None
    for known_region, suffix in QWEN_REALTIME_ASR_REGIONS.items():
        if parsed.hostname and parsed.hostname.endswith(suffix):
            workspace = parsed.hostname[: -len(suffix)]
            if _workspace_id(workspace):
                return normalized, known_region, workspace
    raise RealtimeAsrProviderSettingsError("realtime ASR endpoint is invalid")


def _endpoint(value: object) -> str:
    if not isinstance(value, str):
        raise RealtimeAsrProviderSettingsError("realtime ASR endpoint is invalid")
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "wss"
        or parsed.path != "/api-ws/v1/inference"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or not parsed.hostname
        or not (parsed.hostname == "dashscope.aliyuncs.com" or any(parsed.hostname.endswith(suffix) for suffix in QWEN_REALTIME_ASR_REGIONS.values()))
    ):
        raise RealtimeAsrProviderSettingsError("realtime ASR endpoint is invalid")
    return normalized


def _workspace_id(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 63 and value.replace("-", "").replace("_", "").isalnum()


__all__ = (
    "QWEN_REALTIME_ASR_ENDPOINT",
    "QWEN_REALTIME_ASR_DEFAULT_REGION",
    "QWEN_REALTIME_ASR_REGIONS",
    "QWEN_REALTIME_ASR_MAX_AUDIO_BYTES",
    "QWEN_REALTIME_ASR_MAX_SESSION_SECONDS",
    "QWEN_REALTIME_ASR_MODEL",
    "QWEN_REALTIME_ASR_PROVIDER_ID",
    "QWEN_REALTIME_ASR_PROVIDER_NAME",
    "QWEN_REALTIME_ASR_SAMPLE_RATE",
    "QWEN_REALTIME_ASR_SECRET_REF",
    "GetRealtimeAsrProviderSettings",
    "RealtimeAsrProviderSettings",
    "RealtimeAsrProviderSettingsError",
    "SaveRealtimeAsrProviderSettings",
)
