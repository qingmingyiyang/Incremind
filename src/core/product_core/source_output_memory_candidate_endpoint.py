from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from .source_output_memory_candidate import (
    SourceOutputMemoryCandidateError,
    SourceOutputMemoryCandidateResult,
    serialize_source_output_memory_candidate,
)


@dataclass(frozen=True, slots=True)
class SourceOutputMemoryCandidateEndpointResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class ServeSourceOutputMemoryCandidateEndpoint:
    """Serve manual Source output -> Memory Candidate creation without publication."""

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        create_from_content_read: Callable[..., SourceOutputMemoryCandidateResult],
        create_from_media_output: Callable[..., SourceOutputMemoryCandidateResult],
    ) -> SourceOutputMemoryCandidateEndpointResponse:
        source_id = _source_id_from_path(path)
        if source_id is None:
            return self._json_response(404, {"detail": "source output memory candidate endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "source output memory candidate endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})
        evidence_kind = _optional_str(body.get("evidence_kind")) or "source_content_read"
        project_id = _optional_str(body.get("project_id")) or "default"
        proposed_content = _optional_str(body.get("proposed_content"))
        target_layer = _optional_str(body.get("target_layer")) or "atom"
        candidate_type = _optional_str(body.get("candidate_type")) or "other"
        evidence_id = _optional_str(body.get("evidence_id"))
        try:
            if evidence_kind == "source_content_read":
                result = create_from_content_read(
                    source_id=source_id,
                    project_id=project_id,
                    content_read_id=evidence_id,
                    proposed_content=proposed_content,
                    target_layer=target_layer,
                    candidate_type=candidate_type,
                )
            elif evidence_kind == "media_processing_output":
                if evidence_id is None:
                    raise SourceOutputMemoryCandidateError("evidence_id is required for media processing output")
                result = create_from_media_output(
                    output_id=evidence_id,
                    project_id=project_id,
                    proposed_content=proposed_content,
                    target_layer=target_layer,
                    candidate_type=candidate_type,
                )
            else:
                raise SourceOutputMemoryCandidateError(
                    "evidence_kind must be source_content_read or media_processing_output"
                )
        except SourceOutputMemoryCandidateError as error:
            status_code = 404 if str(error).endswith("not found") else 400
            return self._json_response(
                status_code,
                {
                    "detail": "source output memory candidate rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(200, serialize_source_output_memory_candidate(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> SourceOutputMemoryCandidateEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return SourceOutputMemoryCandidateEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )


def _source_id_from_path(path: str) -> str | None:
    parsed = urlsplit(path)
    prefix = "/api/rebuild/sources/"
    suffix = "/memory-candidate"
    if not parsed.path.startswith(prefix) or not parsed.path.endswith(suffix):
        return None
    source_id = unquote(parsed.path[len(prefix) : -len(suffix)]).strip()
    return source_id or None


def _optional_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean or None
