"""Local project WorldEvent and derived WorldState API."""

from __future__ import annotations

from backend.security.device_identity import server_mode, server_authorized

import asyncio
import ipaddress
from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.ai_runtime import get_or_build_ai_runtime
from backend.api.ai_turn_runner import (
    AITurnRunnerCapacityError,
    get_or_build_ai_turn_runner,
)
from backend.api.application_skill_learning_composition import (
    compose_application_skill_learning,
)
from backend.api.container import ApiContainerDep
from backend.api.personal_world_model_learning import (
    PersonalWorldModelLearningRuntime,
)
from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.api.project_provenance_runtime import (
    ProjectProvenanceError,
    ProjectProvenanceRuntime,
)
from backend.api.personal_world_model_workflow import (
    PersonalWorldModelWorkflow,
    PersonalWorldModelWorkflowError,
)
from backend.api.world_action_agent_submitter import WorldActionAgentSubmitter
from backend.api.task_reference_projection import task_ref_for_world_action
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.application_skill import SkillLearningWorkshopError
from core.ai_kernel import AIKernelContractError, AIKernelRuntimeError
from core.memory_core import (
    MemoryCandidateRepositoryError,
    ObjectStoreMemoryCandidateRepository,
)
from core.personal_world_model import (
    PersonalWorldModelError,
    WorldEventDraft,
    WorldEventKind,
)


router = APIRouter(tags=["personal-world-model"])
_BASE = "/api/rebuild/projects/{project_id}/world-model"
_NO_STORE = {"Cache-Control": "no-store"}
_EVENT_FIELDS = {
    "event_id",
    "kind",
    "actor",
    "source_ref",
    "source_revision",
    "occurred_at",
    "recorded_at",
    "payload",
}
_FEEDBACK_FIELDS = {
    "event_id",
    "feedback_id",
    "supersedes_feedback_id",
    "action_id",
    "turn_id",
    "outcome_ref",
    "expected_outcome",
    "actual_outcome",
    "outcome",
    "state_delta",
    "cost",
    "user_evaluation",
    "observed_evidence_refs",
    "actor",
    "occurred_at",
    "recorded_at",
}
_LEARNING_FIELDS = {"turn_id", "memory_proposal", "skill_proposal"}
_WORKFLOW_GOAL_FIELDS = {"command_id", "title", "success_criteria"}
_WORKFLOW_ACTION_FIELDS = {
    "command_id",
    "title",
    "expected_outcome",
    "question",
}
_WORKFLOW_PIVOT_FIELDS = {
    "command_id",
    "supersedes_action_id",
    "title",
    "expected_outcome",
    "question",
}
_WORKFLOW_FEEDBACK_FIELDS = {
    "command_id",
    "actual_outcome",
    "outcome",
    "state_delta",
    "user_evaluation",
}


@router.get(f"{_BASE}/state")
async def get_project_world_state(
    project_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    if not _local_request(request):
        return _error(403, "personal_world_model_local_only")
    try:
        projection = await asyncio.to_thread(
            PersonalWorldModelRuntime.for_root(container.root_dir).project,
            project_id,
        )
    except (PersonalWorldModelError, TypeError, ValueError):
        return _error(409, "personal_world_model_state_rejected")
    return JSONResponse(projection.to_payload(), headers=_NO_STORE)


@router.get(f"{_BASE}/provenance")
async def get_project_world_provenance(
    project_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    if not _local_request(request):
        return _error(403, "personal_world_model_local_only")
    try:
        world = PersonalWorldModelRuntime.for_root(container.root_dir)
        projection = await asyncio.to_thread(
            ProjectProvenanceRuntime(world=world).project,
            project_id,
        )
    except (PersonalWorldModelError, ProjectProvenanceError, TypeError, ValueError):
        return _error(409, "personal_world_model_provenance_unavailable")
    return JSONResponse(projection.summary_payload(), headers=_NO_STORE)


@router.get(f"{_BASE}/workflow")
async def get_project_world_workflow(
    project_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    if not _local_request(request):
        return _error(403, "personal_world_model_local_only")
    try:
        result = await asyncio.to_thread(
            _workflow(request, container).overview,
            project_id=project_id,
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
        return _error(409, "personal_world_model_workflow_unavailable")
    return JSONResponse(result, headers=_NO_STORE)


@router.post(f"{_BASE}/workflow/goal")
async def declare_project_world_workflow_goal(
    project_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    if not _local_request(request):
        return _error(403, "personal_world_model_local_only")
    body = await _body(request, fields=_WORKFLOW_GOAL_FIELDS)
    if isinstance(body, JSONResponse):
        return body
    if not isinstance(body.get("success_criteria"), list):
        return _error(400, "personal_world_model_workflow_goal_invalid")
    try:
        result = await asyncio.to_thread(
            _workflow(request, container).declare_goal,
            project_id=project_id,
            command_id=body.get("command_id"),
            title=body.get("title"),
            success_criteria=body.get("success_criteria"),
        )
    except (PersonalWorldModelError, PersonalWorldModelWorkflowError, TypeError, ValueError):
        return _error(409, "personal_world_model_workflow_goal_rejected")
    return JSONResponse(
        result.to_payload(),
        status_code=200 if result.replayed else 201,
        headers=_NO_STORE,
    )


@router.post(f"{_BASE}/workflow/actions")
async def submit_project_world_workflow_action(
    project_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    if not _local_request(request):
        return _error(403, "personal_world_model_local_only")
    body = await _body(request, fields=_WORKFLOW_ACTION_FIELDS)
    if isinstance(body, JSONResponse):
        return body
    try:
        result = await asyncio.to_thread(
            _workflow(request, container).submit_action,
            project_id=project_id,
            command_id=body.get("command_id"),
            title=body.get("title"),
            expected_outcome=body.get("expected_outcome"),
            question=body.get("question"),
        )
    except AITurnRunnerCapacityError:
        return _error(503, "personal_world_model_workflow_capacity")
    except (
        AIKernelContractError,
        AIKernelRuntimeError,
        PersonalWorldModelError,
        PersonalWorldModelWorkflowError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return _error(409, "personal_world_model_workflow_action_rejected")
    return JSONResponse(
        _action_admission_payload(project_id, result.to_payload()),
        status_code=200 if result.replayed else 202,
        headers=_NO_STORE,
    )


@router.get(f"{_BASE}/workflow/actions/{{turn_id}}")
async def get_project_world_workflow_action(
    project_id: str,
    turn_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    if not _local_request(request):
        return _error(403, "personal_world_model_local_only")
    try:
        result = await asyncio.to_thread(
            _workflow(request, container).action_status,
            project_id=project_id,
            turn_id=turn_id,
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
        return _error(409, "personal_world_model_workflow_action_unavailable")
    return JSONResponse(result, headers=_NO_STORE)


@router.post(f"{_BASE}/workflow/actions/pivot")
async def pivot_project_world_workflow_action(
    project_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    if not _local_request(request):
        return _error(403, "personal_world_model_local_only")
    body = await _body(request, fields=_WORKFLOW_PIVOT_FIELDS)
    if isinstance(body, JSONResponse):
        return body
    try:
        result = await asyncio.to_thread(
            _workflow(request, container).pivot_action,
            project_id=project_id,
            command_id=body.get("command_id"),
            supersedes_action_id=body.get("supersedes_action_id"),
            title=body.get("title"),
            expected_outcome=body.get("expected_outcome"),
            question=body.get("question"),
        )
    except AITurnRunnerCapacityError:
        return _error(503, "personal_world_model_workflow_capacity")
    except (
        AIKernelContractError,
        AIKernelRuntimeError,
        PersonalWorldModelError,
        PersonalWorldModelWorkflowError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return _error(409, "personal_world_model_workflow_pivot_rejected")
    return JSONResponse(
        _action_admission_payload(project_id, result.to_payload()),
        status_code=200 if result.replayed else 202,
        headers=_NO_STORE,
    )


def _action_admission_payload(project_id: str, payload: dict) -> dict:
    """Link admitted actions to the existing durable task reader."""
    return {
        **payload,
        "task_ref": task_ref_for_world_action(
            project_id=project_id, action_id=payload["action_id"],
        ),
    }


@router.post(f"{_BASE}/workflow/actions/{{turn_id}}/feedback")
async def record_project_world_workflow_feedback(
    project_id: str,
    turn_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    if not _local_request(request):
        return _error(403, "personal_world_model_local_only")
    body = await _body(request, fields=_WORKFLOW_FEEDBACK_FIELDS)
    if isinstance(body, JSONResponse):
        return body
    if (
        not isinstance(body.get("state_delta"), list)
        or not isinstance(body.get("user_evaluation"), Mapping)
    ):
        return _error(400, "personal_world_model_workflow_feedback_invalid")
    try:
        result = await asyncio.to_thread(
            _workflow(request, container).record_feedback,
            project_id=project_id,
            turn_id=turn_id,
            command_id=body.get("command_id"),
            actual_outcome=body.get("actual_outcome"),
            outcome=body.get("outcome"),
            state_delta=body.get("state_delta"),
            user_evaluation=body.get("user_evaluation"),
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
        return _error(409, "personal_world_model_workflow_feedback_rejected")
    return JSONResponse(
        result,
        status_code=200 if result["replayed"] is True else 201,
        headers=_NO_STORE,
    )


@router.post(f"{_BASE}/events")
async def append_project_world_event(
    project_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    if not _local_request(request):
        return _error(403, "personal_world_model_local_only")
    body = await _body(request, fields=_EVENT_FIELDS)
    if isinstance(body, JSONResponse):
        return body
    if not isinstance(body.get("payload"), Mapping):
        return _error(400, "personal_world_model_event_invalid")
    try:
        kind = WorldEventKind(body.get("kind"))
        if kind in {
            WorldEventKind.FEEDBACK_RECORDED,
            WorldEventKind.SUPERVISION_CLAIM_DECLARED,
            WorldEventKind.SUPERVISION_VERIFICATION_RECORDED,
            WorldEventKind.SUPERVISION_DECISION_RECORDED,
            WorldEventKind.PROVENANCE_SUBJECT_RECORDED,
            WorldEventKind.PROVENANCE_LINK_RECORDED,
            WorldEventKind.PROVENANCE_VALIDATION_RECORDED,
            WorldEventKind.TASK_GRAPH_EVENT_RECORDED,
            WorldEventKind.TRAJECTORY_CHECKPOINT_RECORDED,
        }:
            raise PersonalWorldModelError("event kind requires system-owned authority")
        draft = WorldEventDraft(
            event_id=body.get("event_id"),
            project_id=project_id,
            kind=kind,
            actor=body.get("actor"),
            source_ref=body.get("source_ref"),
            source_revision=body.get("source_revision"),
            occurred_at=body.get("occurred_at"),
            recorded_at=body.get("recorded_at"),
            payload=body["payload"],
        )
        runtime = PersonalWorldModelRuntime.for_root(container.root_dir)
        result = await asyncio.to_thread(runtime.append_event, draft)
        projection = await asyncio.to_thread(
            runtime.project,
            project_id,
            now=draft.recorded_at,
        )
    except (PersonalWorldModelError, TypeError, ValueError):
        return _error(409, "personal_world_model_event_rejected")
    return JSONResponse(
        {
            "event": result.event.to_record(),
            "replayed": result.replayed,
            "state": projection.to_payload(),
        },
        status_code=200 if result.replayed else 201,
        headers=_NO_STORE,
    )


@router.post(f"{_BASE}/feedback")
async def record_project_world_feedback(
    project_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    if not _local_request(request):
        return _error(403, "personal_world_model_local_only")
    body = await _body(request, fields=_FEEDBACK_FIELDS)
    if isinstance(body, JSONResponse):
        return body
    if (
        not isinstance(body.get("state_delta"), list)
        or not isinstance(body.get("cost"), Mapping)
        or not isinstance(body.get("user_evaluation"), Mapping)
        or not isinstance(body.get("observed_evidence_refs"), list)
    ):
        return _error(400, "personal_world_model_feedback_invalid")
    try:
        turn_store = getattr(request.app.state, "ai_turn_effect_store", None)
        if turn_store is None:
            raise PersonalWorldModelError("AI Turn authority is unavailable")
        runtime = PersonalWorldModelRuntime.for_root(
            container.root_dir,
            receipts=get_or_build_ai_runtime(request, container),
            turn_store=turn_store,
        )
        result = await asyncio.to_thread(
            runtime.record_feedback,
            project_id=project_id,
            event_id=body.get("event_id"),
            feedback_id=body.get("feedback_id"),
            supersedes_feedback_id=body.get("supersedes_feedback_id"),
            action_id=body.get("action_id"),
            turn_id=body.get("turn_id"),
            outcome_ref=body.get("outcome_ref"),
            expected_outcome=body.get("expected_outcome"),
            actual_outcome=body.get("actual_outcome"),
            outcome=body.get("outcome"),
            state_delta=body["state_delta"],
            cost=body["cost"],
            user_evaluation=body["user_evaluation"],
            observed_evidence_refs=body["observed_evidence_refs"],
            actor=body.get("actor"),
            occurred_at=body.get("occurred_at"),
            recorded_at=body.get("recorded_at"),
        )
        projection = await asyncio.to_thread(
            runtime.project,
            project_id,
            now=str(body["recorded_at"]),
        )
    except (PersonalWorldModelError, TypeError, ValueError):
        return _error(409, "personal_world_model_feedback_rejected")
    return JSONResponse(
        {
            "event": result.append.event.to_record(),
            "replayed": result.append.replayed,
            "verified_outcome": result.evidence.to_payload(),
            "state": projection.to_payload(),
        },
        status_code=200 if result.append.replayed else 201,
        headers=_NO_STORE,
    )


@router.post(f"{_BASE}/feedback/{{feedback_id}}/learning-proposals")
async def propose_project_world_feedback_learning(
    project_id: str,
    feedback_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    if not _local_request(request):
        return _error(403, "personal_world_model_local_only")
    body = await _body(request, fields=_LEARNING_FIELDS)
    if isinstance(body, JSONResponse):
        return body
    memory_proposal = body.get("memory_proposal")
    skill_proposal = body.get("skill_proposal")
    if (
        (memory_proposal is not None and not isinstance(memory_proposal, Mapping))
        or (skill_proposal is not None and not isinstance(skill_proposal, Mapping))
        or ((memory_proposal is None) == (skill_proposal is None))
    ):
        return _error(400, "personal_world_model_learning_invalid")
    try:
        turn_id = str(body.get("turn_id") or "")
        turn_store = getattr(request.app.state, "ai_turn_effect_store", None)
        if turn_store is None:
            raise PersonalWorldModelError("AI Turn authority is unavailable")
        receipts = get_or_build_ai_runtime(request, container)
        world = PersonalWorldModelRuntime.for_root(
            container.root_dir,
            receipts=receipts,
            turn_store=turn_store,
        )
        store, _settings = build_rebuild_object_store(container.root_dir)
        skill_runtime = None
        if skill_proposal is not None:
            skill_id = skill_proposal.get("skill_id")
            if not isinstance(skill_id, str):
                raise PersonalWorldModelError("Skill learning identity is invalid")
            composition = compose_application_skill_learning(
                request,
                container,
                turn_id=turn_id,
                skill_id=skill_id,
            )
            if composition.project_id != project_id:
                raise PersonalWorldModelError("Skill learning project scope drifted")
            skill_runtime = composition.runtime
        learning = PersonalWorldModelLearningRuntime(
            world=world,
            object_store=store,
            candidates=ObjectStoreMemoryCandidateRepository(store),
            skill_runtime=skill_runtime,
        )
        result = await asyncio.to_thread(
            learning.propose,
            project_id=project_id,
            feedback_id=feedback_id,
            turn_id=turn_id,
            memory_proposal=memory_proposal,
            skill_proposal=skill_proposal,
        )
    except (
        MemoryCandidateRepositoryError,
        PersonalWorldModelError,
        SkillLearningWorkshopError,
        TypeError,
        ValueError,
    ):
        return _error(409, "personal_world_model_learning_rejected")
    return JSONResponse(
        result.to_payload(),
        status_code=200 if result.replayed else 201,
        headers=_NO_STORE,
    )


def _workflow(
    request: Request,
    container: object,
) -> PersonalWorldModelWorkflow:
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
            getattr(container, "root_dir"),
            receipts=turns,
            turn_store=turn_store,
        ),
        turns=turns,
        turn_store=turn_store,
        runner=runner,
        organization_submitter=organization_submitter,
    )


async def _body(
    request: Request,
    *,
    fields: set[str],
) -> dict[str, object] | JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return _error(400, "personal_world_model_body_invalid")
    if not isinstance(body, Mapping) or set(body) != fields:
        return _error(400, "personal_world_model_body_invalid")
    return dict(body)


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
    return JSONResponse(
        {"detail": detail},
        status_code=status_code,
        headers=_NO_STORE,
    )
