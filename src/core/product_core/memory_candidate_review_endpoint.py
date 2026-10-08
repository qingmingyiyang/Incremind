from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from .memory_candidate_review import (
    MemoryCandidateReviewError,
    MemoryCandidateReviewResult,
    serialize_memory_candidate_review_result,
)


@dataclass(frozen=True, slots=True)
class MemoryCandidateReviewEndpointResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class ServeMemoryCandidateReviewEndpoint:
    """Serve the narrow Memory Candidate review endpoint without publishing long-term memory."""

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, object] | None,
        get_candidate: Callable[[str], Mapping[str, object] | None],
        reject_candidate: Callable[..., MemoryCandidateReviewResult],
        promote_to_atom: Callable[..., MemoryCandidateReviewResult],
        withdraw_candidate: Callable[..., MemoryCandidateReviewResult] | None = None,
        promote_to_layer: Callable[..., MemoryCandidateReviewResult] | None = None,
        promote_to_project_skill: Callable[..., MemoryCandidateReviewResult] | None = None,
    ) -> MemoryCandidateReviewEndpointResponse:
        candidate_id = _candidate_id_from_path(path)
        if candidate_id is None:
            return self._json_response(404, {"detail": "memory candidate review endpoint not found"})
        method_name = method.upper()
        if method_name == "GET":
            candidate = get_candidate(candidate_id)
            if candidate is None:
                return self._json_response(
                    404,
                    {
                        "detail": "memory candidate not found",
                        "reason": f"Memory Candidate not found: {candidate_id}",
                        "actionable": False,
                    },
                )
            return self._json_response(200, _candidate_review_detail(candidate))
        if method_name != "POST":
            return self._json_response(
                405,
                {"detail": "memory candidate review endpoint only supports GET and POST"},
                extra_headers={"Allow": "GET, POST"},
            )
        try:
            action = _required_body_str(body, "action")
            reason = _required_body_str(body, "reason")
            if action == "reject":
                result = reject_candidate(candidate_id, reason=reason, reviewed_by="user")
            elif action == "withdraw":
                if withdraw_candidate is None:
                    raise MemoryCandidateReviewError("review action withdraw is not available")
                result = withdraw_candidate(candidate_id, reason=reason, reviewed_by="user")
            elif action == "promote_to_atom":
                result = promote_to_atom(
                    candidate_id,
                    reason=reason,
                    reviewed_by="user",
                    atom_type=_optional_body_str(body, "atom_type"),
                    tags=_body_tags(body),
                    confidence=_body_confidence(body),
                )
            elif action in {"promote_to_scenario", "promote_to_series_memory", "promote_to_project_skill"}:
                if action == "promote_to_project_skill" and promote_to_project_skill is not None:
                    result = promote_to_project_skill(candidate_id, reason=reason, reviewed_by="user")
                else:
                    if promote_to_layer is None:
                        raise MemoryCandidateReviewError(f"review action {action} is not available")
                    result = promote_to_layer(
                        candidate_id,
                        target_layer=_target_layer_for_action(action),
                        reason=reason,
                        reviewed_by="user",
                        tags=_body_tags(body),
                        series_id=_optional_body_str(body, "series_id"),
                        scenario_ids=_body_string_list(body, "scenario_ids"),
                        atom_ids=_body_string_list(body, "atom_ids"),
                    )
            else:
                raise MemoryCandidateReviewError(
                    "review action must be reject, promote_to_atom, promote_to_scenario, "
                    "promote_to_series_memory, promote_to_project_skill, or withdraw"
                )
        except MemoryCandidateReviewError as error:
            status_code = 404 if str(error).startswith("Memory Candidate not found") else 400
            return self._json_response(
                status_code,
                {
                    "detail": "memory candidate review rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(200, serialize_memory_candidate_review_result(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> MemoryCandidateReviewEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return MemoryCandidateReviewEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )


def _candidate_id_from_path(path: str) -> str | None:
    parsed = urlsplit(path)
    prefix = "/api/rebuild/memory-candidates/"
    suffix = "/review"
    if not parsed.path.startswith(prefix) or not parsed.path.endswith(suffix):
        return None
    candidate_id = unquote(parsed.path[len(prefix) : -len(suffix)]).strip()
    return candidate_id or None


def _candidate_review_detail(candidate: Mapping[str, object]) -> dict[str, object]:
    review = candidate.get("review")
    review_payload = dict(review) if isinstance(review, Mapping) else {}
    source_refs = candidate.get("source_refs")
    provenance = candidate.get("provenance")
    input_refs = provenance.get("input_refs") if isinstance(provenance, Mapping) else ()
    return {
        "candidate_id": _required_candidate_str(candidate, "id"),
        "candidate_status": _required_candidate_str(candidate, "status"),
        "project_id": _required_candidate_str(candidate, "project_id"),
        "target_layer": _required_candidate_str(candidate, "target_layer"),
        "candidate_type": _required_candidate_str(candidate, "candidate_type"),
        "proposed_content": _required_candidate_str(candidate, "proposed_content"),
        "source_refs": _refs(source_refs),
        "input_refs": _refs(input_refs),
        "review": review_payload,
        "available_actions": _available_actions(candidate, review_payload),
        "memory_publication_state": "not_published",
    }


def _available_actions(candidate: Mapping[str, object], review_payload: Mapping[str, object]) -> list[str]:
    if (
        candidate.get("status") != "pending_review"
        or review_payload.get("requires_user_confirmation") is not True
        or review_payload.get("auto_promote_allowed") is not False
    ):
        return []
    target_layer = candidate.get("target_layer")
    promote_action = {
        "atom": "promote_to_atom",
        "scenario": "promote_to_scenario",
        "series_memory": "promote_to_series_memory",
        "project_skill": "promote_to_project_skill",
    }.get(target_layer)
    if promote_action is None:
        return ["reject", "withdraw"]
    return ["reject", promote_action, "withdraw"]


def _refs(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _required_body_str(body: Mapping[str, object] | None, key: str) -> str:
    if body is None:
        raise MemoryCandidateReviewError("review body is required")
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MemoryCandidateReviewError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_body_str(body: Mapping[str, object] | None, key: str) -> str | None:
    if body is None:
        return None
    value = body.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _body_tags(body: Mapping[str, object] | None) -> tuple[str, ...]:
    if body is None:
        return ()
    value = body.get("tags")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MemoryCandidateReviewError("tags must be a list")
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise MemoryCandidateReviewError("tags must contain non-empty strings")
        tags.append(item.strip())
    return tuple(tags)


def _body_string_list(body: Mapping[str, object] | None, key: str) -> tuple[str, ...]:
    if body is None:
        return ()
    value = body.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MemoryCandidateReviewError(f"{key} must be a list")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise MemoryCandidateReviewError(f"{key} must contain non-empty strings")
        items.append(item.strip())
    return tuple(items)


def _body_confidence(body: Mapping[str, object] | None) -> float:
    if body is None or body.get("confidence") is None:
        return 0.7
    value = body.get("confidence")
    if not isinstance(value, (float, int)) or isinstance(value, bool):
        raise MemoryCandidateReviewError("confidence must be a number")
    return float(value)


def _target_layer_for_action(action: str) -> str:
    return {
        "promote_to_scenario": "scenario",
        "promote_to_series_memory": "series_memory",
        "promote_to_project_skill": "project_skill",
    }[action]


def _required_candidate_str(candidate: Mapping[str, object], key: str) -> str:
    value = candidate.get(key)
    if not isinstance(value, str) or not value:
        raise MemoryCandidateReviewError(f"{key} is required")
    return value
