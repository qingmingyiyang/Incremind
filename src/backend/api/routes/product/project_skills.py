"""Project skills ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json, uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.ai_runtime import get_or_build_ai_runtime
from backend.api.container import ApiContainerDep
from backend.api.project_skill_ai_runtime import (
    PROJECT_SKILL_DRAFT_OUTCOME,
    PROJECT_SKILL_EVIDENCE_CAPABILITY,
    PROJECT_SKILL_PROPOSE_CAPABILITY,
)

from core.product_core.outline import Outline, OutlineError
from core.product_core.project_skill_overview import GetProjectSkillOverview
from core.product_core.project_skill_overview_endpoint import ServeProjectSkillOverviewEndpoint
from core.project_skill_core import (
    ProjectSkillExpectedRevisionError,
    ProjectSkillRepositoryError,
    ProjectSkillUpdate,
)
from core.storage_provider import ObjectStoreRevisionError

from . import developer_logs as product_developer_logs
from . import document_delivery_services as product_document_delivery_services
from . import http as product_http
from . import project_skill_formats as product_project_skill_formats
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.get("/api/rebuild/project-skill/overview")
async def project_skill_overview(request: Request, container: ApiContainerDep) -> JSONResponse:
    """Phase 13 只读 Project Skill overview：返回状态/revision/source refs，不允许改写。"""
    store, settings = product_repositories._object_store(container.root_dir)
    skills = product_repositories._project_skill_repository(container.root_dir, store, settings)
    use_case = GetProjectSkillOverview(skills)
    response = ServeProjectSkillOverviewEndpoint().execute(
        method=request.method,
        path=product_document_delivery_services._path_with_query(request),
        get_project_skill_overview=use_case.execute,
    )
    return product_http._json_response(response.status_code, response.body, response.headers)


@router.get("/api/rebuild/projects/{project_id}/skill")
async def project_skill_detail(
    container: ApiContainerDep,
    project_id: str,
) -> JSONResponse:
    """读取 ProjectSkill 完整 JSON（含可选 outline 覆盖），供 Developer Studio 编辑器使用。"""
    if not project_id or not project_id.strip():
        return product_http._json_response(400, {"detail": "project_id cannot be empty"}, product_http._no_store_headers())
    store, settings = product_repositories._object_store(container.root_dir)
    skills = product_repositories._project_skill_repository(container.root_dir, store, settings)
    skill = skills.load(project_id)
    if skill is None:
        return product_http._json_response(
            404,
            {"detail": "project skill not found", "project_id": project_id},
            product_http._no_store_headers(),
        )
    markdown = skills.markdown(project_id) or ""
    payload = product_project_skill_formats._serialize_project_skill_for_editor(skill, markdown=markdown)
    payload["revision_history"] = [dict(item) for item in skills.revisions(project_id)]
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.post("/api/rebuild/projects/{project_id}/skill")
async def project_skill_create(
    request: Request,
    container: ApiContainerDep,
    project_id: str,
) -> JSONResponse:
    """Create revision 1 after an explicit user confirmation."""
    if not project_id or not project_id.strip():
        return product_http._json_response(400, {"detail": "project_id cannot be empty"}, product_http._no_store_headers())
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping) or body.get("confirm") is not True:
        return product_http._json_response(400, {"detail": "project skill creation requires confirm=true"}, product_http._no_store_headers())
    if body.get("expected_revision") != 0:
        return product_http._json_response(400, {"detail": "project skill creation requires expected_revision=0"}, product_http._no_store_headers())
    reason = product_http._optional_body_str(body, "reason")
    name = product_http._optional_body_str(body, "name")
    purpose = product_http._optional_body_str(body, "purpose")
    if reason is None or name is None or purpose is None:
        return product_http._json_response(400, {"detail": "name, purpose and reason are required"}, product_http._no_store_headers())
    try:
        outline_payload = body.get("outline")
        outline = [] if outline_payload in (None, []) else Outline.from_payload(outline_payload).to_payload()
    except OutlineError as error:
        return product_http._json_response(400, {"detail": "outline validation failed", "reason": str(error)}, product_http._no_store_headers())
    store, settings = product_repositories._object_store(container.root_dir)
    skills = product_repositories._project_skill_repository(container.root_dir, store, settings)
    if skills.load(project_id) is not None:
        return product_http._json_response(409, {"detail": "project skill already exists", "project_id": project_id}, product_http._no_store_headers())
    structured: dict[str, object] = {
        "project_id": project_id,
        "name": name,
        "purpose": purpose,
        "required_context": [],
        "output_rules": [],
        "style_preferences": {},
        "update_rules": {
            "patch_strategy": "patch_existing_first",
            "user_edit_policy": "user_wins",
            "allowed_auto_updates": [],
        },
        "source_refs": [],
        "evidence_refs": [],
        "conflict": {"status": "none", "conflict_refs": [], "resolution": None},
        "status": "active",
        "trust_status": "user_confirmed",
    }
    if outline:
        structured["outline"] = outline
    markdown = f"# {name}\n\n{purpose}\n"
    try:
        created = skills.save(ProjectSkillUpdate(
            project_id=project_id,
            markdown=markdown,
            structured=structured,
            expected_revision=0,
            reason=reason,
            transition_kind="user_edit",
            actor="user",
            confirmation_kind="direct_user_save",
        ))
    except ProjectSkillExpectedRevisionError as error:
        return product_http._json_response(409, {"detail": "project skill revision conflict", "reason": str(error)}, product_http._no_store_headers())
    except ProjectSkillRepositoryError as error:
        return product_http._json_response(400, {"detail": "project skill creation rejected", "reason": str(error)}, product_http._no_store_headers())
    payload = product_project_skill_formats._serialize_project_skill_for_editor(created, markdown=markdown)
    payload["revision_history"] = [dict(item) for item in skills.revisions(project_id)]
    payload["outline_status"] = "skill_created"
    return product_http._json_response(201, payload, product_http._no_store_headers())


@router.post("/api/rebuild/projects/{project_id}/skill/import-preview")
async def project_skill_import_preview(
    request: Request,
    container: ApiContainerDep,
    project_id: str,
) -> JSONResponse:
    """Validate one user-selected Markdown or JSON file without writing authority."""
    body = await product_http._json_body(request)
    if not project_id or not project_id.strip() or not isinstance(body, Mapping):
        return product_http._json_response(400, {"detail": "valid project_id and request body are required"}, product_http._no_store_headers())
    store, settings = product_repositories._object_store(container.root_dir)
    skills = product_repositories._project_skill_repository(container.root_dir, store, settings)
    current = skills.load(project_id)
    try:
        draft = product_project_skill_formats._project_skill_import_draft(project_id, body, current=current)
    except (ValueError, ProjectSkillRepositoryError, OutlineError) as error:
        return product_http._json_response(400, {"detail": "project skill import rejected", "reason": str(error)}, product_http._no_store_headers())
    return product_http._json_response(200, {
        "status": "preview_ready",
        "project_id": project_id,
        "expected_revision": 0 if current is None else current.get("revision"),
        "mode": "create" if current is None else "update",
        "format": draft["format"],
        "preview": {
            "name": draft["structured"]["name"],
            "purpose": draft["structured"]["purpose"],
            "outline": draft["structured"].get("outline", []),
            "markdown": draft["markdown"],
        },
        "writes_performed": False,
    }, product_http._no_store_headers())


@router.post("/api/rebuild/projects/{project_id}/skill/import")
async def project_skill_import_confirm(
    request: Request,
    container: ApiContainerDep,
    project_id: str,
) -> JSONResponse:
    """Re-validate and import after explicit user confirmation."""
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping) or body.get("confirm") is not True:
        return product_http._json_response(400, {"detail": "project skill import requires confirm=true"}, product_http._no_store_headers())
    expected_revision = body.get("expected_revision")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        return product_http._json_response(400, {"detail": "expected_revision must be a non-negative integer"}, product_http._no_store_headers())
    store, settings = product_repositories._object_store(container.root_dir)
    skills = product_repositories._project_skill_repository(container.root_dir, store, settings)
    current = skills.load(project_id)
    current_revision = 0 if current is None else current.get("revision")
    if current_revision != expected_revision:
        return product_http._json_response(409, {"detail": "project skill revision conflict", "expected_revision": expected_revision, "current_revision": current_revision}, product_http._no_store_headers())
    try:
        draft = product_project_skill_formats._project_skill_import_draft(project_id, body, current=current)
        imported = skills.save(ProjectSkillUpdate(
            project_id=project_id,
            markdown=str(draft["markdown"]),
            structured=draft["structured"],
            expected_revision=expected_revision,
            reason=f"用户确认导入{draft['format'].upper()}项目工作规则",
            transition_kind="external_proposal_apply",
            actor="user",
            confirmation_kind="external_review_confirmation",
        ))
    except ProjectSkillExpectedRevisionError as error:
        return product_http._json_response(409, {"detail": "project skill revision conflict", "reason": str(error)}, product_http._no_store_headers())
    except (ValueError, ProjectSkillRepositoryError, OutlineError) as error:
        return product_http._json_response(400, {"detail": "project skill import rejected", "reason": str(error)}, product_http._no_store_headers())
    payload = product_project_skill_formats._serialize_project_skill_for_editor(imported, markdown=str(draft["markdown"]))
    payload["revision_history"] = [dict(item) for item in skills.revisions(project_id)]
    payload["outline_status"] = "skill_imported"
    return product_http._json_response(200 if expected_revision else 201, payload, product_http._no_store_headers())


@router.post("/api/rebuild/projects/{project_id}/skill/ai-drafts")
async def project_skill_ai_draft_generate(
    request: Request,
    container: ApiContainerDep,
    project_id: str,
) -> JSONResponse:
    """Legacy DTO adapter for the governed Project Skill AI Turn workflow."""
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping) or body.get("provider_call_confirmed") is not True:
        return product_http._json_response(400, {"detail": "AI draft requires provider_call_confirmed=true", "provider_call_performed": False}, product_http._no_store_headers())
    goal = product_http._optional_body_str(body, "goal")
    if goal is None or len(goal) > 6000:
        return product_http._json_response(400, {"detail": "goal is required and must be at most 6000 characters", "provider_call_performed": False}, product_http._no_store_headers())
    token = uuid.uuid4().hex
    turn_request = {
        "schema_version": "1.0.0",
        "turn_id": f"turn-{token}",
        "session_id": f"project-skill.{project_id}",
        "operation_id": f"op-project-skill-{token[:20]}",
        "idempotency_key": f"project-skill-draft-{token}",
        "scope": {"kind": "project", "project_id": project_id, "series_id": None},
        "input": {"kind": "text", "text": goal, "refs": []},
        "desired_outcome": PROJECT_SKILL_DRAFT_OUTCOME,
        "privacy": {
            "mode": "remote_allowed",
            "allow_remote": True,
            "pii": "possible",
            "consent_refs": ["crp://default/consent/provider-egress-policy"],
            "retention": "local_durable",
        },
        "capability_policy": {
            "allowed": [PROJECT_SKILL_EVIDENCE_CAPABILITY, PROJECT_SKILL_PROPOSE_CAPABILITY],
            "denied": [],
            "require_approval": [PROJECT_SKILL_PROPOSE_CAPABILITY],
        },
        "context_policy": {
            "include_project_skill": True,
            "include_memory": True,
            "include_session_history": False,
            "max_context_bytes": 262144,
        },
        "approval_policy": {"mode": "explicit", "auto_approve_read_only": True},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        runtime = get_or_build_ai_runtime(request, container)
        waiting = runtime.submit_turn(turn_request)
        turn_events = tuple(runtime.events_after(waiting.turn_id))
        if waiting.status == "failed":
            return _project_skill_ai_turn_failure(project_id, waiting.turn_id, turn_events)
        if waiting.status != "waiting_approval":
            return product_http._json_response(409, {
                "detail": "Project Skill AI Turn did not reach approval",
                "turn_id": waiting.turn_id,
                "active_project_skill_changed": False,
            }, product_http._no_store_headers())
        approval = next(
            event
            for event in reversed(turn_events)
            if event.get("type") == "approval.required"
        )
        completed = runtime.apply_action({
            "schema_version": "1.0.0",
            "action_id": f"action-{uuid.uuid4().hex}",
            "turn_id": waiting.turn_id,
            "type": "approve",
            "target_event_id": approval["event_id"],
            "reason": "legacy provider_call_confirmed mapped to AI Turn approval",
            "actor": "user",
            "expected_sequence": waiting.current_sequence,
            "idempotency_key": f"approve-project-skill-{token}",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        if completed.status == "failed":
            return _project_skill_ai_turn_failure(
                project_id,
                completed.turn_id,
                tuple(runtime.events_after(completed.turn_id)),
            )
        presentation = runtime.presentation_for(completed.turn_id)
    except (KeyError, StopIteration, TypeError, ValueError) as error:
        reason = product_developer_logs._sanitize_dev_log_text(str(error))
        insufficient = "needs verified project evidence" in reason
        return product_http._json_response(409 if insufficient else 400, {
            "detail": "Project Skill AI draft needs verified project evidence" if insufficient else "Project Skill AI draft failed",
            "reason": "insufficient_evidence" if insufficient else reason,
            "project_id": project_id,
            "provider_call_performed": not insufficient,
            "active_project_skill_changed": False,
            "next_action": "add_or_confirm_project_evidence" if insufficient else "inspect_ai_turn_events",
        }, product_http._no_store_headers())
    if waiting.status != "waiting_approval" or completed.status != "completed" or not isinstance(presentation, Mapping):
        return product_http._json_response(409, {
            "detail": "Project Skill AI Turn did not produce a review candidate",
            "turn_id": waiting.turn_id,
            "active_project_skill_changed": False,
        }, product_http._no_store_headers())
    return product_http._json_response(200, {
        **dict(presentation),
        "turn_id": completed.turn_id,
        "operation_id": turn_request["operation_id"],
    }, product_http._no_store_headers())


def _project_skill_ai_turn_failure(
    project_id: str,
    turn_id: str,
    events: Sequence[Mapping[str, object]],
) -> JSONResponse:
    latest = events[-1] if events else {}
    data = latest.get("data") if isinstance(latest, Mapping) else None
    code = data.get("error_code") if isinstance(data, Mapping) else None
    insufficient = code == "ai.insufficient_evidence"
    stale = code == "ai.stale_baseline"
    return product_http._json_response(409 if insufficient or stale else 400, {
        "detail": (
            "Project Skill AI draft needs verified project evidence"
            if insufficient
            else "Project Skill AI draft baseline is stale"
            if stale
            else "Project Skill AI draft failed"
        ),
        "reason": "insufficient_evidence" if insufficient else "stale_baseline" if stale else "ai_execution_failed",
        "project_id": project_id,
        "turn_id": turn_id,
        "provider_call_performed": not insufficient,
        "active_project_skill_changed": False,
        "next_action": "add_or_confirm_project_evidence" if insufficient else "restart_from_current_project_skill" if stale else "inspect_ai_turn_events",
    }, product_http._no_store_headers())


@router.patch("/api/rebuild/projects/{project_id}/skill/ai-drafts/{candidate_id:path}")
async def project_skill_ai_draft_edit(
    request: Request,
    container: ApiContainerDep,
    project_id: str,
    candidate_id: str,
) -> JSONResponse:
    """Persist a user edit to a pending AI candidate without touching active Skill authority."""
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping) or body.get("confirm") is not True:
        return product_http._json_response(400, {"detail": "AI draft edit requires confirm=true", "active_project_skill_changed": False}, product_http._no_store_headers())
    expected_revision = body.get("expected_revision")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
        return product_http._json_response(400, {"detail": "expected_revision must be a positive integer", "active_project_skill_changed": False}, product_http._no_store_headers())
    draft_patch = body.get("draft")
    if not isinstance(draft_patch, Mapping):
        return product_http._json_response(400, {"detail": "draft must be an object", "active_project_skill_changed": False}, product_http._no_store_headers())
    store, _settings = product_repositories._object_store(container.root_dir)
    candidate = store.read("memory_candidates", candidate_id)
    if candidate is None or candidate.get("project_id") != project_id or candidate.get("target_layer") != "project_skill":
        return product_http._json_response(404, {"detail": "Project Skill AI candidate not found", "active_project_skill_changed": False}, product_http._no_store_headers())
    if candidate.get("status") != "pending_review":
        return product_http._json_response(409, {"detail": "only pending AI candidates can be edited", "candidate_status": candidate.get("status"), "active_project_skill_changed": False}, product_http._no_store_headers())
    existing_draft = candidate.get("project_skill_draft")
    if not isinstance(existing_draft, Mapping):
        return product_http._json_response(409, {"detail": "Project Skill AI candidate draft is invalid", "active_project_skill_changed": False}, product_http._no_store_headers())
    provenance = candidate.get("provenance")
    allowed_refs = provenance.get("project_evidence_refs") if isinstance(provenance, Mapping) else None
    if not isinstance(allowed_refs, list) or not allowed_refs:
        return product_http._json_response(409, {"detail": "Project Skill AI candidate evidence is missing", "active_project_skill_changed": False}, product_http._no_store_headers())
    merged = dict(existing_draft)
    for key in ("name", "purpose", "output_rules", "style_preferences", "update_rules", "outline", "markdown"):
        if key in draft_patch:
            merged[key] = draft_patch[key]
    try:
        normalized = product_project_skill_formats._project_skill_import_draft(
            project_id,
            {"format": "json", "content": json.dumps(merged, ensure_ascii=False)},
            current=None,
        )
        structured = product_project_skill_formats._project_skill_ai_safe_structured(
            normalized["structured"],
            allowed_source_refs=allowed_refs,
        )
    except (ValueError, ProjectSkillRepositoryError, OutlineError) as error:
        return product_http._json_response(400, {"detail": "Project Skill AI draft edit rejected", "reason": str(error), "active_project_skill_changed": False}, product_http._no_store_headers())
    for rule in structured.get("output_rules", []):
        rule["origin"] = "user"
        rule["locked_by_user"] = True
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    updated = dict(candidate)
    updated["project_skill_draft"] = structured
    updated["proposed_content"] = str(structured["purpose"])
    updated["updated_at"] = timestamp
    history = list(candidate.get("edit_history")) if isinstance(candidate.get("edit_history"), list) else []
    history.append({"actor": "user", "edited_at": timestamp, "base_revision": expected_revision})
    updated["edit_history"] = history[-20:]
    try:
        store.write("memory_candidates", candidate_id, updated, expected_revision=expected_revision)
    except ObjectStoreRevisionError:
        return product_http._json_response(409, {
            "detail": "Project Skill AI candidate revision conflict",
            "expected_revision": expected_revision,
            "current_revision": store.revision("memory_candidates", candidate_id),
            "active_project_skill_changed": False,
        }, product_http._no_store_headers())
    return product_http._json_response(200, {
        "status": "pending_review",
        "candidate_id": candidate_id,
        "candidate_revision": store.revision("memory_candidates", candidate_id),
        "preview": {
            "name": structured["name"],
            "purpose": structured["purpose"],
            "outline": structured.get("outline", []),
            "output_rules": structured.get("output_rules", []),
        },
        "active_project_skill_changed": False,
    }, product_http._no_store_headers())


@router.post("/api/rebuild/projects/{project_id}/skill/ai-drafts/{candidate_id:path}/restore")
async def project_skill_ai_draft_restore(
    request: Request,
    container: ApiContainerDep,
    project_id: str,
    candidate_id: str,
) -> JSONResponse:
    """Restore a rejected AI candidate to pending review with explicit CAS."""
    body = await product_http._json_body(request)
    expected_revision = body.get("expected_revision") if isinstance(body, Mapping) else None
    if not isinstance(body, Mapping) or body.get("confirm") is not True or not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
        return product_http._json_response(400, {"detail": "restore requires confirm=true and a positive expected_revision", "active_project_skill_changed": False}, product_http._no_store_headers())
    store, _settings = product_repositories._object_store(container.root_dir)
    candidate = store.read("memory_candidates", candidate_id)
    if candidate is None or candidate.get("project_id") != project_id or candidate.get("target_layer") != "project_skill":
        return product_http._json_response(404, {"detail": "Project Skill AI candidate not found", "active_project_skill_changed": False}, product_http._no_store_headers())
    if candidate.get("status") != "rejected":
        return product_http._json_response(409, {"detail": "only rejected AI candidates can be restored", "candidate_status": candidate.get("status"), "active_project_skill_changed": False}, product_http._no_store_headers())
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    restored = dict(candidate)
    review = dict(candidate.get("review")) if isinstance(candidate.get("review"), Mapping) else {}
    review.update({"reason": "用户恢复已拒绝的AI草稿以继续编辑", "reviewed_by": None, "reviewed_at": None})
    restored.update({"status": "pending_review", "review": review, "updated_at": timestamp})
    history = list(candidate.get("lifecycle_history")) if isinstance(candidate.get("lifecycle_history"), list) else []
    history.append({"transition": "rejected_to_pending_review", "actor": "user", "created_at": timestamp, "base_revision": expected_revision})
    restored["lifecycle_history"] = history[-20:]
    try:
        store.write("memory_candidates", candidate_id, restored, expected_revision=expected_revision)
    except ObjectStoreRevisionError:
        return product_http._json_response(409, {
            "detail": "Project Skill AI candidate revision conflict",
            "expected_revision": expected_revision,
            "current_revision": store.revision("memory_candidates", candidate_id),
            "active_project_skill_changed": False,
        }, product_http._no_store_headers())
    draft = restored.get("project_skill_draft") if isinstance(restored.get("project_skill_draft"), Mapping) else {}
    return product_http._json_response(200, {
        "status": "pending_review",
        "candidate_id": candidate_id,
        "candidate_revision": store.revision("memory_candidates", candidate_id),
        "preview": {
            "name": draft.get("name"), "purpose": draft.get("purpose"),
            "outline": draft.get("outline", []), "output_rules": draft.get("output_rules", []),
        },
        "active_project_skill_changed": False,
    }, product_http._no_store_headers())


@router.put("/api/rebuild/projects/{project_id}/skill/outline")
async def project_skill_outline_update(
    request: Request,
    container: ApiContainerDep,
    project_id: str,
) -> JSONResponse:
    """更新 ProjectSkill 的 outline 覆盖字段。

    请求体：{ outline: [...], expected_revision: int, reason?: str }
    - outline 为空数组时表示清除项目级覆盖（回落到模板默认）
    - outline 非空时使用 Outline.from_payload 校验并归一化
    - 其它字段保持不变，仅写入 outline + revision +1
    """
    if not project_id or not project_id.strip():
        return product_http._json_response(400, {"detail": "project_id cannot be empty"}, product_http._no_store_headers())
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping):
        return product_http._json_response(400, {"detail": "request body must be an object"}, product_http._no_store_headers())
    expected_revision = body.get("expected_revision")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
        return product_http._json_response(
            400,
            {"detail": "expected_revision is required and must be an integer"},
            product_http._no_store_headers(),
        )
    outline_payload = body.get("outline")
    normalized_outline: list[dict[str, object]]
    try:
        if outline_payload is None or (isinstance(outline_payload, list) and len(outline_payload) == 0):
            normalized_outline = []
        else:
            normalized_outline = Outline.from_payload(outline_payload).to_payload()
    except OutlineError as error:
        return product_http._json_response(
            400,
            {"detail": "outline validation failed", "reason": str(error)},
            product_http._no_store_headers(),
        )
    reason = product_http._optional_body_str(body, "reason")
    if reason is None:
        return product_http._json_response(400, {"detail": "reason is required"}, product_http._no_store_headers())
    store, settings = product_repositories._object_store(container.root_dir)
    skills = product_repositories._project_skill_repository(container.root_dir, store, settings)
    current = skills.load(project_id)
    if current is None:
        return product_http._json_response(
            404,
            {"detail": "project skill not found", "project_id": project_id},
            product_http._no_store_headers(),
        )
    structured = dict(current)
    if normalized_outline:
        structured["outline"] = normalized_outline
    else:
        structured.pop("outline", None)
    markdown = skills.markdown(project_id) or product_project_skill_formats._project_skill_markdown_fallback(current)
    try:
        updated = skills.save(
            ProjectSkillUpdate(
                project_id=project_id,
                markdown=markdown,
                structured=structured,
                expected_revision=expected_revision,
                reason=reason,
                transition_kind="user_edit",
                actor="user",
                confirmation_kind="direct_user_save",
            )
        )
    except ProjectSkillExpectedRevisionError as error:
        return product_http._json_response(
            409,
            {"detail": "project skill revision conflict", "reason": str(error)},
            product_http._no_store_headers(),
        )
    except ProjectSkillRepositoryError as error:
        return product_http._json_response(
            400,
            {"detail": "project skill update rejected", "reason": str(error)},
            product_http._no_store_headers(),
        )
    payload = product_project_skill_formats._serialize_project_skill_for_editor(updated, markdown=markdown)
    payload["revision_history"] = [dict(item) for item in skills.revisions(project_id)]
    payload["outline_status"] = "outline_updated"
    return product_http._json_response(200, payload, product_http._no_store_headers())


@router.post("/api/rebuild/projects/{project_id}/skill/rollback")
async def project_skill_direct_rollback(
    request: Request,
    container: ApiContainerDep,
    project_id: str,
) -> JSONResponse:
    if not project_id or not project_id.strip():
        return product_http._json_response(400, {"detail": "project_id cannot be empty"}, product_http._no_store_headers())
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping) or body.get("confirm") is not True:
        return product_http._json_response(400, {"detail": "direct rollback requires confirm=true"}, product_http._no_store_headers())
    target_revision = body.get("target_revision")
    expected_revision = body.get("expected_revision")
    if (
        not isinstance(target_revision, int)
        or isinstance(target_revision, bool)
        or not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
    ):
        return product_http._json_response(400, {"detail": "target_revision and expected_revision must be integers"}, product_http._no_store_headers())
    reason = product_http._optional_body_str(body, "reason")
    if reason is None:
        return product_http._json_response(400, {"detail": "reason is required"}, product_http._no_store_headers())
    store, settings = product_repositories._object_store(container.root_dir)
    skills = product_repositories._project_skill_repository(container.root_dir, store, settings)
    try:
        updated = skills.rollback(
            project_id,
            target_revision=target_revision,
            expected_revision=expected_revision,
            reason=reason,
        )
    except ProjectSkillExpectedRevisionError as error:
        return product_http._json_response(409, {"detail": "project skill revision conflict", "reason": str(error)}, product_http._no_store_headers())
    except ProjectSkillRepositoryError as error:
        return product_http._json_response(400, {"detail": "project skill rollback rejected", "reason": str(error)}, product_http._no_store_headers())
    markdown = skills.markdown(project_id) or ""
    payload = product_project_skill_formats._serialize_project_skill_for_editor(updated, markdown=markdown)
    payload["revision_history"] = [dict(item) for item in skills.revisions(project_id)]
    payload["outline_status"] = "user_rollback"
    payload["restored_revision"] = target_revision
    return product_http._json_response(200, payload, product_http._no_store_headers())
