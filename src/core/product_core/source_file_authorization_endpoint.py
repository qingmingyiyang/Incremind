from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from .source_file_authorization import (
    SourceFileAuthorizationError,
    SourceFileAuthorizationResult,
    serialize_source_file_authorization_result,
)


@dataclass(frozen=True, slots=True)
class SourceFileAuthorizationEndpointResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class ServeSourceFileAuthorizationEndpoint:
    """Serve local file authorization for an existing Source."""

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        authorize_file: Callable[..., SourceFileAuthorizationResult],
    ) -> SourceFileAuthorizationEndpointResponse:
        source_id = _source_id_from_path(path)
        if source_id is None:
            return self._json_response(404, {"detail": "source file authorization endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "source file authorization endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})
        file_path = body.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            return self._json_response(400, {"detail": "file_path must be a non-empty string"})
        try:
            result = authorize_file(source_id=source_id, file_path=file_path)
        except SourceFileAuthorizationError as error:
            status_code = 404 if str(error) == "source not found" else 400
            return self._json_response(
                status_code,
                {
                    "detail": "source file authorization rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(200, serialize_source_file_authorization_result(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> SourceFileAuthorizationEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return SourceFileAuthorizationEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )


def _source_id_from_path(path: str) -> str | None:
    parsed = urlsplit(path)
    prefix = "/api/rebuild/sources/"
    suffix = "/file-authorization"
    if not parsed.path.startswith(prefix) or not parsed.path.endswith(suffix):
        return None
    source_id = unquote(parsed.path[len(prefix) : -len(suffix)]).strip()
    return source_id or None
