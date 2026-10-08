"""Local governance API for configured Agent profiles and safe run status."""
from __future__ import annotations

from backend.security.device_identity import server_mode, server_authorized

import asyncio
import json
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from backend.api.agent_organization_runtime import AgentOrganizationError
from backend.api.agent_organization_projection import build_agent_organization_projection
from backend.api.ai_runtime import get_or_build_ai_runtime
from backend.api.ai_turn_runner import AITurnRunnerCapacityError
from backend.api.container import ApiContainerDep
from backend.api.desktop_session import DESKTOP_SESSION_HEADER, desktop_session, desktop_session_authorized
from backend.api.series_turn_scope_authority import build_series_turn_scope_authority
from core.ai_kernel import AIKernelContractError, AIKernelRuntimeError
from core.ai_kernel.agent_contracts import agent_profile_to_payload
from core.ai_kernel.agent_profiles import AgentProfileConflict, AgentProfileError


router = APIRouter(prefix="/api/ai", tags=["ai-agent-governance"])
_FORBIDDEN_AGENT_FIELDS = frozenset({
    "provider", "provider_id", "model", "model_name", "endpoint", "secret",
    "api_key", "base_url", "token", "credential", "authorization", "password",
    "cookie", "private_key", "local_path", "hidden_context", "context", "path",
    "input", "prompt",
})
_PUBLIC_AGENT_ROUTE_BINDING_FIELDS = frozenset({"model_route_key", "model_route_revision"})
_TERMINAL = frozenset({"completed", "failed", "cancelled", "timed_out", "quarantined", "stopped"})


def _response(status: int, body: object) -> JSONResponse:
    return JSONResponse(content=body, status_code=status, headers={"Cache-Control": "no-store"})


@router.get("/agent-profiles")
async def list_agent_profiles(request: Request) -> JSONResponse:
    if not _local_governance(request):
        return _response(403, {"detail": "Agent profile governance is local-only"})
    try:
        profiles = _profiles(request).list_profiles()
        return _response(200, {"items": [_profile_public(item) for item in profiles]})
    except Exception as error:
        return _agent_error(error, "Agent profile governance")


@router.post("/agent-profiles")
async def create_agent_profile(request: Request) -> JSONResponse:
    if not _local_governance(request):
        return _response(403, {"detail": "Agent profile governance is local-only"})
    body = await _body(request)
    if isinstance(body, JSONResponse): return body
    if set(body) != {"profile"} or not isinstance(body.get("profile"), Mapping) or _contains_forbidden(body["profile"]):
        return _response(400, {"detail": "Agent profile body rejected"})
    try:
        profile = _profiles(request).create_custom_from_payload(body["profile"])
    except AgentProfileConflict:
        return _response(409, {"detail": "Agent profile conflict"})
    except (AgentProfileError, TypeError, ValueError):
        return _response(400, {"detail": "Agent profile rejected"})
    except Exception as error:
        return _agent_error(error, "Agent profile governance")
    return _response(201, _profile_public(profile))


@router.put("/agent-profiles/{profile_id}")
async def update_agent_profile(profile_id: str, request: Request) -> JSONResponse:
    if not _local_governance(request): return _response(403, {"detail": "Agent profile governance is local-only"})
    body = await _body(request)
    if isinstance(body, JSONResponse): return body
    if set(body) != {"expected_revision", "profile"} or not isinstance(body.get("profile"), Mapping) or body["profile"].get("profile_id") != profile_id or _contains_forbidden(body["profile"]):
        return _response(400, {"detail": "Agent profile body rejected"})
    expected = body.get("expected_revision")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
        return _response(400, {"detail": "Agent profile revision rejected"})
    try:
        profile = _profiles(request).update_from_payload(body["profile"], expected_revision=expected)
    except AgentProfileConflict:
        return _response(409, {"detail": "Agent profile conflict"})
    except (AgentProfileError, TypeError, ValueError):
        return _response(400, {"detail": "Agent profile rejected"})
    except Exception as error:
        return _agent_error(error, "Agent profile governance")
    return _response(200, _profile_public(profile))


@router.delete("/agent-profiles/{profile_id}")
async def delete_agent_profile(profile_id: str, request: Request) -> Response:
    if not _local_governance(request): return _response(403, {"detail": "Agent profile governance is local-only"})
    body = await _body(request)
    if isinstance(body, JSONResponse): return body
    if set(body) != {"expected_revision"} or not isinstance(body.get("expected_revision"), int) or isinstance(body["expected_revision"], bool):
        return _response(400, {"detail": "Agent profile revision rejected"})
    try:
        _profiles(request).delete_custom(profile_id, expected_revision=body["expected_revision"])
    except AgentProfileConflict:
        return _response(409, {"detail": "Agent profile conflict"})
    except (AgentProfileError, TypeError, ValueError):
        return _response(400, {"detail": "Agent profile rejected"})
    except Exception as error:
        return _agent_error(error, "Agent profile governance")
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.post("/agent-turns")
async def start_agent_organization_turn(request: Request, container: ApiContainerDep) -> JSONResponse:
    """Start the fixed main/steward organization through the governed runtime.

    This remains a separate admission path from ``/turns``: it canonicalizes
    the same series authority, but only the organization runtime may create
    the main/steward topology and opt into host Agent capabilities.
    """
    if not _local_governance(request):
        return _response(403, {"detail": "Agent organization is local-only"})
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    try:
        scope = body.get("scope")
        if isinstance(scope, Mapping) and scope.get("kind") == "series":
            authority = await asyncio.to_thread(
                build_series_turn_scope_authority, getattr(container, "root_dir"),
            )
            body = await asyncio.to_thread(authority.canonicalize, body)
        await asyncio.to_thread(get_or_build_ai_runtime, request, container)
        organization = _organization(request)
        result = await asyncio.to_thread(organization.start, body, agent_turn_mode=True)
    except AITurnRunnerCapacityError:
        return _response(503, {
            "detail": "Agent organization runner unavailable",
            "error_code": "ai.runner_capacity",
        })
    except (AIKernelContractError, AIKernelRuntimeError, TypeError, ValueError):
        return _response(400, {"detail": "Agent organization rejected"})
    except AgentOrganizationError:
        return _response(400, {"detail": "Agent organization rejected"})
    except Exception as error:
        if _is_missing_composition(error):
            return _response(503, {"detail": "Agent organization is unavailable"})
        return _response(500, {"detail": "Agent organization failed"})
    if not isinstance(result, Mapping):
        return _response(500, {"detail": "Agent organization failed"})
    return _response(202, _safe(result))


@router.get("/projects/{project_id}/agent-topology")
async def agent_topology(project_id: str, request: Request) -> JSONResponse:
    if not _local_governance(request): return _response(403, {"detail": "Agent topology is local-only"})
    turn_id = request.query_params.get("turn_id")
    include_messages = request.query_params.get("include_messages", "false") == "true"
    try:
        return _response(200, await asyncio.to_thread(_topology, request, project_id, turn_id, include_messages))
    except KeyError: return _response(404, {"detail": "Agent run not found"})
    except ValueError: return _response(400, {"detail": "Agent topology rejected"})
    except Exception as error: return _agent_error(error, "Agent topology")


@router.get("/projects/{project_id}/agent-organization")
async def agent_organization(project_id: str, request: Request) -> JSONResponse:
    """Read the safe organization view; ``turn_id`` is intentionally optional."""
    if not _local_governance(request): return _response(403, {"detail": "Agent organization is local-only"})
    turn_id = request.query_params.get("turn_id")
    try:
        topology = _topology(request, project_id, turn_id, False) if turn_id else None
        return _response(200, build_agent_organization_projection(
            project_id=project_id, profiles=_profiles(request), topology=topology,
            dispatch_store=_dispatch_store(request),
        ))
    except KeyError: return _response(404, {"detail": "Agent run not found"})
    except ValueError: return _response(400, {"detail": "Agent organization rejected"})
    except Exception as error: return _agent_error(error, "Agent organization")


@router.get("/projects/{project_id}/agent-tasks")
async def agent_tasks(project_id: str, request: Request) -> JSONResponse:
    if not _local_governance(request): return _response(403, {"detail": "Agent tasks are local-only"})
    try:
        result = await asyncio.to_thread(_topology, request, project_id, request.query_params.get("turn_id"), False)
    except KeyError: return _response(404, {"detail": "Agent run not found"})
    except ValueError: return _response(400, {"detail": "Agent tasks rejected"})
    except Exception as error: return _agent_error(error, "Agent tasks")
    return _response(200, {"run": result["run"], "tasks": result["children"]})


@router.post("/projects/{project_id}/agent-operations/{operation}")
async def agent_operation(project_id: str, operation: str, request: Request) -> JSONResponse:
    if not _local_governance(request): return _response(403, {"detail": "Agent operation is local-only"})
    body = await _body(request)
    if isinstance(body, JSONResponse): return body
    allowed = {
        "interrupt": {"turn_id", "child_run_id", "operation_id", "reason"},
        "wait": {"turn_id", "child_run_ids", "timeout_ms", "operation_id"},
        "fan-in": {"turn_id", "child_run_ids", "policy", "quorum", "operation_id"},
    }.get(operation)
    if allowed is None or set(body) != allowed:
        return _response(400, {"detail": "Agent operation body rejected"})
    try:
        scope, privacy = _frozen_authority(request, str(body["turn_id"]), project_id)
        coordinator = _coordinator(request)
        method = {"interrupt": "interrupt", "wait": "wait", "fan-in": "fan_in"}[operation]
        arguments = {key: value for key, value in body.items() if key not in {"turn_id", "operation_id"}}
        result = await asyncio.to_thread(getattr(coordinator, method), parent_turn_id=body["turn_id"], operation_id=body["operation_id"], project_id=project_id, scope=scope, privacy=privacy, arguments=arguments)
    except KeyError: return _response(404, {"detail": "Agent run not found"})
    except (TypeError, ValueError): return _response(400, {"detail": "Agent operation rejected"})
    except Exception as error: return _agent_error(error, "Agent operation")
    return _response(202, _safe(result))


@router.get("/projects/{project_id}/agent-topology/stream")
async def stream_agent_topology(project_id: str, request: Request):
    if not _local_governance(request): return _response(403, {"detail": "Agent topology is local-only"})
    turn_id = request.query_params.get("turn_id")
    try: after = _cursor(request)
    except ValueError: return _response(400, {"detail": "Agent stream cursor rejected"})
    try: _topology(request, project_id, turn_id, False)
    except KeyError: return _response(404, {"detail": "Agent run not found"})
    except ValueError: return _response(400, {"detail": "Agent topology rejected"})
    except Exception as error: return _agent_error(error, "Agent topology")
    return StreamingResponse(_topology_stream(request, project_id, str(turn_id), after), media_type="text/event-stream", headers={"Cache-Control": "no-store, no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})


@router.get("/projects/{project_id}/agent-organization/stream")
async def stream_agent_organization(project_id: str, request: Request):
    if not _local_governance(request): return _response(403, {"detail": "Agent organization is local-only"})
    turn_id = request.query_params.get("turn_id")
    if not turn_id:
        return _response(400, {"detail": "Agent organization stream requires turn_id"})
    try: after = _cursor(request)
    except ValueError: return _response(400, {"detail": "Agent stream cursor rejected"})
    try: _topology(request, project_id, turn_id, False)
    except KeyError: return _response(404, {"detail": "Agent run not found"})
    except ValueError: return _response(400, {"detail": "Agent organization rejected"})
    except Exception as error: return _agent_error(error, "Agent organization")
    return StreamingResponse(_organization_stream(request, project_id, turn_id, after), media_type="text/event-stream", headers={"Cache-Control": "no-store, no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})


async def _topology_stream(request: Request, project_id: str, turn_id: str, cursor: int):
    last = None
    while not await request.is_disconnected():
        try: projection = await asyncio.to_thread(_topology, request, project_id, turn_id, False)
        except asyncio.CancelledError:
            raise
        except Exception:
            yield "event: stream_error\ndata: {\"error_code\":\"agent.topology_unavailable\"}\n\n"; return
        encoded = json.dumps(projection, ensure_ascii=False, separators=(",", ":"))
        if encoded != last:
            cursor += 1
            yield f"id: {cursor}\nevent: topology\ndata: {json.dumps({'project_id': project_id, 'turn_id': turn_id, 'cursor': cursor, 'topology': projection}, ensure_ascii=False, separators=(',', ':'))}\n\n"
            last = encoded
        children = projection.get("children", ())
        if projection.get("run", {}).get("status") in _TERMINAL and all(item.get("status") in _TERMINAL for item in children if isinstance(item, Mapping)): return
        await asyncio.sleep(0.1)


async def _organization_stream(request: Request, project_id: str, turn_id: str, cursor: int):
    last_revision = None
    while not await request.is_disconnected():
        try:
            topology = await asyncio.to_thread(_topology, request, project_id, turn_id, False)
            projection = build_agent_organization_projection(
                project_id=project_id, profiles=_profiles(request), topology=topology,
                dispatch_store=_dispatch_store(request),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            yield "event: stream_error\ndata: {\"error_code\":\"agent.organization_unavailable\"}\n\n"; return
        revision = projection["projection_revision"]
        if revision != last_revision:
            cursor += 1
            event = {"project_id": project_id, "turn_id": turn_id, "cursor": cursor, "projection_revision": revision, "organization": projection}
            yield f"id: {cursor}\nevent: organization\ndata: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
            last_revision = revision
        run = projection.get("main")
        if isinstance(run, Mapping) and run.get("status") in _TERMINAL:
            return
        await asyncio.sleep(0.1)


def _topology(request: Request, project_id: str, turn_id: object, include_messages: bool) -> Mapping[str, object]:
    if not isinstance(turn_id, str) or not turn_id: raise ValueError("turn_id is required")
    scope, privacy = _frozen_authority(request, turn_id, project_id)
    result = _coordinator(request).list(parent_turn_id=turn_id, project_id=project_id, scope=scope, privacy=privacy, arguments={"include_messages": include_messages})
    if not isinstance(result, Mapping): raise RuntimeError("agent coordinator result is invalid")
    return _safe(result)


def _profiles(request: Request):
    state = request.app.state
    result = getattr(state, "agent_profile_registry", getattr(state, "agent_profiles", None))
    if result is None: raise RuntimeError("agent profiles are unavailable")
    return result


def _coordinator(request: Request):
    state = request.app.state
    result = getattr(state, "agent_run_coordinator", getattr(state, "agent_coordinator", None))
    if result is None: raise RuntimeError("agent coordinator is unavailable")
    return result


def _dispatch_store(request: Request):
    composition = getattr(request.app.state, "agent_runtime_composition", None)
    return getattr(composition, "dispatch_store", None)


def _organization(request: Request):
    result = getattr(request.app.state, "agent_organization_runtime", None)
    if result is None:
        raise RuntimeError("agent organization is unavailable")
    start = getattr(result, "start", None)
    if not callable(start):
        raise RuntimeError("agent organization is unavailable")
    return result


def _frozen_authority(request: Request, turn_id: str, project_id: str) -> tuple[Mapping[str, object], Mapping[str, object]]:
    loader = getattr(request.app.state, "agent_turn_request_loader", None)
    if not callable(loader):
        loader = getattr(_coordinator(request), "request_for_turn", None)
    if not callable(loader): raise RuntimeError("agent turn authority is unavailable")
    frozen = loader(turn_id)
    if not isinstance(frozen, Mapping): raise KeyError(turn_id)
    scope, privacy = frozen.get("scope"), frozen.get("privacy")
    if not isinstance(scope, Mapping) or scope.get("project_id") != project_id or not isinstance(privacy, Mapping): raise ValueError("agent run is outside the project scope")
    return dict(scope), dict(privacy)


def _profile_public(profile: object) -> Mapping[str, object]:
    payload = agent_profile_to_payload(profile)
    return _safe(payload)


def _safe(value: object) -> Any:
    if is_dataclass(value): value = asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _safe(item) for key, item in value.items() if not _forbidden_agent_field(key)}
    if isinstance(value, (list, tuple)): return [_safe(item) for item in value]
    return value


def _contains_forbidden(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            not isinstance(key, str)
            or _forbidden_agent_field(key)
            or _contains_forbidden(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden(item) for item in value)
    return False


def _forbidden_agent_field(key: object) -> bool:
    """Keep provider resolution and private execution context off this API.

    ``model_tier``, ``model_calls`` and the opaque route binding pair are
    governed configuration fields.  A route key is not a provider endpoint or
    credential, and its concrete provider/model display is obtained only from
    the existing model-route projection.
    """
    if not isinstance(key, str):
        return True
    normalized = key.strip().lower().replace("-", "_")
    compact = "".join(character for character in normalized if character.isalnum())
    if normalized in _PUBLIC_AGENT_ROUTE_BINDING_FIELDS:
        return False
    if normalized in _FORBIDDEN_AGENT_FIELDS:
        return True
    if normalized.startswith(("provider_", "secret_", "credential_", "token_", "api_key_", "local_path_", "hidden_context_", "endpoint_")):
        return True
    if normalized.startswith("model_") and normalized not in {"model_tier", "model_calls"}:
        return True
    if compact.startswith((
        "provider", "secret", "credential", "token", "apikey", "baseurl",
        "authorization", "password", "cookie", "privatekey", "localpath",
        "hiddencontext", "context", "path", "prompt", "endpoint",
    )):
        return True
    if compact.startswith("model") and compact not in {"modeltier", "modelcalls"}:
        return True
    return normalized.endswith(("_secret", "_credential", "_token", "_api_key", "_path", "_endpoint")) or compact.endswith(("secret", "credential", "token", "apikey", "path", "context", "prompt", "endpoint"))


async def _body(request: Request) -> dict[str, Any] | JSONResponse:
    try: body = await request.json()
    except ValueError: return _response(400, {"detail": "request body must be JSON"})
    return dict(body) if isinstance(body, Mapping) else _response(400, {"detail": "request body must be an object"})


def _local_governance(request: Request) -> bool:
    if server_mode(request):
        return server_authorized(request)
    host = request.client.host if request.client else ""
    if host not in {"localhost", "testclient", "127.0.0.1", "::1"}: return False
    try:
        session = desktop_session()
        return session is None or desktop_session_authorized(request.headers.get(DESKTOP_SESSION_HEADER))
    except RuntimeError: return False


def _cursor(request: Request) -> int:
    value = request.headers.get("last-event-id", request.query_params.get("after", "0"))
    if not isinstance(value, str) or not value.isdecimal() or len(value) > 18: raise ValueError("cursor must be a non-negative integer")
    cursor = int(value)
    if cursor > 9_223_372_036_854_775_807: raise ValueError("cursor is outside the supported range")
    return cursor


def _unavailable_errors(): return (AttributeError, RuntimeError)


def _agent_error(error: Exception, subject: str) -> JSONResponse:
    """Map known domain failures without importing coordinator implementation."""
    name = type(error).__name__
    if name.endswith("Conflict") or name in {"AgentStoreConflict", "AgentProfileConflict"}:
        return _response(409, {"detail": f"{subject} conflict"})
    if name in {"AgentCoordinatorError", "AgentStoreError", "AgentContractError", "AgentProfileError"} or isinstance(error, (TypeError, ValueError)):
        return _response(400, {"detail": f"{subject} rejected"})
    if _is_missing_composition(error):
        return _response(503, {"detail": f"{subject} is unavailable"})
    return _response(500, {"detail": f"{subject} failed"})


def _is_missing_composition(error: Exception) -> bool:
    return isinstance(error, (AttributeError, RuntimeError)) and str(error) in {
        "agent profiles are unavailable", "agent coordinator is unavailable",
        "agent turn authority is unavailable", "agent coordinator result is invalid",
        "agent organization is unavailable",
    }
