"""Local-only, derived Task Case API.

This endpoint composes the existing World Model workflow with the existing
Agent organization projection.  It does not persist a task case or accept a
client-selected Turn identifier.
"""

from __future__ import annotations

from backend.security.device_identity import server_mode, server_authorized

import asyncio
import ipaddress
from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.agent_organization_projection import build_agent_organization_projection
from backend.api.ai_runtime import get_or_build_ai_runtime
from backend.api.ai_turn_runner import get_or_build_ai_turn_runner
from backend.api.container import ApiContainerDep
from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.api.personal_world_model_workflow import (
    PersonalWorldModelWorkflow,
    PersonalWorldModelWorkflowError,
)
from backend.api.world_action_agent_submitter import WorldActionAgentSubmitter
from backend.api.project_task_case_projection import build_project_task_case_projection
from backend.api.workbench_ai_runtime import WORLD_PROJECT_SESSION_ID
from core.ai_kernel import AIKernelContractError, AIKernelRuntimeError
from core.long_horizon_runtime import TaskGraphEvent, project_task_graph
from core.personal_world_model import PersonalWorldModelError, WorldEventKind


router = APIRouter(tags=["project-task-cases"])
_BASE = "/api/rebuild/projects/{project_id}/task-case"
_NO_STORE = {"Cache-Control": "no-store"}


@router.get(_BASE)
async def get_project_task_case(
    project_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    if not _local_request(request):
        return _error(403, "project_task_case_local_only")
    try:
        workflow = _workflow(request, container)
        result = await asyncio.to_thread(
            build_project_task_case_projection,
            project_id=project_id,
            workflow=workflow,
            agent_organization_for_turn=lambda turn_id: _organization_for_verified_action(
                request, project_id, turn_id,
            ),
            task_graph_for_project=_task_graph_trace_for_project(workflow),
        )
    except (
        AIKernelContractError,
        AIKernelRuntimeError,
        PersonalWorldModelError,
        PersonalWorldModelWorkflowError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return _error(409, "project_task_case_unavailable")
    return JSONResponse(result, headers=_NO_STORE)


def _workflow(request: Request, container: object) -> PersonalWorldModelWorkflow:
    turn_store = getattr(request.app.state, "ai_turn_effect_store", None)
    if turn_store is None:
        raise PersonalWorldModelWorkflowError("AI Turn authority is unavailable")
    turns = get_or_build_ai_runtime(request, container)
    runner = get_or_build_ai_turn_runner(request, turns)
    organization = getattr(request.app.state, "agent_organization_runtime", None)
    organization_submitter = (
        WorldActionAgentSubmitter(
            organization=organization,
            turns=turns,
            turn_store=turn_store,
        )
        if callable(getattr(organization, "start", None))
        else None
    )
    return PersonalWorldModelWorkflow(
        world=PersonalWorldModelRuntime.for_root(
            getattr(container, "root_dir"), receipts=turns, turn_store=turn_store,
        ),
        turns=turns,
        turn_store=turn_store,
        runner=runner,
        organization_submitter=organization_submitter,
    )


def _organization_for_verified_action(
    request: Request,
    project_id: str,
    turn_id: str,
) -> Mapping[str, object] | None:
    """Load topology only after the derived World action selected its Turn."""
    state = request.app.state
    composition = getattr(state, "agent_runtime_composition", None)
    profiles = getattr(state, "agent_profile_registry", None)
    coordinator = getattr(state, "agent_run_coordinator", None)
    loader = getattr(state, "agent_turn_request_loader", None)
    if not callable(loader) and coordinator is not None:
        loader = getattr(coordinator, "request_for_turn", None)
    if profiles is None or coordinator is None or not callable(loader):
        return None
    frozen = loader(turn_id)
    if not isinstance(frozen, Mapping):
        raise ValueError("task case action authority is invalid")
    scope, privacy = frozen.get("scope"), frozen.get("privacy")
    if (
        not isinstance(scope, Mapping)
        or scope.get("project_id") != project_id
        or frozen.get("session_id") != WORLD_PROJECT_SESSION_ID
        or not isinstance(privacy, Mapping)
    ):
        raise ValueError("task case action authority is outside project scope")
    try:
        topology = coordinator.list(
            parent_turn_id=turn_id,
            project_id=project_id,
            scope=dict(scope),
            privacy=dict(privacy),
            arguments={"include_messages": False},
        )
    except KeyError:
        # A normal World action does not necessarily start the Agent
        # organization.  The safe profile skeleton remains useful.
        topology = None
    if topology is not None and not isinstance(topology, Mapping):
        raise ValueError("task case organization topology is invalid")
    return build_agent_organization_projection(
        project_id=project_id,
        profiles=profiles,
        topology=topology,
        dispatch_store=getattr(composition, "dispatch_store", None),
    )


def _task_graph_trace_for_project(workflow: object):
    """Return the newest local task-graph projection for a project.

    This is deliberately a read-only callback.  The Task Case projection is
    the sole renderer boundary and turns the result into safe labels/counts.
    """
    world = getattr(workflow, "_world", None)
    if not callable(getattr(world, "events", None)):
        return lambda _project_id: None

    def latest_graph(project_id: str):
        grouped: dict[str, list[TaskGraphEvent]] = {}
        latest_graph_id: str | None = None
        for event in world.events(project_id):
            if event.kind is not WorldEventKind.TASK_GRAPH_EVENT_RECORDED:
                continue
            graph_event = TaskGraphEvent.from_payload(event.payload)
            if graph_event.project_id != project_id:
                raise ValueError("task graph project scope drifted")
            if graph_event.graph_id not in grouped:
                grouped[graph_event.graph_id] = []
            grouped[graph_event.graph_id].append(graph_event)
            latest_graph_id = graph_event.graph_id
        if latest_graph_id is None:
            return None
        # World events are append-only. The graph mentioned by the final
        # record is the latest active graph, while project_task_graph verifies
        # the entire grouped stream before it reaches the renderer.
        return project_task_graph(grouped[latest_graph_id])

    return latest_graph


def _local_request(request: Request) -> bool:
    if server_mode(request):
        return server_authorized(request)
    host = request.client.host if request.client is not None else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code, headers=_NO_STORE)
