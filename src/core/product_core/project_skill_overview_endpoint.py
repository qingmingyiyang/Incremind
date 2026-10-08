from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .project_skill_overview import (
    ProjectSkillOverview,
    ProjectSkillOverviewError,
    serialize_project_skill_overview,
)


@dataclass(frozen=True, slots=True)
class ProjectSkillOverviewEndpointResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class ServeProjectSkillOverviewEndpoint:
    """Serve the narrow read-only Phase 13 Project Skill overview endpoint."""

    endpoint_path = "/api/rebuild/project-skill/overview"

    def execute(
        self,
        *,
        method: str,
        path: str,
        get_project_skill_overview: Callable[[str], ProjectSkillOverview],
    ) -> ProjectSkillOverviewEndpointResponse:
        parsed = urlsplit(path)
        if parsed.path != self.endpoint_path:
            return self._json_response(404, {"detail": "project skill overview endpoint not found"})
        if method.upper() != "GET":
            return self._json_response(
                405,
                {"detail": "project skill overview endpoint only supports GET"},
                extra_headers={"Allow": "GET"},
            )
        query = parse_qs(parsed.query, keep_blank_values=True)
        query_error = _validate_query(query)
        if query_error is not None:
            return self._json_response(400, {"detail": query_error})
        project_id = query["project_id"][0]
        try:
            overview = get_project_skill_overview(project_id)
        except ProjectSkillOverviewError as error:
            return self._json_response(
                400,
                {
                    "detail": "project skill overview request rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        payload = serialize_project_skill_overview(overview)
        payload["endpoint_boundary"] = {
            "read_only": True,
            "display_ready": True,
            "allows_ai_rewrite": False,
            "allows_mutation": False,
            "allows_memory_publication": False,
        }
        return self._json_response(200, payload)

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> ProjectSkillOverviewEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return ProjectSkillOverviewEndpointResponse(
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
        return "project_id is required"
    if len(project_values) != 1:
        return "project_id must be provided exactly once"
    if not project_values[0].strip():
        return "project_id cannot be empty"
    return None
