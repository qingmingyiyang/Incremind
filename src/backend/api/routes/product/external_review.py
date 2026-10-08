"""External review ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.external_apply_saga import (
    ExternalDocumentApplyConflict,
    ExternalDocumentApplyError,
    ExternalDocumentApplySagaService,
)
from backend.api.external_apply_startup import plan_external_document_apply
from backend.api.external_project_skill_apply_saga import (
    ExternalProjectSkillApplyConflict,
    ExternalProjectSkillApplyError,
    ExternalProjectSkillApplySagaService,
)
from backend.api.external_project_skill_apply_startup import plan_external_project_skill_apply
from backend.api.external_series_candidate_saga import (
    ExternalSeriesCandidateConflict,
    ExternalSeriesCandidateError,
    ExternalSeriesCandidateSagaService,
    SQLiteSeriesCurrentProjection,
)

from core.aggregate_repository_factory import (
    AggregateRepositoryFactory,
    AggregateRepositoryFactoryError,
    STRUCTURED_DATABASE_NAME,
)
from core.memory_core import MemoryReaderPort, ObjectStoreMemoryStore, SQLiteMemoryReader
from core.product_core.external_agent_proposal_import import (
    ExternalAgentProposalImportError,
    ImportExternalAgentProposal,
    serialize_external_agent_proposal_import,
)
from core.storage_provider import (
    JsonObjectStore,
    RebuildStorageSettings,
    SQLiteExternalApplySagaStore,
    SQLiteExternalProjectSkillApplySagaStore,
    SQLiteExternalSeriesCandidateSagaStore,
    SQLiteStructuredRecordStore,
)

from . import external_review_formats as product_external_review_formats
from . import http as product_http
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.post("/api/rebuild/external-agent/proposals")
async def external_agent_proposal_import(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    proposal = body.get("proposal")
    if not isinstance(proposal, Mapping):
        return product_http._json_response(
            400,
            {
                "detail": "external agent proposal rejected",
                "reason": "proposal object is required",
                "actionable": True,
                "memory_publication_state": "not_published",
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    try:
        result = ImportExternalAgentProposal(store, namespace_id=settings.namespace_id).execute(
            proposal=proposal,
            project_id=product_http._optional_body_str(body, "project_id"),
        )
    except ExternalAgentProposalImportError as error:
        return product_http._json_response(
            400,
            {
                "detail": "external agent proposal rejected",
                "reason": str(error),
                "actionable": True,
                "memory_publication_state": "not_published",
                "blocked_operations": [
                    "direct_long_term_memory_write",
                    "direct_project_skill_overwrite",
                    "staging_memory_write",
                    "automatic_memory_publication",
                    "provider_secret_request",
                    "cookie_request",
                    "remote_upload",
                ],
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        serialize_external_agent_proposal_import(result),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.get("/api/rebuild/external-agent/review-drafts")
async def external_agent_review_draft_list(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    status_filter = request.query_params.get("status")
    project_filter = request.query_params.get("project_id")
    drafts = [
        product_external_review_formats._serialize_external_agent_review_draft(draft)
        for draft in store.list("external_agent_review_drafts")
        if product_external_review_formats._external_agent_draft_matches(draft, status_filter=status_filter, project_filter=project_filter)
    ]
    drafts.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return product_http._json_response(
        200,
        {
            "status": "ready",
            "items": drafts,
            "count": len(drafts),
            "filters": {"status": status_filter, "project_id": project_filter},
            "read_only": True,
            "memory_publication_state": "not_published",
        },
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.get("/api/rebuild/external-agent/review-drafts/{draft_id:path}/preview")
async def external_agent_review_draft_preview(
    container: ApiContainerDep,
    draft_id: str,
) -> JSONResponse:
    store, settings = product_repositories._object_store(container.root_dir)
    draft = store.read("external_agent_review_drafts", draft_id)
    if draft is None:
        return product_http._json_response(
            404,
            {
                "detail": "external agent review draft not found",
                "draft_id": draft_id,
                "actionable": True,
                "read_only": True,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    memory: MemoryReaderPort = ObjectStoreMemoryStore(store)
    if draft.get("draft_type") == "series_update":
        try:
            resolution = AggregateRepositoryFactory(
                runtime_root=container.root_dir,
                namespace_id=settings.namespace_id,
                json_store=store,
            ).memory_publication_authority_resolution()
        except AggregateRepositoryFactoryError as error:
            return product_http._json_response(
                409,
                {
                    "detail": "external agent review draft preview rejected",
                    "reason": str(error),
                    "read_only": True,
                },
                product_http._no_store_headers(),
            )
        if resolution.records is not None:
            memory = SQLiteMemoryReader(resolution.records)
    return product_http._json_response(
        200,
        product_external_review_formats._serialize_external_agent_review_draft_preview(
            draft,
            documents=product_repositories._document_repository(container.root_dir, store, settings),
            skills=product_repositories._project_skill_repository(container.root_dir, store, settings),
            memory=memory,
        ),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.get("/api/rebuild/external-agent/review-drafts/{draft_id:path}")
async def external_agent_review_draft_detail(
    container: ApiContainerDep,
    draft_id: str,
) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    draft = store.read("external_agent_review_drafts", draft_id)
    if draft is None:
        return product_http._json_response(
            404,
            {
                "detail": "external agent review draft not found",
                "draft_id": draft_id,
                "actionable": True,
                "read_only": True,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        product_external_review_formats._serialize_external_agent_review_draft(draft),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.post("/api/rebuild/external-agent/review-drafts/{draft_id:path}/apply")
async def external_agent_review_draft_apply(
    request: Request,
    container: ApiContainerDep,
    draft_id: str,
) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    try:
        confirm = product_http._required_body_bool(body, "confirm")
    except ValueError as error:
        return product_http._json_response(
            400,
            {"detail": "external agent review draft apply rejected", "reason": str(error), "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    if confirm is not True:
        return product_http._json_response(
            400,
            {
                "detail": "external agent review draft apply rejected",
                "reason": "confirm must be true",
                "actionable": True,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    expected_revision = product_http._optional_body_int(body, "expected_revision")
    if expected_revision is None:
        return product_http._json_response(
            400,
            {
                "detail": "external agent review draft apply rejected",
                "reason": "expected_revision is required",
                "actionable": True,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    draft = store.read("external_agent_review_drafts", draft_id)
    if draft is None:
        return product_http._json_response(
            404,
            {"detail": "external agent review draft not found", "draft_id": draft_id, "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    draft_type = draft.get("draft_type")
    if draft_type == "series_update":
        return _apply_external_agent_series_draft(
            container.root_dir,
            store,
            _settings,
            draft_id=draft_id,
            draft=draft,
            expected_revision=expected_revision,
        )
    if draft_type == "project_skill_update":
        return _apply_external_agent_project_skill_draft(
            container.root_dir,
            store,
            _settings,
            request.app.state.effect_runtime,
            draft_id=draft_id,
            draft=draft,
            expected_revision=expected_revision,
        )
    if draft_type != "document_revision":
        return product_http._json_response(
            400,
            {
                "detail": "external agent review draft apply rejected",
                "reason": "only document_revision, project_skill_update, or series_update drafts can be applied",
                "draft_type": draft_type,
                "actionable": True,
                "memory_publication_state": "not_published",
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    operations = SQLiteExternalApplySagaStore(
        SQLiteStructuredRecordStore(
            container.root_dir / ".rebuild-data" / STRUCTURED_DATABASE_NAME,
        )
    )
    application = draft.get("application") if isinstance(draft.get("application"), Mapping) else {}
    recoverable_operation = operations.get(draft_id) if draft_type == "document_revision" else None
    recoverable_document_apply = (
        draft_type == "document_revision"
        and draft.get("status") == "applied"
        and application.get("operation_id") == draft_id
        and recoverable_operation is not None
        and recoverable_operation.state == "document_applied"
    )
    if draft.get("status") != "pending_review" and not recoverable_document_apply:
        return product_http._json_response(
            409,
            {
                "detail": "external agent review draft apply rejected",
                "reason": "draft is not pending_review",
                "draft_id": draft_id,
                "actionable": True,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    document_id = draft.get("target_id")
    markdown = draft.get("proposed_content")
    if not isinstance(document_id, str) or not document_id or not isinstance(markdown, str) or not markdown:
        return product_http._json_response(
            400,
            {
                "detail": "external agent review draft apply rejected",
                "reason": "document_revision draft requires target_id and proposed_content",
                "actionable": True,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    document_resolution = product_repositories._document_repository_resolution(container.root_dir, store, _settings)
    documents = document_resolution.repository
    try:
        service = ExternalDocumentApplySagaService(
            documents=documents,
            drafts=store,
            operations=operations,
            namespace_id=_settings.namespace_id,
            document_authority_identity=document_resolution.authority_identity,
        )
        effect_id = plan_external_document_apply(
            service,
            request.app.state.effect_runtime.log,
            draft_id,
            expected_revision=expected_revision,
        )
        request.app.state.effect_runtime.dispatch_operation(effect_id, now=int(time.time()))
        operation = operations.get(draft_id)
        if operation is None or operation.state != "finalized" or operation.applied_document_revision is None:
            raise ExternalDocumentApplyError("external apply operation did not finalize")
    except ExternalDocumentApplyConflict as error:
        return product_http._json_response(
            409,
            {
                "detail": "document revision conflict",
                "reason": str(error),
                "actionable": True,
                "document_id": document_id,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    except ExternalDocumentApplyError as error:
        return product_http._json_response(
            400,
            {
                "detail": "external agent review draft apply rejected",
                "reason": str(error),
                "actionable": True,
                "document_id": document_id,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    updated = documents.read(document_id)
    return product_http._json_response(
        200,
        {
            "status": "applied",
            "draft_id": draft_id,
            "effect_id": effect_id,
            "draft_type": "document_revision",
            "document_id": document_id,
            "document_revision": operation.applied_document_revision,
            "document_status": updated.get("status") if updated is not None else None,
            "review_state": "applied",
            "application_state": "applied",
            "memory_publication_state": "not_published",
            "staging_memory_written": False,
            "long_term_memory_written": False,
        },
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


def _apply_external_agent_series_draft(
    runtime_root: Path,
    store: JsonObjectStore,
    settings: RebuildStorageSettings,
    *,
    draft_id: str,
    draft: Mapping[str, object],
    expected_revision: int,
) -> JSONResponse:
    operations = SQLiteExternalSeriesCandidateSagaStore(
        SQLiteStructuredRecordStore(runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    )
    application = draft.get("application") if isinstance(draft.get("application"), Mapping) else {}
    operation = operations.get(draft_id)
    recoverable = (
        draft.get("status") == "applied"
        and application.get("operation_id") == draft_id
        and operation is not None
        and operation.state == "candidate_created"
    )
    if draft.get("status") != "pending_review" and not recoverable:
        return product_http._json_response(
            409,
            {
                "detail": "external agent review draft apply rejected",
                "reason": "draft is not pending_review",
                "draft_id": draft_id,
                "actionable": True,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    try:
        resolution = AggregateRepositoryFactory(
            runtime_root=runtime_root, namespace_id=settings.namespace_id, json_store=store
        ).memory_publication_authority_resolution()
        current = SQLiteSeriesCurrentProjection(resolution.records) if resolution.records is not None else None
        result = ExternalSeriesCandidateSagaService(
            objects=store,
            operations=operations,
            namespace_id=settings.namespace_id,
            authority_identity=resolution.authority_identity,
            current=current,
        ).apply(draft_id, expected_object_revision=expected_revision)
    except (ExternalSeriesCandidateConflict, AggregateRepositoryFactoryError) as error:
        return product_http._json_response(
            409,
            {
                "detail": "series candidate revision conflict",
                "reason": str(error),
                "actionable": True,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    except ExternalSeriesCandidateError as error:
        return product_http._json_response(
            400,
            {
                "detail": "external agent review draft apply rejected",
                "reason": str(error),
                "actionable": True,
                "memory_publication_state": "not_published",
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        {
            "status": "candidate_created",
            "draft_id": draft_id,
            "draft_type": "series_update",
            "series_id": result.series_id,
            "series_memory_id": result.series_memory_id,
            "series_memory_revision": result.proposed_series_revision,
            "memory_candidate_id": result.candidate_id,
            "memory_candidate_revision": result.candidate_revision,
            "review_state": "pending_review",
            "application_state": "candidate_created",
            "memory_publication_state": "not_published",
            "staging_memory_written": False,
            "long_term_memory_written": False,
            "long_term_memory_write_reason": "external_series_update_requires_candidate_review",
            "next_action": "review_memory_candidate_then_confirm_staging_publication",
        },
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


def _apply_external_agent_project_skill_draft(
    runtime_root: Path,
    store: JsonObjectStore,
    settings: RebuildStorageSettings,
    effect_runtime,
    *,
    draft_id: str,
    draft: Mapping[str, object],
    expected_revision: int,
) -> JSONResponse:
    resolution = AggregateRepositoryFactory(
        runtime_root=runtime_root,
        namespace_id=settings.namespace_id,
        json_store=store,
    ).project_skill_repository_resolution()
    operations = SQLiteExternalProjectSkillApplySagaStore(
        SQLiteStructuredRecordStore(runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    )
    application = draft.get("application") if isinstance(draft.get("application"), Mapping) else {}
    operation = operations.get(draft_id)
    recoverable = (
        draft.get("status") == "applied"
        and application.get("operation_id") == draft_id
        and operation is not None
        and operation.state == "skill_applied"
    )
    if draft.get("status") != "pending_review" and not recoverable:
        return product_http._json_response(
            409,
            {
                "detail": "external agent review draft apply rejected",
                "reason": "draft is not pending_review",
                "draft_id": draft_id,
                "actionable": True,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    try:
        service = ExternalProjectSkillApplySagaService(
            skills=resolution.repository,
            drafts=store,
            operations=operations,
            namespace_id=settings.namespace_id,
            project_skill_authority_identity=resolution.authority_identity,
        )
        effect_id = plan_external_project_skill_apply(
            service,
            effect_runtime.log,
            draft_id,
            expected_revision=expected_revision,
        )
        effect_runtime.dispatch_operation(effect_id, now=int(time.time()))
        operation = operations.get(draft_id)
        if operation is None or operation.state != "finalized" or operation.applied_skill_revision is None:
            raise ExternalProjectSkillApplyError("project skill apply operation did not finalize")
    except ExternalProjectSkillApplyConflict as error:
        return product_http._json_response(
            409,
            {
                "detail": "project skill revision conflict",
                "reason": str(error),
                "actionable": True,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    except ExternalProjectSkillApplyError as error:
        return product_http._json_response(
            400,
            {
                "detail": "external agent review draft apply rejected",
                "reason": str(error),
                "actionable": True,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    evidence = operation.evidence
    updated = resolution.repository.load(evidence.project_id)
    return product_http._json_response(
        200,
        {
            "status": "applied",
            "draft_id": draft_id,
            "effect_id": effect_id,
            "draft_type": "project_skill_update",
            "project_id": evidence.project_id,
            "project_skill_id": evidence.project_skill_id,
            "project_skill_revision": operation.applied_skill_revision,
            "project_skill_status": updated.get("status") if updated is not None else None,
            "review_state": "applied",
            "application_state": "applied",
            "memory_publication_state": "not_published",
            "staging_memory_written": False,
            "long_term_memory_written": False,
        },
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )
