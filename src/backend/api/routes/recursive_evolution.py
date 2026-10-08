"""Local-only HTTP boundary for governed recursive evolution.

The route deliberately accepts immutable contract payloads but only returns a
small lifecycle projection.  Candidate artifacts, prompts, evidence, Receipts,
providers, model details, paths and revisions never cross this boundary in a
response.
"""

from __future__ import annotations

from backend.security.device_identity import server_mode, server_authorized

import ipaddress
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.recursive_evolution_runtime import (
    RecursiveEvolutionRuntimeError,
)
from core.recursive_evolution import (
    EvolutionContractError,
    EvolutionEpisode,
    EvolutionProposal,
    EvolutionReview,
)


router = APIRouter(
    prefix="/api/rebuild/projects/{project_id}/recursive-evolution",
    tags=["recursive-evolution"],
)

_NO_STORE = {"Cache-Control": "no-store"}
_EPISODE_FIELDS = {"command_id", "episode_id", "target_kind", "policy"}
_PROPOSAL_FIELDS = {"command_id", *EvolutionProposal.__dataclass_fields__}
_REVIEW_FIELDS = {"command_id", *EvolutionReview.__dataclass_fields__}
_HUMAN_FIELDS = {"command_id", "user_id", "confirmation_ref", "evidence_refs"}
_OBSERVATION_FIELDS = {"command_id", "evidence_ref"}
_LOCAL_ACTION_FIELDS = {"command_id"}


def _response(status: int, body: Mapping[str, object]) -> JSONResponse:
    return JSONResponse(status_code=status, content=dict(body), headers=_NO_STORE)


def _is_local(request: Request) -> bool:
    if server_mode(request):
        return server_authorized(request)
    host = request.client.host if request.client is not None else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _runtime(request: Request) -> object | None:
    runtime = getattr(request.app.state, "recursive_evolution_runtime", None)
    if runtime is not None:
        return runtime
    # The evolution authority shares the application AI runtime.  Routes may
    # be the first consumer during desktop startup, so resolve the existing
    # lazy composition instead of treating an uninitialised state slot as an
    # unavailable authority.
    container = getattr(request.app.state, "container", None)
    if container is None:
        return None
    try:
        from backend.api.ai_runtime import get_or_build_ai_runtime

        get_or_build_ai_runtime(request, container)
    except Exception:
        return None
    return getattr(request.app.state, "recursive_evolution_runtime", None)


async def _body(request: Request, fields: set[str]) -> dict[str, object]:
    try:
        body: Any = await request.json()
    except Exception as error:
        raise EvolutionContractError("request body is invalid") from error
    if not isinstance(body, dict) or set(body) != fields:
        raise EvolutionContractError("request body is invalid")
    return body


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvolutionContractError(f"{label} is invalid")
    return value


def _refs(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise EvolutionContractError("evidence refs are invalid")
    if not isinstance(value, Sequence):
        raise EvolutionContractError("evidence refs are invalid")
    return tuple(value)  # domain/runtime performs the final reference validation


def _summary(value: object, *, replayed: bool | None = None) -> dict[str, object]:
    """Allowlist the only fields safe to render in Settings and Task Detail."""

    projection = getattr(value, "projection", value)
    statuses = getattr(projection, "proposal_statuses", {})
    if not isinstance(statuses, Mapping):
        raise RecursiveEvolutionRuntimeError("evolution summary is invalid")
    target_kind = getattr(projection, "target_kind")
    safe_target_kind = (
        target_kind if isinstance(target_kind, str) else str(target_kind.value)
    )
    policy = getattr(projection, "policy", None)
    limits, governance = _safe_policy(policy)
    summary: dict[str, object] = {
        "episode_id": getattr(projection, "episode_id"),
        "project_id": getattr(projection, "project_id"),
        "target_kind": safe_target_kind,
        "current_generation": getattr(projection, "current_generation"),
        "candidate_count": getattr(projection, "candidate_count"),
        "evaluation_count": getattr(projection, "evaluation_count"),
        "budget_used": getattr(projection, "budget_used"),
        "status": "stopped" if getattr(projection, "is_terminal") else "active",
        "stop_reason": getattr(projection, "stop_reason"),
        "proposal_statuses": dict(statuses),
        "available_actions": {
            proposal_id: _available_actions(status)
            for proposal_id, status in statuses.items()
            if isinstance(proposal_id, str) and isinstance(status, str)
        } | ({"episode": ["stop"]} if not getattr(projection, "is_terminal") else {}),
        "policy_limits": limits,
        "governance_flags": governance,
    }
    if replayed is not None:
        summary["replayed"] = replayed
    return summary


def _safe_policy(policy: object) -> tuple[dict[str, int | float], dict[str, bool]]:
    """Project only UI-safe policy ceilings, never deadlines or target material."""

    numeric = {
        "max_generations": getattr(policy, "max_generations", None),
        "max_candidates_per_generation": getattr(policy, "max_candidates_per_generation", None),
        "max_evaluations_per_candidate": getattr(policy, "max_evaluations_per_candidate", None),
        "total_budget_units": getattr(policy, "total_budget_units", None),
        "max_no_improvement_cycles": getattr(policy, "max_no_improvement_cycles", None),
        "minimum_improvement": getattr(policy, "minimum_improvement", None),
        "minimum_canary_samples": getattr(policy, "minimum_canary_samples", None),
    }
    flags = {
        "requires_user_confirmation": getattr(policy, "requires_user_confirmation", None),
        "auto_promote_allowed": getattr(policy, "auto_promote_allowed", None),
        "canary_required": getattr(policy, "canary_required", None),
    }
    if (
        any(type(value) not in {int, float} for value in numeric.values())
        or any(type(value) is not bool for value in flags.values())
    ):
        raise RecursiveEvolutionRuntimeError("evolution policy summary is invalid")
    return dict(numeric), dict(flags)


def _available_actions(status: str) -> list[str]:
    """Mirror the server lifecycle gate without exposing action evidence."""

    return {
        "reviewed_qualified": ["start-canary", "reject"],
        "canary_passed": ["promote"],
        "canary_failed": ["rollback"],
        "promoted": ["rollback"],
    }.get(status, [])


def _result(result: object) -> JSONResponse:
    replayed = getattr(result, "replayed", None)
    if not isinstance(replayed, bool):
        raise RecursiveEvolutionRuntimeError("evolution command result is invalid")
    return _response(200, _summary(result, replayed=replayed))


def _error(error: Exception) -> JSONResponse:
    if isinstance(error, EvolutionContractError):
        return _response(400, {"detail": "recursive evolution contract rejected"})
    if isinstance(error, RecursiveEvolutionRuntimeError):
        return _response(409, {"detail": "recursive evolution command conflicted"})
    return _response(503, {"detail": "recursive evolution authority is unavailable"})


def _available(request: Request, method: str) -> tuple[object | None, JSONResponse | None]:
    if not _is_local(request):
        return None, _response(403, {"detail": "recursive evolution is local-only"})
    runtime = _runtime(request)
    if not callable(getattr(runtime, method, None)):
        return None, _response(503, {"detail": "recursive evolution authority is unavailable"})
    return runtime, None


def _local_actions(request: Request) -> object | None:
    _runtime(request)
    return getattr(request.app.state, "recursive_evolution_local_actions", None)


@router.get("/episodes")
async def list_episodes(project_id: str, request: Request) -> JSONResponse:
    runtime, failure = _available(request, "list_projections")
    if failure is not None:
        return failure
    try:
        values = runtime.list_projections(project_id=project_id)  # type: ignore[union-attr]
        return _response(200, {"episodes": [_summary(value) for value in values]})
    except (EvolutionContractError, RecursiveEvolutionRuntimeError) as error:
        return _error(error)
    except Exception as error:
        return _error(error)


@router.get("/episodes/{episode_id}")
async def get_episode(project_id: str, episode_id: str, request: Request) -> JSONResponse:
    runtime, failure = _available(request, "get_projection")
    if failure is not None:
        return failure
    try:
        projection = runtime.get_projection(project_id=project_id, episode_id=episode_id)  # type: ignore[union-attr]
        if projection is None:
            return _response(404, {"detail": "evolution episode is unavailable"})
        return _response(200, _summary(projection))
    except (EvolutionContractError, RecursiveEvolutionRuntimeError) as error:
        return _error(error)
    except Exception as error:
        return _error(error)


@router.post("/episodes")
async def create_episode(project_id: str, request: Request) -> JSONResponse:
    runtime, failure = _available(request, "create_episode")
    if failure is not None:
        return failure
    try:
        body = await _body(request, _EPISODE_FIELDS)
        episode = EvolutionEpisode.from_payload(body["episode_id"], {
            "project_id": project_id,
            "target_kind": body["target_kind"],
            "policy": body["policy"],
        })
        return _result(runtime.create_episode(episode=episode, command_id=_text(body["command_id"], "command id")))  # type: ignore[union-attr]
    except (EvolutionContractError, RecursiveEvolutionRuntimeError) as error:
        return _error(error)
    except Exception as error:
        return _error(error)


@router.post("/episodes/{episode_id}/candidates")
async def record_candidate(project_id: str, episode_id: str, request: Request) -> JSONResponse:
    runtime, failure = _available(request, "record_candidate")
    if failure is not None:
        return failure
    try:
        body = await _body(request, _PROPOSAL_FIELDS)
        proposal_payload = {key: value for key, value in body.items() if key != "command_id"}
        proposal = EvolutionProposal.from_payload(proposal_payload)
        if proposal.episode_id != episode_id:
            raise EvolutionContractError("candidate episode scope is invalid")
        return _result(runtime.record_candidate(  # type: ignore[union-attr]
            project_id=project_id, proposal=proposal,
            command_id=_text(body["command_id"], "command id"),
        ))
    except (EvolutionContractError, RecursiveEvolutionRuntimeError) as error:
        return _error(error)
    except Exception as error:
        return _error(error)


@router.post("/episodes/{episode_id}/candidates/{proposal_id}/evaluations")
async def record_evaluation(
    project_id: str, episode_id: str, proposal_id: str, request: Request,
) -> JSONResponse:
    runtime, failure = _available(request, "record_evaluation_from_receipt")
    if failure is not None:
        return failure
    try:
        body = await _body(request, {"command_id", "receipt_ref"})
        return _result(runtime.record_evaluation_from_receipt(  # type: ignore[union-attr]
            project_id=project_id, episode_id=episode_id, proposal_id=proposal_id,
            receipt_ref=_text(body["receipt_ref"], "receipt ref"),
            command_id=_text(body["command_id"], "command id"),
        ))
    except (EvolutionContractError, RecursiveEvolutionRuntimeError) as error:
        return _error(error)
    except Exception as error:
        return _error(error)


@router.post("/episodes/{episode_id}/reviews")
async def record_review(project_id: str, episode_id: str, request: Request) -> JSONResponse:
    runtime, failure = _available(request, "record_review")
    if failure is not None:
        return failure
    try:
        body = await _body(request, _REVIEW_FIELDS)
        review_payload = {key: value for key, value in body.items() if key != "command_id"}
        review = EvolutionReview.from_payload(review_payload)
        if review.episode_id != episode_id:
            raise EvolutionContractError("review episode scope is invalid")
        return _result(runtime.record_review(  # type: ignore[union-attr]
            project_id=project_id, review=review,
            command_id=_text(body["command_id"], "command id"),
        ))
    except (EvolutionContractError, RecursiveEvolutionRuntimeError) as error:
        return _error(error)
    except Exception as error:
        return _error(error)


@router.post("/episodes/{episode_id}/candidates/{proposal_id}/canary/approve")
async def approve_canary(
    project_id: str, episode_id: str, proposal_id: str, request: Request,
) -> JSONResponse:
    runtime, failure = _available(request, "approve_canary")
    if failure is not None:
        return failure
    try:
        body = await _body(request, _HUMAN_FIELDS)
        return _result(runtime.approve_canary(  # type: ignore[union-attr]
            project_id=project_id, episode_id=episode_id, proposal_id=proposal_id,
            user_id=_text(body["user_id"], "user id"),
            confirmation_ref=_text(body["confirmation_ref"], "confirmation ref"),
            evidence_refs=_refs(body["evidence_refs"]),
            command_id=_text(body["command_id"], "command id"),
        ))
    except (EvolutionContractError, RecursiveEvolutionRuntimeError) as error:
        return _error(error)
    except Exception as error:
        return _error(error)


async def _local_action(
    *,
    action: str,
    project_id: str,
    episode_id: str,
    proposal_id: str | None,
    request: Request,
) -> JSONResponse:
    if not _is_local(request):
        return _response(403, {"detail": "recursive evolution is local-only"})
    service = _local_actions(request)
    if not callable(getattr(service, "execute", None)):
        return _response(503, {"detail": "recursive evolution authority is unavailable"})
    try:
        body = await _body(request, _LOCAL_ACTION_FIELDS)
        return _result(service.execute(
            project_id=project_id,
            episode_id=episode_id,
            proposal_id=proposal_id,
            action=action,
            command_id=_text(body["command_id"], "command id"),
        ))
    except (EvolutionContractError, RecursiveEvolutionRuntimeError) as error:
        return _error(error)
    except Exception as error:
        return _error(error)


@router.post("/episodes/{episode_id}/candidates/{proposal_id}/actions/{action}")
async def local_candidate_action(
    project_id: str,
    episode_id: str,
    proposal_id: str,
    action: str,
    request: Request,
) -> JSONResponse:
    if action not in {"start-canary", "promote", "rollback", "reject"}:
        return _response(404, {"detail": "recursive evolution action is unavailable"})
    return await _local_action(
        action=action.replace("-", "_"),
        project_id=project_id,
        episode_id=episode_id,
        proposal_id=proposal_id,
        request=request,
    )


@router.post("/episodes/{episode_id}/actions/stop")
async def local_stop_action(
    project_id: str, episode_id: str, request: Request,
) -> JSONResponse:
    return await _local_action(
        action="stop", project_id=project_id, episode_id=episode_id,
        proposal_id=None, request=request,
    )


@router.post("/episodes/{episode_id}/candidates/{proposal_id}/canary/observe")
async def observe_canary(
    project_id: str, episode_id: str, proposal_id: str, request: Request,
) -> JSONResponse:
    runtime, failure = _available(request, "observe_canary")
    if failure is not None:
        return failure
    try:
        body = await _body(request, _OBSERVATION_FIELDS)
        return _result(runtime.observe_canary(  # type: ignore[union-attr]
            project_id=project_id, episode_id=episode_id, proposal_id=proposal_id,
            evidence_ref=_text(body["evidence_ref"], "evidence ref"),
            command_id=_text(body["command_id"], "command id"),
        ))
    except (EvolutionContractError, RecursiveEvolutionRuntimeError) as error:
        return _error(error)
    except Exception as error:
        return _error(error)


async def _human_action(
    action: str, project_id: str, episode_id: str, proposal_id: str, request: Request,
) -> JSONResponse:
    runtime, failure = _available(request, action)
    if failure is not None:
        return failure
    try:
        body = await _body(request, _HUMAN_FIELDS)
        result = getattr(runtime, action)(
            project_id=project_id, episode_id=episode_id, proposal_id=proposal_id,
            user_id=_text(body["user_id"], "user id"),
            confirmation_ref=_text(body["confirmation_ref"], "confirmation ref"),
            evidence_refs=_refs(body["evidence_refs"]),
            command_id=_text(body["command_id"], "command id"),
        )
        return _result(result)
    except (EvolutionContractError, RecursiveEvolutionRuntimeError) as error:
        return _error(error)
    except Exception as error:
        return _error(error)


@router.post("/episodes/{episode_id}/candidates/{proposal_id}/promote")
async def promote(project_id: str, episode_id: str, proposal_id: str, request: Request) -> JSONResponse:
    return await _human_action("promote", project_id, episode_id, proposal_id, request)


@router.post("/episodes/{episode_id}/candidates/{proposal_id}/rollback")
async def rollback(project_id: str, episode_id: str, proposal_id: str, request: Request) -> JSONResponse:
    return await _human_action("rollback", project_id, episode_id, proposal_id, request)


@router.post("/episodes/{episode_id}/candidates/{proposal_id}/reject")
async def reject(project_id: str, episode_id: str, proposal_id: str, request: Request) -> JSONResponse:
    return await _human_action("reject", project_id, episode_id, proposal_id, request)


@router.post("/episodes/{episode_id}/stop")
async def stop_episode(project_id: str, episode_id: str, request: Request) -> JSONResponse:
    runtime, failure = _available(request, "stop")
    if failure is not None:
        return failure
    try:
        body = await _body(request, _HUMAN_FIELDS)
        return _result(runtime.stop(  # type: ignore[union-attr]
            project_id=project_id, episode_id=episode_id, reason="user",
            user_id=_text(body["user_id"], "user id"),
            confirmation_ref=_text(body["confirmation_ref"], "confirmation ref"),
            evidence_refs=_refs(body["evidence_refs"]),
            command_id=_text(body["command_id"], "command id"),
        ))
    except (EvolutionContractError, RecursiveEvolutionRuntimeError) as error:
        return _error(error)
    except Exception as error:
        return _error(error)
