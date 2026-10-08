from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .workbench_document_draft import (
    WorkbenchDocumentDraftError,
    WorkbenchDocumentDraftResult,
    WorkbenchDocumentDraftSelection,
    serialize_workbench_document_draft,
)


@dataclass(frozen=True, slots=True)
class WorkbenchDocumentDraftEndpointResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class ServeWorkbenchDocumentDraftEndpoint:
    """Serve the narrow Phase 11 Workbench selection to Document draft endpoint."""

    endpoint_path = "/api/rebuild/workbench/document-draft"

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        create_document_draft: Callable[..., WorkbenchDocumentDraftResult],
    ) -> WorkbenchDocumentDraftEndpointResponse:
        request_path = path.split("?", 1)[0]
        if request_path != self.endpoint_path:
            return self._json_response(404, {"detail": "workbench Document draft endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "workbench Document draft endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})

        selection_error = _validate_request_body(body)
        if selection_error is not None:
            return self._json_response(400, {"detail": selection_error})

        selection = WorkbenchDocumentDraftSelection(
            selection_id=str(body["selection_id"]),
            source_id=str(body["source_id"]),
            source_title=str(body["source_title"]),
            source_uri=str(body["source_uri"]),
            capture_job_id=str(body["capture_job_id"]),
            media_type=str(body["media_type"]),
            selected_evidence_refs=tuple(str(ref) for ref in body["selected_evidence_refs"]),
            project_id=str(body["project_id"]) if isinstance(body.get("project_id"), str) else None,
        )
        title = body.get("title")
        summary = body.get("summary")
        try:
            result = create_document_draft(
                selection,
                title=title if isinstance(title, str) else None,
                summary=summary if isinstance(summary, str) else None,
            )
        except WorkbenchDocumentDraftError as error:
            return self._json_response(
                400,
                {
                    "detail": "workbench Document draft handoff rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(201, serialize_workbench_document_draft(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> WorkbenchDocumentDraftEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return WorkbenchDocumentDraftEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )


def _validate_request_body(body: Mapping[str, Any]) -> str | None:
    string_fields = (
        "selection_id",
        "source_id",
        "source_title",
        "source_uri",
        "capture_job_id",
        "media_type",
    )
    for field in string_fields:
        value = body.get(field)
        if not isinstance(value, str):
            return (
                "selection_id, source_id, source_title, source_uri, capture_job_id "
                "and media_type must be strings"
            )
    evidence_refs = body.get("selected_evidence_refs")
    if not _is_string_sequence(evidence_refs):
        return "selected_evidence_refs must be a non-empty string array"
    if body.get("project_id") is not None and not isinstance(body.get("project_id"), str):
        return "project_id must be a string when provided"
    if body.get("title") is not None and not isinstance(body.get("title"), str):
        return "title must be a string when provided"
    if body.get("summary") is not None and not isinstance(body.get("summary"), str):
        return "summary must be a string when provided"
    return None


def _is_string_sequence(value: object) -> bool:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return False
    if not value:
        return False
    return all(isinstance(item, str) for item in value)
