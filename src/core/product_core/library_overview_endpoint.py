from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .library_overview import (
    LibraryOverview,
    LibraryOverviewError,
    serialize_library_overview,
)


@dataclass(frozen=True, slots=True)
class LibraryOverviewEndpointResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class ServeLibraryOverviewEndpoint:
    """Serve the narrow read-only Phase 12 Library Overview endpoint."""

    endpoint_path = "/api/rebuild/library/overview"

    def execute(
        self,
        *,
        method: str,
        path: str,
        get_library_overview: Callable[..., LibraryOverview],
    ) -> LibraryOverviewEndpointResponse:
        parsed = urlsplit(path)
        if parsed.path != self.endpoint_path:
            return self._json_response(404, {"detail": "library overview endpoint not found"})
        if method.upper() != "GET":
            return self._json_response(
                405,
                {"detail": "library overview endpoint only supports GET"},
                extra_headers={"Allow": "GET"},
            )
        query = parse_qs(parsed.query, keep_blank_values=True)
        project_error = _validate_query(query)
        if project_error is not None:
            return self._json_response(400, {"detail": project_error})
        project_values = query.get("project_id", [])
        project_id = project_values[0] if project_values else None
        try:
            overview = get_library_overview(project_id=project_id)
        except LibraryOverviewError as error:
            return self._json_response(
                400,
                {
                    "detail": "library overview request rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(200, serialize_library_overview(overview))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> LibraryOverviewEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return LibraryOverviewEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )


def _validate_query(query: Mapping[str, list[str]]) -> str | None:
    allowed = {"project_id"}
    unknown = sorted(key for key in query if key not in allowed)
    if unknown:
        return f"unsupported query parameter: {unknown[0]}"
    project_values = query.get("project_id")
    if project_values is None:
        return None
    if len(project_values) != 1:
        return "project_id must be provided at most once"
    if not project_values[0].strip():
        return "project_id cannot be empty"
    return None
