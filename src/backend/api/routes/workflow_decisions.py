from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.effect_log import EffectState, InvalidEffectTransition
from core.product_core.workflow_decision_evidence import (
    WorkflowDecisionEvidenceError,
    WorkflowGateOutcome,
    WorkflowUserChoice,
    WorkflowUserDecisionRepository,
    decide_workflow_transition,
)
from core.product_core.workflow_progression import WorkflowDecisionBoundary
from core.product_core.workflow_handler_governance import workflow_handler_governance_projection


router = APIRouter(tags=["workflow-decisions"])


def _response(status: int, body: Mapping[str, object]) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _fingerprint(effect) -> str:
    payload = {
        "operation_id": effect.operation_id,
        "intent_digest": effect.intent_digest,
        "gate_decision_id": effect.gate_decision_id,
        "rev_set": dict(effect.rev_set),
        "contract_version": effect.contract_version,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _projection(effect, decision: Mapping[str, object] | None = None) -> dict[str, object]:
    boundary = (
        WorkflowDecisionBoundary.UNKNOWN_EFFECT
        if effect.state is EffectState.UNKNOWN
        else WorkflowDecisionBoundary.DETERMINISTIC
    )
    transition = decide_workflow_transition(
        effect_states=(effect.state.value,), boundaries=(boundary,),
        # UNKNOWN is already an Effect-history boundary. Gate ask is added only
        # when a frozen Gate fact explicitly says ask; do not duplicate it here.
        gate_outcome=WorkflowGateOutcome.ALLOW,
        intent_fingerprint=_fingerprint(effect), user_decision=decision,
    )
    projection = transition.to_projection()
    if decision is not None and projection["decision_ref"] is None:
        projection["decision_ref"] = decision.get("decision_ref")
    return {
        "schema_version": transition.schema_version,
        "operation_id": effect.operation_id,
        "effect_state": effect.state.value,
        "intent_fingerprint": _fingerprint(effect),
        "handler_governance": workflow_handler_governance_projection(effect.kind),
        **{key: value for key, value in projection.items()
           if key != "schema_version"},
    }


@router.get("/api/rebuild/workflows/effects/{operation_id}")
def workflow_effect_projection(
    operation_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    try:
        effect = request.app.state.effect_runtime.log.get(operation_id)
    except KeyError:
        return _response(404, {"detail": "workflow effect was not found"})
    try:
        store, _settings = build_rebuild_object_store(container.root_dir)
        decision = WorkflowUserDecisionRepository(store).find(
            workflow_id=operation_id, intent_fingerprint=_fingerprint(effect),
        )
        return _response(200, _projection(effect, decision))
    except (RuntimeError, WorkflowDecisionEvidenceError) as error:
        return _response(409, {"detail": str(error)})


@router.post("/api/rebuild/workflows/effects/{operation_id}/decision")
async def decide_unknown_workflow_effect(
    operation_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, Mapping):
        return _response(400, {"detail": "request body is required"})
    runtime = request.app.state.effect_runtime
    try:
        effect = runtime.log.get(operation_id)
        if effect.state is not EffectState.UNKNOWN:
            return _response(409, {"detail": "workflow effect is not awaiting a user decision"})
        choice = WorkflowUserChoice(str(body.get("choice") or ""))
        store, _settings = build_rebuild_object_store(container.root_dir)
        decision = WorkflowUserDecisionRepository(store).record(
            workflow_id=operation_id,
            intent_fingerprint=_fingerprint(effect),
            reasons=("unknown_effect",), choice=choice,
            decided_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            confirm=body.get("confirm") is True,
        )
        now = int(time.time())
        updated = (
            runtime.runner.reauthorize_unknown(
                effect, probe_ref=str(decision["decision_ref"]), now=now,
            )
            if choice is WorkflowUserChoice.APPROVE
            else runtime.runner.abandon_unknown(
                effect, decision_ref=str(decision["decision_ref"]), now=now,
            )
        )
    except KeyError:
        return _response(404, {"detail": "workflow effect was not found"})
    except (ValueError, WorkflowDecisionEvidenceError, InvalidEffectTransition) as error:
        return _response(400, {"detail": str(error)})
    return _response(200, {**_projection(updated, decision), "recorded_decision": dict(decision)})
