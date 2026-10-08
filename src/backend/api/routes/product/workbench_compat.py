"""Workbench compat ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.ai_runtime import get_or_build_ai_runtime
from backend.api.container import ApiContainerDep

from core.product_core.project_skill_authoring_intent import (
    ProjectSkillAuthoringIntentError,
    classify_project_skill_authoring_intent,
)

from . import http as product_http

router = APIRouter(tags=["rebuild-product-core"])


@router.post("/api/rebuild/workbench/direct-question", deprecated=True)
async def workbench_direct_question_compat(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    """Legacy DTO adapter. All execution is owned by the unified AI Turn runtime."""
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping):
        return product_http._json_response(400, {"detail": "request body must be a JSON object"}, product_http._no_store_headers())
    question = body.get("question")
    if not isinstance(question, str) or not question.strip():
        return product_http._json_response(400, {"detail": "question must be a non-empty string"}, product_http._no_store_headers())
    project_id = body.get("project_id")
    if project_id is not None and (not isinstance(project_id, str) or not project_id.strip()):
        return product_http._json_response(400, {"detail": "project_id must be a non-empty string"}, product_http._no_store_headers())
    token = uuid.uuid4().hex
    turn_request = {
        "schema_version": "1.0.0",
        "turn_id": f"turn-{token}",
        "session_id": "workbench.compat",
        "operation_id": f"op-workbench-{token[:20]}",
        "idempotency_key": f"workbench-question-{token}",
        "scope": (
            {"kind": "project", "project_id": project_id.strip(), "series_id": None}
            if isinstance(project_id, str)
            else {"kind": "global", "project_id": None, "series_id": None}
        ),
        "input": {"kind": "text", "text": question.strip(), "refs": []},
        "desired_outcome": "workbench.question.answer",
        "privacy": {
            "mode": "remote_allowed",
            "allow_remote": True,
            "pii": "possible",
            "consent_refs": ["crp://default/consent/provider-egress-policy"],
            "retention": "local_durable",
        },
        "capability_policy": {
            "allowed": ["workbench.question.answer"],
            "denied": [],
            "require_approval": [],
        },
        "context_policy": {
            "include_project_skill": True,
            "include_memory": True,
            "include_session_history": False,
            "max_context_bytes": 262144,
        },
        "approval_policy": {"mode": "risk_based", "auto_approve_read_only": True},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        runtime = get_or_build_ai_runtime(request, container)
        receipt = runtime.submit_turn(turn_request)
        presentation = runtime.presentation_for(receipt.turn_id)
    except (KeyError, TypeError, ValueError) as error:
        status = 409 if "authority" in str(error).casefold() else 400
        return product_http._json_response(status, {"detail": "workbench direct question rejected", "reason": str(error)}, product_http._no_store_headers())
    if not isinstance(presentation, Mapping):
        return product_http._json_response(409, {"detail": "workbench direct question produced no presentation"}, product_http._no_store_headers())
    project_route = presentation.get("project_route")
    if isinstance(project_route, Mapping) and project_route.get("status") == "ambiguous":
        return product_http._json_response(409, {
            "detail": "project scope is ambiguous",
            "reason": project_route.get("reason_code"),
            "actionable": True,
            "candidates": list(project_route.get("candidates") or ()),
            "project_route": dict(project_route),
        }, product_http._no_store_headers())
    return product_http._json_response(200, dict(presentation), product_http._no_store_headers())


@router.post("/api/rebuild/workbench/project-skill-authoring-intent")
async def project_skill_authoring_intent(request: Request) -> JSONResponse:
    """Classify an explicit homepage Skill-authoring request without side effects."""
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping):
        return product_http._json_response(400, {"detail": "request body must be an object"}, product_http._no_store_headers())
    project_id = body.get("project_id", "default")
    question = body.get("question")
    try:
        intent = classify_project_skill_authoring_intent(
            project_id=project_id,
            question=question,
        )
    except ProjectSkillAuthoringIntentError as error:
        return product_http._json_response(
            400,
            {"detail": "project skill authoring intent rejected", "reason": str(error)},
            product_http._no_store_headers(),
        )
    return product_http._json_response(200, dict(intent.to_payload()), product_http._no_store_headers())
