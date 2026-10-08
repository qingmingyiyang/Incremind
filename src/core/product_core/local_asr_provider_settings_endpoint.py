from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from .local_asr_provider_settings import (
    LocalAsrProviderSettings,
    LocalAsrProviderSettingsError,
    serialize_local_asr_provider_settings,
)
from .media_processing_queue import (
    MediaProcessingQueueError,
    MediaProcessingQueueResult,
    serialize_media_processing_queue_result,
)


@dataclass(frozen=True, slots=True)
class LocalAsrProviderSettingsEndpointResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class ServeLocalAsrProviderSettingsEndpoint:
    """Serve the default-off local ASR Provider settings endpoint."""

    endpoint_path = "/api/rebuild/settings/local-asr-provider"

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        get_settings: Callable[[], LocalAsrProviderSettings],
        save_settings: Callable[..., LocalAsrProviderSettings],
    ) -> LocalAsrProviderSettingsEndpointResponse:
        if urlsplit(path).path != self.endpoint_path:
            return self._json_response(404, {"detail": "local ASR provider settings endpoint not found"})
        method_upper = method.upper()
        if method_upper == "GET":
            return self._json_response(200, serialize_local_asr_provider_settings(get_settings()))
        if method_upper != "PUT":
            return self._json_response(
                405,
                {"detail": "local ASR provider settings endpoint only supports GET and PUT"},
                extra_headers={"Allow": "GET, PUT"},
            )
        if not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})
        try:
            result = save_settings(
                enabled=_required_bool(body.get("enabled"), "enabled"),
                command=_required_command(body.get("command")),
                provider_name=_optional_str(body.get("provider_name")) or "local-command-asr",
                model_profile=_optional_str(body.get("model_profile")) or "large-v3-turbo",
                model_name=_optional_str(body.get("model_name")),
                timeout_seconds=body.get("timeout_seconds", 7200.0),
                confirm_enable=body.get("confirm_enable") is True,
            )
        except LocalAsrProviderSettingsError as error:
            return self._json_response(
                400,
                {
                    "detail": "local ASR provider settings rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(200, serialize_local_asr_provider_settings(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> LocalAsrProviderSettingsEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return LocalAsrProviderSettingsEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )


class ServeLocalAsrProviderRunEndpoint:
    """Serve Source transcription execution without accepting UI-supplied commands."""

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        run_asr: Callable[..., MediaProcessingQueueResult],
    ) -> LocalAsrProviderSettingsEndpointResponse:
        source_id = _source_id_from_transcription_path(path)
        if source_id is None:
            return self._json_response(404, {"detail": "local ASR run endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "local ASR run endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if body is not None and not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})
        if isinstance(body, Mapping) and "command" in body:
            return self._json_response(
                400,
                {
                    "detail": "local ASR run rejected",
                    "reason": "local ASR run endpoint does not accept provider command",
                    "actionable": True,
                },
            )
        try:
            result = run_asr(source_id=source_id)
        except (LocalAsrProviderSettingsError, MediaProcessingQueueError) as error:
            status_code = 404 if str(error) in {"source not found", "media processing job not found"} else 400
            return self._json_response(
                status_code,
                {
                    "detail": "local ASR run rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(200, serialize_media_processing_queue_result(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> LocalAsrProviderSettingsEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return LocalAsrProviderSettingsEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )


def _source_id_from_transcription_path(path: str) -> str | None:
    parsed = urlsplit(path)
    prefix = "/api/rebuild/sources/"
    suffix = "/transcription"
    if not parsed.path.startswith(prefix) or not parsed.path.endswith(suffix):
        return None
    source_id = unquote(parsed.path[len(prefix) : -len(suffix)]).strip()
    return source_id or None


def _required_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise LocalAsrProviderSettingsError(f"{field_name} must be boolean")
    return value


def _required_command(value: object) -> list[str]:
    if value is None:
        return ["builtin:faster-whisper"]
    if not isinstance(value, list):
        raise LocalAsrProviderSettingsError("command must be a list")
    command: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise LocalAsrProviderSettingsError("command must contain non-empty strings")
        command.append(item.strip())
    return command


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
