"""Explicit proposal-only learning from one completed AI Turn."""
from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.ai_runtime import get_or_build_ai_runtime
from backend.api.application_skill_learning_composition import (
    TurnProjectAuthority,
    compose_application_skill_learning,
    current_application_skill_snapshot,
)
from backend.api.container import ApiContainerDep
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.application_skill import (
    ApplicationSkillManagementConflict,
    ApplicationSkillManagementError,
    ApplicationSkillProposalRegistry,
    ObjectStoreApplicationSkillTraceRepository,
    SkillLearningWorkshopError,
)


router = APIRouter(tags=["application-skill-learning"])
_PATH = "/api/rebuild/developer-studio/application-skills/learning-proposals"
_ELIGIBLE_PATH = "/api/rebuild/developer-studio/application-skills/learning-eligible-invocations"
_FIELDS = {
    "turn_id", "resolution_id", "skill_id", "expected_fingerprint",
    "reusable_signal", "proposed_content",
}
_NO_STORE = {"Cache-Control": "no-store"}
# Compatibility name for focused contract tests and older local imports.  The
# implementation now lives in the shared proposal composition module.
_TurnProjectAuthority = TurnProjectAuthority


@router.post(_PATH)
async def propose_application_skill_learning(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return _error(400, "application_skill_learning_invalid")
    if not isinstance(body, Mapping) or set(body) != _FIELDS:
        return _error(400, "application_skill_learning_invalid")
    for field in ("turn_id", "resolution_id", "skill_id", "expected_fingerprint"):
        if not isinstance(body.get(field), str):
            return _error(400, "application_skill_learning_invalid")
    if not isinstance(body.get("reusable_signal"), Mapping) or not isinstance(body.get("proposed_content"), Mapping):
        return _error(400, "application_skill_learning_invalid")
    try:
        composition = compose_application_skill_learning(
            request,
            container,
            turn_id=str(body["turn_id"]),
            skill_id=str(body["skill_id"]),
        )
        result = composition.runtime.propose(
            turn_id=str(body["turn_id"]),
            resolution_id=str(body["resolution_id"]),
            skill_id=str(body["skill_id"]),
            expected_fingerprint=str(body["expected_fingerprint"]),
            reusable_signal=body["reusable_signal"],
            proposed_content=body["proposed_content"],
        )
    except (SkillLearningWorkshopError, ValueError):
        return _error(409, "application_skill_learning_rejected")
    return JSONResponse(status_code=201, content=result, headers=_NO_STORE)


@router.get(_ELIGIBLE_PATH)
async def list_learning_eligible_invocations(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    project_id = request.query_params.get("project_id")
    turn_id = request.query_params.get("turn_id")
    resolution_id = request.query_params.get("resolution_id")
    if (
        not isinstance(project_id, str) or not project_id
        or not isinstance(turn_id, str) or not turn_id
        or not isinstance(resolution_id, str) or not resolution_id
    ):
        return _error(400, "application_skill_learning_invalid")
    try:
        turn_store = getattr(request.app.state, "ai_turn_effect_store", None)
        projects = TurnProjectAuthority(turn_store)
        store, _settings = build_rebuild_object_store(container.root_dir)
        invocation = _eligible_invocation(
            request=request,
            container=container,
            store=store,
            receipts=get_or_build_ai_runtime(request, container),
            projects=projects,
            project_id=project_id,
            turn_id=turn_id,
            resolution_id=resolution_id,
        )
    except (SkillLearningWorkshopError, ValueError):
        return _error(409, "application_skill_learning_rejected")
    if invocation is None:
        return _error(404, "application_skill_learning_not_found")
    return JSONResponse(status_code=200, content=invocation, headers=_NO_STORE)


@router.get(_PATH)
async def list_application_skill_learning_proposals(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    project_id = request.query_params.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        return _error(400, "application_skill_learning_invalid")
    store, _settings = build_rebuild_object_store(container.root_dir)
    proposals = [
        _proposal_projection(item)
        for item in ApplicationSkillProposalRegistry(store).list()
        if _proposal_project_id(item) == project_id
    ]
    proposals.sort(key=lambda item: str(item["created_at"]), reverse=True)
    return JSONResponse(status_code=200, content={"project_id": project_id, "proposals": proposals}, headers=_NO_STORE)


@router.get(f"{_PATH}/{{proposal_id}}")
async def get_application_skill_learning_proposal(
    proposal_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    project_id = request.query_params.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        return _error(400, "application_skill_learning_invalid")
    store, _settings = build_rebuild_object_store(container.root_dir)
    record = _learning_proposal(store, proposal_id, project_id)
    if record is None:
        return _error(404, "application_skill_learning_not_found")
    return JSONResponse(status_code=200, content=_proposal_projection(record), headers=_NO_STORE)


@router.post(f"{_PATH}/{{proposal_id}}/review")
async def review_application_skill_learning_proposal(
    proposal_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return _error(400, "application_skill_learning_invalid")
    if not isinstance(body, Mapping) or set(body) != {"project_id", "decision", "reason", "confirm"}:
        return _error(400, "application_skill_learning_invalid")
    project_id, decision, reason = body.get("project_id"), body.get("decision"), body.get("reason")
    if (
        not isinstance(project_id, str)
        or not isinstance(decision, str)
        or not isinstance(reason, str)
        or body.get("confirm") is not True
    ):
        return _error(400, "application_skill_learning_invalid")
    store, _settings = build_rebuild_object_store(container.root_dir)
    record = _learning_proposal(store, proposal_id, project_id)
    if record is None:
        return _error(404, "application_skill_learning_not_found")
    try:
        proposals = ApplicationSkillProposalRegistry(store)
        action = str(record["action"])
        payload = record["payload"]
        if decision == "approve":
            result = proposals.require_approved(
                proposal_id, action=action, expected_payload=payload,
                confirm=True, reason=reason,
            )
        elif decision == "reject":
            result = proposals.reject(
                proposal_id, action=action, expected_payload=payload,
                confirm=True, reason=reason,
            )
        else:
            return _error(400, "application_skill_learning_invalid")
    except (ApplicationSkillManagementConflict, ApplicationSkillManagementError, SkillLearningWorkshopError, ValueError):
        return _error(409, "application_skill_learning_rejected")
    return JSONResponse(status_code=200, content=_proposal_projection(result), headers=_NO_STORE)


def _eligible_invocation(
    *,
    request: Request,
    container: object,
    store: object,
    receipts: object,
    projects: TurnProjectAuthority,
    project_id: str,
    turn_id: str,
    resolution_id: str,
) -> dict[str, object] | None:
    """Read exactly one completed, traced Turn without a repository scan."""
    receipt_for = getattr(receipts, "receipt_for", None)
    if not callable(receipt_for):
        raise SkillLearningWorkshopError("Skill learning authority is unavailable")
    traces = ObjectStoreApplicationSkillTraceRepository(store)
    trace = traces.get_trace(resolution_id)
    if trace is None or trace.get("invocation_id") != turn_id:
        return None
    try:
        if projects.project_id_for(turn_id) != project_id:
            return None
        receipt = receipt_for(turn_id)
    except Exception:
        return None
    if (
        getattr(receipt, "turn_id", None) != turn_id
        or getattr(receipt, "status", None) != "completed"
        or not isinstance(getattr(receipt, "operation_id", None), str)
    ):
        return None
    selected: list[dict[str, object]] = []
    for item in trace.get("selected", []):
        if not isinstance(item, Mapping):
            continue
        skill_id, fingerprint = item.get("skill_id"), item.get("skill_fingerprint")
        if not isinstance(skill_id, str) or not isinstance(fingerprint, str):
            continue
        source_kind: str | None = None
        current_fingerprint: str | None = None
        try:
            package = current_application_skill_snapshot(
                request.app, container, project_id=project_id, skill_id=skill_id,
            ).get(skill_id)
            if package is not None:
                source_kind, current_fingerprint = package.source_kind, package.fingerprint
        except (SkillLearningWorkshopError, ValueError):
            pass
        selected.append({
            "skill_id": skill_id,
            "skill_fingerprint": fingerprint,
            "source_kind": source_kind,
            "current_fingerprint": current_fingerprint,
            "fingerprint_status": (
                "current" if current_fingerprint == fingerprint
                else "changed" if current_fingerprint is not None else "unavailable"
            ),
        })
    if not selected:
        return None
    return {
        "turn_id": turn_id,
        "resolution_id": resolution_id,
        "recorded_at": trace.get("recorded_at"),
        "selected": selected,
    }


def _proposal_project_id(record: object) -> str | None:
    if not isinstance(record, Mapping) or record.get("action") not in {"skill.update", "skill.fork"}:
        return None
    payload = record.get("payload")
    project_id = payload.get("project_id") if isinstance(payload, Mapping) else None
    return project_id if isinstance(project_id, str) else None


def _learning_proposal(store: object, proposal_id: str, project_id: str) -> dict[str, object] | None:
    record = getattr(store, "read")("application_skill_proposals", proposal_id)
    if _proposal_project_id(record) != project_id:
        return None
    return dict(record) if isinstance(record, Mapping) else None


def _proposal_projection(record: Mapping[str, object]) -> dict[str, object]:
    payload = record.get("payload")
    if not isinstance(payload, Mapping) or _proposal_project_id(record) is None:
        raise SkillLearningWorkshopError("Skill learning proposal has an invalid schema")
    signal = payload.get("reusable_signal")
    content = payload.get("proposed_content")
    diagnostics = payload.get("safety_diagnostics")
    if not isinstance(signal, Mapping) or not isinstance(content, Mapping) or not isinstance(diagnostics, list):
        raise SkillLearningWorkshopError("Skill learning proposal has an invalid schema")
    return {
        "proposal_id": record.get("proposal_id"),
        "action": record.get("action"),
        "status": record.get("status"),
        "created_at": record.get("created_at"),
        "reviewed_at": record.get("reviewed_at"),
        "review_reason": record.get("review_reason"),
        "turn_id": payload.get("turn_id"),
        "receipt_id": payload.get("receipt_id"),
        "project_id": payload.get("project_id"),
        "resolution_id": payload.get("resolution_id"),
        "skill_id": payload.get("skill_id"),
        "skill_fingerprint": payload.get("skill_fingerprint"),
        "source_kind": payload.get("source_kind"),
        "mutation_mode": payload.get("mutation_mode"),
        "reusable_signal": {"kind": signal.get("kind"), "evidence": signal.get("evidence")},
        "proposed_content": {"summary": content.get("summary"), "instructions": content.get("instructions")},
        "safety_diagnostics": diagnostics,
        "write_effect": "proposal_only",
    }


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail}, headers=_NO_STORE)
