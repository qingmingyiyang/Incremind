"""Source documents ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.ai_runtime import get_or_build_ai_runtime
from backend.api.container import ApiContainerDep
from backend.api.source_document_ai_runtime import (
    DOCUMENT_DRAFT_PROPOSE_CAPABILITY,
    SOURCE_DOCUMENT_DRAFT_OUTCOME,
    SOURCE_EVIDENCE_CAPABILITY,
)

from core.model_gateway import ObjectStoreModelRequestRepository, ObjectStoreModelResultRepository
from core.product_core.answer_model_request import CreateAnswerModelRequestFromRecallResult
from core.product_core.local_extractive_answer import CreateLocalExtractiveAnswerFromModelRequest
from core.product_core.local_extractive_answer_endpoint import ServeLocalExtractiveAnswerEndpoint
from core.product_core.model_result_document_handoff import (
    CreateDocumentFromModelResult,
    ModelResultDocumentHandoffError,
)
from core.product_core.source_content_qa_recall import CreateSourceContentQaRecall
from core.product_core.source_content_qa_recall_endpoint import ServeSourceContentQaRecallEndpoint
from core.product_core.source_template_document import (
    CreateMediaOutputTemplateDocument,
    CreateSourceTemplateDocument,
    SourceTemplateDocumentError,
    serialize_source_template_document_result,
)
from core.search_and_recall import ObjectStoreRecallRepository

from . import developer_logs as product_developer_logs
from . import developer_prompt_catalog as product_developer_prompt_catalog
from . import document_delivery_services as product_document_delivery_services
from . import document_templates as product_document_templates
from . import http as product_http
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.post("/api/rebuild/sources/{source_id:path}/template-document")
async def source_template_document(
    request: Request,
    container: ApiContainerDep,
    source_id: str,
) -> JSONResponse:
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    template_prompt_context = product_developer_prompt_catalog._developer_studio_prompts(
        store,
        ("pt-title", "pt-detail-summary", "pt-longterm-organize", "pt-output-validate"),
    )
    style_prefix = product_document_templates._style_prefix_for_source(store, source_id)
    outline_override = product_document_templates._outline_override_for_source(container.root_dir, store, settings, source_id)
    try:
        result = CreateSourceTemplateDocument(
            object_store=store,
            documents=product_repositories._document_repository(container.root_dir, store, settings),
            namespace_id=settings.namespace_id,
        ).execute(
            source_id=source_id,
            template_type=product_http._optional_body_str(body, "template_type") or "answer_manual",
            prompt_context=template_prompt_context,
            style_prefix=style_prefix,
            outline_override=outline_override,
        )
    except SourceTemplateDocumentError as error:
        status_code = 404 if str(error) == "source not found" else 400
        return product_http._json_response(
            status_code,
            {
                "detail": "source template document rejected",
                "reason": str(error),
                "actionable": True,
                "memory_publication_state": "not_published",
                "blocked_operations": [
                    "model_provider_execution",
                    "memory_candidate_auto_creation",
                    "long_term_memory_publication",
                ],
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        serialize_source_template_document_result(result),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.post("/api/rebuild/sources/{source_id:path}/provider-template-document")
async def provider_source_template_document(
    request: Request,
    container: ApiContainerDep,
    source_id: str,
) -> JSONResponse:
    """Legacy DTO adapter for the governed Source Document AI Turn workflow."""
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    source = store.read("sources", source_id)
    if source is None:
        return _source_document_ai_turn_failure(source_id, None, (), reason="source not found")
    template_type = product_http._optional_body_str(body, "template_type") or "answer_manual"
    project_id = str(source.get("project_id") or "default")
    token = uuid.uuid4().hex
    turn_request = {
        "schema_version": "1.0.0",
        "turn_id": f"turn-{token}",
        "session_id": f"source-document.{source_id}",
        "operation_id": f"op-source-document-{token[:20]}",
        "idempotency_key": f"source-document-draft-{token}",
        "scope": {"kind": "project", "project_id": project_id, "series_id": None},
        "input": {
            "kind": "references",
            "text": template_type,
            "refs": [{
                "kind": "source",
                "object_id": source_id,
                "uri": f"crp://{settings.namespace_id}/sources/{source_id}",
            }],
        },
        "desired_outcome": SOURCE_DOCUMENT_DRAFT_OUTCOME,
        "privacy": {
            "mode": "remote_allowed",
            "allow_remote": True,
            "pii": "possible",
            "consent_refs": ["crp://default/consent/provider-egress-policy"],
            "retention": "local_durable",
        },
        "capability_policy": {
            "allowed": [SOURCE_EVIDENCE_CAPABILITY, DOCUMENT_DRAFT_PROPOSE_CAPABILITY],
            "denied": [],
            "require_approval": [DOCUMENT_DRAFT_PROPOSE_CAPABILITY],
        },
        "context_policy": {
            "include_project_skill": True,
            "include_memory": False,
            "include_session_history": False,
            "max_context_bytes": 262144,
        },
        "approval_policy": {"mode": "explicit", "auto_approve_read_only": True},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        runtime = get_or_build_ai_runtime(request, container)
        waiting = runtime.submit_turn(turn_request)
        events = tuple(runtime.events_after(waiting.turn_id))
        if waiting.status == "failed":
            return _source_document_ai_turn_failure(source_id, waiting.turn_id, events)
        if waiting.status != "waiting_approval":
            return _source_document_ai_turn_failure(
                source_id, waiting.turn_id, events, reason="Source document AI Turn did not reach approval"
            )
        approval = next(event for event in reversed(events) if event.get("type") == "approval.required")
        completed = runtime.apply_action({
            "schema_version": "1.0.0",
            "action_id": f"action-{uuid.uuid4().hex}",
            "turn_id": waiting.turn_id,
            "type": "approve",
            "target_event_id": approval["event_id"],
            "reason": "legacy provider template request mapped to AI Turn approval",
            "actor": "user",
            "expected_sequence": waiting.current_sequence,
            "idempotency_key": f"approve-source-document-{token}",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        if completed.status == "failed":
            return _source_document_ai_turn_failure(
                source_id, completed.turn_id, tuple(runtime.events_after(completed.turn_id))
            )
        presentation = runtime.presentation_for(completed.turn_id)
    except (KeyError, StopIteration, TypeError, ValueError) as error:
        return _source_document_ai_turn_failure(source_id, None, (), reason=product_developer_logs._sanitize_dev_log_text(str(error)))
    if completed.status != "completed" or not isinstance(presentation, Mapping):
        return _source_document_ai_turn_failure(
            source_id, completed.turn_id, (), reason="Source document AI Turn did not produce a Document"
        )
    return product_http._json_response(200, {
        **dict(presentation),
        "turn_id": completed.turn_id,
        "operation_id": turn_request["operation_id"],
    }, product_http._no_store_headers())


def _source_document_ai_turn_failure(
    source_id: str,
    turn_id: str | None,
    events: Sequence[Mapping[str, object]],
    *,
    reason: str | None = None,
) -> JSONResponse:
    latest = events[-1] if events else {}
    data = latest.get("data") if isinstance(latest, Mapping) else None
    code = data.get("error_code") if isinstance(data, Mapping) else None
    stale = code == "ai.stale_baseline"
    missing = reason == "source not found"
    return product_http._json_response(404 if missing else 409 if stale else 400, {
        "detail": (
            "source not found"
            if missing
            else "provider source template document baseline is stale"
            if stale
            else "provider source template document rejected"
        ),
        "reason": "stale_baseline" if stale else reason or "ai_execution_failed",
        "source_id": source_id,
        "turn_id": turn_id,
        "actionable": True,
        "provider_enhanced": False,
        "key_material_returned": False,
        "request_payload_persisted": False,
        "memory_publication_state": "not_published",
        "blocked_operations": [
            "memory_candidate_auto_creation",
            "long_term_memory_publication",
            "provider_key_material_return",
        ],
        "next_action": "restart_from_current_source" if stale else "inspect_ai_turn_events",
    }, product_http._no_store_headers())


@router.post("/api/rebuild/media-processing-outputs/{output_id:path}/template-document")
async def media_output_template_document(
    request: Request,
    container: ApiContainerDep,
    output_id: str,
) -> JSONResponse:
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    template_prompt_context = product_developer_prompt_catalog._developer_studio_draft_prompts(
        store,
        ("pt-title", "pt-detail-summary", "pt-video-summary", "pt-longterm-organize", "pt-output-validate"),
    )
    style_prefix = product_document_templates._style_prefix_for_media_output(store, output_id)
    outline_override = product_document_templates._outline_override_for_media_output(container.root_dir, store, settings, output_id)
    try:
        result = CreateMediaOutputTemplateDocument(
            object_store=store,
            documents=product_repositories._document_repository(container.root_dir, store, settings),
            namespace_id=settings.namespace_id,
        ).execute(
            output_id=output_id,
            template_type=product_http._optional_body_str(body, "template_type") or "media_summary",
            prompt_context=template_prompt_context,
            style_prefix=style_prefix,
            outline_override=outline_override,
        )
    except SourceTemplateDocumentError as error:
        status_code = 404 if str(error) in {"media processing output not found", "source not found"} else 400
        return product_http._json_response(
            status_code,
            {
                "detail": "media output template document rejected",
                "reason": str(error),
                "actionable": True,
                "memory_publication_state": "not_published",
                "blocked_operations": [
                    "model_provider_execution",
                    "memory_candidate_auto_creation",
                    "long_term_memory_publication",
                ],
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        serialize_source_template_document_result(result),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.post("/api/rebuild/sources/{source_id:path}/qa-recall")
async def source_content_qa_recall(
    request: Request,
    container: ApiContainerDep,
    source_id: str,
) -> JSONResponse:
    _ = source_id
    store, settings = product_repositories._object_store(container.root_dir)
    recalls = ObjectStoreRecallRepository(store, namespace_id=settings.namespace_id)
    answer_requests = CreateAnswerModelRequestFromRecallResult(
        recalls=recalls,
        model_requests=ObjectStoreModelRequestRepository(store),
        namespace_id=settings.namespace_id,
    )
    use_case = CreateSourceContentQaRecall(
        object_store=store,
        recalls=recalls,
        answer_requests=answer_requests,
        namespace_id=settings.namespace_id,
    )
    response = ServeSourceContentQaRecallEndpoint().execute(
        method=request.method,
        path=product_document_delivery_services._path_with_query(request),
        body=await product_http._json_body(request),
        create_recall=use_case.execute,
    )
    return product_http._json_response(response.status_code, response.body, response.headers)


@router.post("/api/rebuild/model-requests/{model_request_id:path}/local-answer")
async def local_extractive_answer(
    request: Request,
    container: ApiContainerDep,
    model_request_id: str,
) -> JSONResponse:
    _ = model_request_id
    store, _settings = product_repositories._object_store(container.root_dir)
    use_case = CreateLocalExtractiveAnswerFromModelRequest(
        model_requests=ObjectStoreModelRequestRepository(store),
        model_results=ObjectStoreModelResultRepository(store),
    )
    response = ServeLocalExtractiveAnswerEndpoint().execute(
        method=request.method,
        path=product_document_delivery_services._path_with_query(request),
        body=await product_http._json_body(request),
        create_answer=use_case.execute,
    )
    return product_http._json_response(response.status_code, response.body, response.headers)


@router.post("/api/rebuild/model-results/{model_result_id:path}/document")
async def model_result_document_handoff(
    request: Request,
    container: ApiContainerDep,
    model_result_id: str,
) -> JSONResponse:
    _ = model_result_id
    store, _settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    title = product_http._optional_body_str(body, "title") or "模型回答文档草稿"
    handoff = CreateDocumentFromModelResult(
        model_requests=ObjectStoreModelRequestRepository(store),
        model_results=ObjectStoreModelResultRepository(store),
        documents=product_repositories._document_repository(container.root_dir, store, _settings),
    )
    try:
        result = handoff.execute(model_result_id, title=title)
    except ModelResultDocumentHandoffError as error:
        return product_http._json_response(
            400,
            {"detail": "model result document handoff rejected", "reason": str(error), "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        {
            "status": "document_created",
            "project_id": result.project_id,
            "model_request_id": result.model_request_id,
            "model_result_id": result.model_result_id,
            "document_id": result.document_id,
            "document_revision": result.document_revision,
            "memory_publication_state": "not_published",
            "blocked_operations": ["long_term_memory_publication", "memory_candidate_auto_creation"],
        },
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )
