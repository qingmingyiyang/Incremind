from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .global_project_series_router import GlobalProjectRouteDecision
from .workbench_direct_question import (
    WorkbenchDirectQuestionError,
    WorkbenchDirectQuestionResult,
    serialize_workbench_direct_question,
)


@dataclass(frozen=True, slots=True)
class WorkbenchDirectQuestionEndpointResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class ServeWorkbenchDirectQuestionEndpoint:
    """Serve the homepage unchecked direct QA endpoint."""

    endpoint_path = "/api/rebuild/workbench/direct-question"

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        answer_question: Callable[..., WorkbenchDirectQuestionResult],
        resolve_project: Callable[[str], GlobalProjectRouteDecision] | None = None,
    ) -> WorkbenchDirectQuestionEndpointResponse:
        request_path = path.split("?", 1)[0]
        if request_path != self.endpoint_path:
            return self._json_response(404, {"detail": "workbench direct question endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "workbench direct question endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})
        question = body.get("question", "")
        if not isinstance(question, str):
            return self._json_response(400, {"detail": "question must be a string"})
        explicit_project = "project_id" in body
        project_route: GlobalProjectRouteDecision | None = None
        project_id = body.get("project_id", "default")
        if not isinstance(project_id, str) or not project_id.strip():
            return self._json_response(
                400,
                {"detail": "project_id must be a non-empty string"},
            )
        if not explicit_project and resolve_project is not None:
            try:
                decision = resolve_project(question)
            except ValueError as error:
                return self._json_response(
                    400,
                    {
                        "detail": "project routing rejected",
                        "reason": str(error),
                        "actionable": True,
                    },
                )
            project_route = decision
            if (
                decision.status == "routed"
                and decision.selected_project_id is not None
            ):
                project_id = decision.selected_project_id
            elif decision.status == "ambiguous":
                return self._json_response(
                    409,
                    {
                        "detail": "project scope is ambiguous",
                        "reason": decision.reason_code,
                        "actionable": True,
                        "candidates": [
                            candidate.to_payload()
                            for candidate in decision.candidates
                        ],
                        "project_route": decision.to_payload(),
                    },
                )
        try:
            result = answer_question(
                question=question,
                project_id=project_id.strip(),
            )
        except WorkbenchDirectQuestionError as error:
            return self._json_response(
                400,
                {
                    "detail": "workbench direct question rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        payload = dict(serialize_workbench_direct_question(result))
        payload["project_route"] = (
            project_route.to_payload()
            if project_route is not None
            else {
                "status": "explicit",
                "selected_project_id": project_id.strip(),
                "ai_assist": None,
            }
        )
        return self._json_response(200, payload)

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> WorkbenchDirectQuestionEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return WorkbenchDirectQuestionEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )
