from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .workbench_input_classifier import (
    WorkbenchInputClassificationResult,
    serialize_workbench_input_classification,
)


@dataclass(frozen=True, slots=True)
class WorkbenchInputClassifierEndpointResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class ServeWorkbenchInputClassifierEndpoint:
    """Serve the pre-intake workbench input classifier endpoint."""

    endpoint_path = "/api/rebuild/workbench/input-classifier"

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        classify_input: Callable[..., WorkbenchInputClassificationResult],
        enhance_input: Callable[..., WorkbenchInputClassificationResult] | None = None,
        classifier_prompt: Mapping[str, object] | None = None,
    ) -> WorkbenchInputClassifierEndpointResponse:
        request_path = path.split("?", 1)[0]
        if request_path != self.endpoint_path:
            return self._json_response(404, {"detail": "workbench input classifier endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "workbench input classifier endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})

        content = body.get("content", "")
        media_type = body.get("media_type", "")
        file_name = body.get("file_name", "")
        urls = body.get("urls", [])
        allow_provider_enhancement = body.get("allow_provider_enhancement") is True
        if not isinstance(content, str) or not isinstance(media_type, str) or not isinstance(file_name, str):
            return self._json_response(400, {"detail": "content, media_type and file_name must be strings"})
        if not isinstance(urls, list) or any(not isinstance(url, str) for url in urls):
            return self._json_response(400, {"detail": "urls must be a list of strings"})

        try:
            result = classify_input(
                content=content,
                media_type=media_type,
                file_name=file_name,
                urls=tuple(urls),
                classifier_prompt=classifier_prompt,
            )
            if allow_provider_enhancement and result.provider_enhancement_recommended and enhance_input is not None:
                try:
                    result = enhance_input(
                        local_result=result,
                        content=content,
                        media_type=media_type,
                        file_name=file_name,
                        urls=tuple(urls),
                        classifier_prompt=classifier_prompt,
                    )
                except ValueError:
                    pass
        except ValueError as error:
            return self._json_response(
                400,
                {
                    "detail": "workbench input classifier rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(200, serialize_workbench_input_classification(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> WorkbenchInputClassifierEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return WorkbenchInputClassifierEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )
