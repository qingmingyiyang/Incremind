from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from .source_content_qa_recall import (
    SourceContentQaRecallError,
    SourceContentQaRecallResult,
    serialize_source_content_qa_recall,
)


@dataclass(frozen=True, slots=True)
class SourceContentQaRecallEndpointResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class ServeSourceContentQaRecallEndpoint:
    """Serve Source content_read -> local QA recall request creation."""

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        create_recall: Callable[..., SourceContentQaRecallResult],
    ) -> SourceContentQaRecallEndpointResponse:
        source_id = _source_id_from_path(path)
        if source_id is None:
            return self._json_response(404, {"detail": "source content QA endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "source content QA endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})
        question = _optional_str(body.get("question"))
        if question is None:
            return self._json_response(400, {"detail": "question is required"})
        try:
            result = create_recall(
                source_id,
                question=question,
                project_id=_optional_str(body.get("project_id")) or "default",
                project_skill_id=_optional_str(body.get("project_skill_id")) or "skill-default",
                content_read_id=_optional_str(body.get("content_read_id")),
            )
        except SourceContentQaRecallError as error:
            status_code = 404 if str(error).endswith("not found") else 400
            return self._json_response(
                status_code,
                {
                    "detail": "source content QA recall rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(200, serialize_source_content_qa_recall(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> SourceContentQaRecallEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return SourceContentQaRecallEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )


def _source_id_from_path(path: str) -> str | None:
    parsed = urlsplit(path)
    prefix = "/api/rebuild/sources/"
    suffix = "/qa-recall"
    if not parsed.path.startswith(prefix) or not parsed.path.endswith(suffix):
        return None
    source_id = unquote(parsed.path[len(prefix) : -len(suffix)]).strip()
    return source_id or None


def _optional_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean or None
