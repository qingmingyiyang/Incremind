from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from .media_processing_queue import (
    MediaProcessingQueueError,
    MediaProcessingQueueResult,
    serialize_media_processing_queue_result,
)


@dataclass(frozen=True, slots=True)
class MediaProcessingQueueEndpointResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class ServeMediaProcessingQueueEndpoint:
    """Serve the narrow Source media processing queue endpoint."""

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, object] | None,
        queue_media_processing: Callable[..., MediaProcessingQueueResult],
    ) -> MediaProcessingQueueEndpointResponse:
        source_id = _source_id_from_path(path)
        if source_id is None:
            return self._json_response(404, {"detail": "media processing queue endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "media processing queue endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        try:
            enabled_capabilities = _enabled_capabilities(body)
            result = queue_media_processing(
                source_id=source_id,
                enabled_capabilities=enabled_capabilities,
            )
        except MediaProcessingQueueError as error:
            status_code = 404 if str(error) in {"source not found"} else 400
            return self._json_response(
                status_code,
                {
                    "detail": "media processing queue rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(201, serialize_media_processing_queue_result(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> MediaProcessingQueueEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return MediaProcessingQueueEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )


def _source_id_from_path(path: str) -> str | None:
    parsed = urlsplit(path)
    prefix = "/api/rebuild/sources/"
    suffix = "/media-processing"
    if not parsed.path.startswith(prefix) or not parsed.path.endswith(suffix):
        return None
    source_id = unquote(parsed.path[len(prefix) : -len(suffix)]).strip()
    return source_id or None


def _enabled_capabilities(body: Mapping[str, object] | None) -> tuple[str, ...]:
    if body is None:
        return ()
    value = body.get("enabled_capabilities")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MediaProcessingQueueError("enabled_capabilities must be a list")
    capabilities: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise MediaProcessingQueueError("enabled_capabilities must contain non-empty strings")
        capabilities.append(item.strip())
    return tuple(capabilities)
