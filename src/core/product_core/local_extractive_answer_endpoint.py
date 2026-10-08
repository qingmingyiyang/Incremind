from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from .local_extractive_answer import (
    LocalExtractiveAnswerError,
    LocalExtractiveAnswerResult,
    serialize_local_extractive_answer_result,
)


@dataclass(frozen=True, slots=True)
class LocalExtractiveAnswerEndpointResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class ServeLocalExtractiveAnswerEndpoint:
    """Serve local answer generation from an existing answer Model Request."""

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        create_answer: Callable[..., LocalExtractiveAnswerResult],
    ) -> LocalExtractiveAnswerEndpointResponse:
        model_request_id = _model_request_id_from_path(path)
        if model_request_id is None:
            return self._json_response(404, {"detail": "local answer endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "local answer endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if body is not None and not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})
        try:
            result = create_answer(model_request_id)
        except LocalExtractiveAnswerError as error:
            status_code = 404 if str(error).endswith("not found") else 400
            return self._json_response(
                status_code,
                {
                    "detail": "local extractive answer rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(200, serialize_local_extractive_answer_result(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> LocalExtractiveAnswerEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return LocalExtractiveAnswerEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )


def _model_request_id_from_path(path: str) -> str | None:
    parsed = urlsplit(path)
    prefix = "/api/rebuild/model-requests/"
    suffix = "/local-answer"
    if not parsed.path.startswith(prefix) or not parsed.path.endswith(suffix):
        return None
    model_request_id = unquote(parsed.path[len(prefix) : -len(suffix)]).strip()
    return model_request_id or None
