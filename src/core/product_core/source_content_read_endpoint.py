from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from .source_content_read import (
    SourceContentReadError,
    SourceContentReadResult,
    serialize_source_content_read_result,
)


@dataclass(frozen=True, slots=True)
class SourceContentReadEndpointResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class ServeSourceContentReadEndpoint:
    """Serve the narrow Source text content read endpoint."""

    def execute(
        self,
        *,
        method: str,
        path: str,
        read_content: Callable[..., SourceContentReadResult],
    ) -> SourceContentReadEndpointResponse:
        source_id = _source_id_from_path(path)
        if source_id is None:
            return self._json_response(404, {"detail": "source content read endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "source content read endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        try:
            result = read_content(source_id=source_id)
        except SourceContentReadError as error:
            status_code = 404 if str(error) == "source not found" else 400
            return self._json_response(
                status_code,
                {
                    "detail": "source content read rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(200, serialize_source_content_read_result(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> SourceContentReadEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return SourceContentReadEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )


def _source_id_from_path(path: str) -> str | None:
    parsed = urlsplit(path)
    prefix = "/api/rebuild/sources/"
    suffix = "/content-read"
    if not parsed.path.startswith(prefix) or not parsed.path.endswith(suffix):
        return None
    source_id = unquote(parsed.path[len(prefix) : -len(suffix)]).strip()
    return source_id or None
