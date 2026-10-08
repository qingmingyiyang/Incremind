"""Composition and HTTP bindings for the durable workspace domains."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from uuid import uuid4
from fastapi import APIRouter, FastAPI, File, Form, Request, UploadFile
from .processing_lease import ProcessingLease
from backend.recognition import RecognitionService
from backend.memory_app.workspace_confirmation import WorkspaceConfirmation
from backend.memory_app.legacy_intake_review import LegacyIntakeReview
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.security.audited_records import audit_records
from backend.security.user_context import json_attribution
from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import JsonObjectStore, SourceAssetRuntimeStore, SQLiteStructuredRecordStore
from .model_config import ModelConfiguration
from .workspace_contracts import _COLLECTION, _json
from .workspace_items import WorkspaceItems
from .workspace_intake import WorkspaceIntake
from .workspace_review import WorkspaceReview
from .workspace_query import WorkspaceQuery


@dataclass(frozen=True)
class WorkspaceDomains:
    items: WorkspaceItems
    intake: WorkspaceIntake
    review: WorkspaceReview
    query: WorkspaceQuery
    confirmations: WorkspaceConfirmation | None
    legacy_reviews: LegacyIntakeReview


def install_workspace_routes(
    application: FastAPI, *, runtime_root: Path, records: SQLiteStructuredRecordStore,
    models: ModelConfiguration, documents: SQLiteDocumentRepository, service: RecognitionService,
) -> WorkspaceDomains:
    """Register the local workspace API against the existing recognition stores."""
    root = runtime_root / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    lock = RLock()
    instance_id = "workspace-instance-" + uuid4().hex
    processing_lease = ProcessingLease(records, _COLLECTION, instance_id)
    confirmations = WorkspaceConfirmation(runtime_root, records, documents) if documents.namespace_id == "default" else None
    if documents.namespace_id == "default":
        legacy_records, legacy_documents = records, documents
    else:
        legacy_records = SQLiteStructuredRecordStore(runtime_root / ".rebuild-data" / "structured-records.sqlite3")
        legacy_records = audit_records(legacy_records, runtime_root, 'default')
        object_store, settings = build_rebuild_object_store(runtime_root)
        legacy_documents = AggregateRepositoryFactory(
            runtime_root=runtime_root, namespace_id=settings.namespace_id, json_store=object_store,
        ).document_repository()
        if isinstance(legacy_documents, SQLiteDocumentRepository):
            legacy_documents.records = audit_records(legacy_documents.records, runtime_root, settings.namespace_id)
    legacy_reviews = LegacyIntakeReview(runtime_root, legacy_records, legacy_documents)
    source_store = SourceAssetRuntimeStore(
        json_store=JsonObjectStore(runtime_root / ".rebuild-data", legacy_root=runtime_root / "library",
                                    namespace_id=documents.namespace_id,
                                    mutation_attribution=json_attribution(runtime_root, documents.namespace_id),
                                    vector_cache_path=records.database_path.parent / 'recognition-vectors.sqlite3'),
        sqlite_records=None, library_root=runtime_root / "library", authority_identity="json",
    )
    application.state.workspace_confirmation_recovery_failures = (
        confirmations.recover_pending() if confirmations is not None else ()
    )
    recovery_report = legacy_reviews.recover_confirming_report()
    application.state.legacy_review_recovery_report = recovery_report
    application.state.legacy_review_recovery_failures = recovery_report["failures"]
    router = APIRouter(prefix="/api/workspace/v1")
    # Recovery never replays model or ASR calls. A valid lease owned by another
    # app instance remains active even when it shares this process PID.
    processing_lease.recover_expired()


    items = WorkspaceItems(records, processing_lease, lock)
    server_context = getattr(application.state, 'server_context', None)
    admission = (lambda: server_context.jobs.intake(application.state.server_user_id)) if server_context is not None else None
    submitter = (lambda kind,callback: server_context.jobs.submit(application.state.server_user_id,kind,callback,retain_running=True)) if server_context is not None else None
    intake = WorkspaceIntake(runtime_root, items, models, admission=admission,job_submitter=submitter)
    review = WorkspaceReview(items, documents, service, confirmations, legacy_reviews, root)
    query = WorkspaceQuery(records, documents, source_store, models, service)
    from .kernel.answer_turns import ProductAnswerTurns
    query.answer_turns = ProductAnswerTurns(application, runtime_root, query)
    application.state.product_answer_turns = query.answer_turns
    application.router.add_event_handler("startup", query.answer_turns._runtime)

    @router.get('/items')
    def list_items(project_id: str = "default"):
        return items.list_items(project_id)

    @router.get('/legacy-reviews')
    def list_legacy_reviews(project_id: str = "default"):
        return review.list_legacy_reviews(project_id)

    @router.get('/legacy-reviews/{source_id}')
    def get_legacy_review(source_id: str, project_id: str = "default"):
        return review.get_legacy_review(source_id, project_id)

    @router.put('/legacy-reviews/{source_id}/draft')
    async def save_legacy_review_draft(source_id: str, request: Request):
        return await review.save_legacy_review_draft(source_id, await _json(request))

    @router.post('/legacy-reviews/{source_id}/confirm')
    async def confirm_legacy_review(source_id: str, request: Request):
        return await review.confirm_legacy_review(source_id, await _json(request))

    @router.post('/legacy-reviews/{source_id}/recognition')
    async def legacy_review_recognition(source_id: str, request: Request):
        return await review.legacy_review_recognition(source_id, await _json(request))

    @router.post('/items/text')
    async def add_text(request: Request):
        return await intake.add_text(await _json(request))

    @router.post('/items/link')
    async def add_link(request: Request):
        return await intake.add_link(await _json(request))

    @router.post('/items/file')
    async def add_file(project_id: str = Form("default"), file: UploadFile = File(...)):
        return await intake.add_file(project_id, file)

    @router.post('/items/{item_id}/process')
    async def process(item_id: str, request: Request):
        return await intake.process(item_id, await _json(request))

    @router.put('/items/{item_id}/draft')
    async def save_draft(item_id: str, request: Request):
        return await review.save_draft(item_id, await _json(request))

    @router.post('/items/{item_id}/confirm')
    async def confirm(item_id: str, request: Request):
        return await review.confirm(item_id, await _json(request))

    @router.get('/items/{item_id}/source')
    def source(item_id: str, project_id: str = "default"):
        return review.source(item_id, project_id)

    @router.get('/items/{item_id}/original')
    def original(item_id: str, project_id: str = "default"):
        return review.original(item_id, project_id)

    @router.get('/search')
    def search(project_id: str = "default", q: str = ""):
        return query.search(project_id, q)

    @router.post('/ask/preview')
    async def ask_preview(request: Request):
        return await query.ask_preview(await _json(request))

    @router.post('/ask')
    async def ask(request: Request):
        return await query.ask(await _json(request))

    @router.post('/items/{item_id}/recognition')
    async def recognition(item_id: str, request: Request):
        return await review.recognition(item_id, await _json(request))

    @router.post('/items/{item_id}/retry')
    async def retry(item_id: str, request: Request):
        return await items.retry(item_id, await _json(request))

    application.include_router(router)
    return WorkspaceDomains(items, intake, review, query, confirmations, legacy_reviews)
