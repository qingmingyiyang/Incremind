"""Memory candidates ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.ai_runtime import get_or_build_ai_runtime
from backend.api.container import ApiContainerDep
from backend.api.four_layer_memory_candidate_ai_runtime import (
    FOUR_LAYER_MEMORY_CANDIDATE_OUTCOME,
    FOUR_LAYER_MEMORY_EVIDENCE_CAPABILITY,
    FOUR_LAYER_MEMORY_PROPOSE_CAPABILITY,
    FourLayerMemoryCandidateEvidenceGrantStore,
)

from core.aggregate_repository_factory import (
    AggregateRepositoryFactoryError,
    STRUCTURED_DATABASE_NAME,
)
from core.memory_core import ObjectStoreMemoryCandidateRepository, ObjectStoreMemoryStore
from core.model_gateway import ObjectStoreModelRequestRepository, ObjectStoreModelResultRepository
from core.product_core.auto_memory_publication import (
    AutoMemoryPublicationError,
    AutoPublishMemoryCandidate,
    serialize_auto_memory_publication_result,
)
from core.product_core.memory_candidate_review import (
    MemoryCandidateReviewError,
    MemoryCandidateReviewResult,
    ReviewMemoryCandidate,
)
from core.product_core.memory_candidate_review_endpoint import ServeMemoryCandidateReviewEndpoint
from core.product_core.memory_publication_review_staging_saga_service import (
    MemoryPublicationReviewStagingSagaService,
    MemoryPublicationReviewStagingServiceConflict,
    MemoryPublicationReviewStagingServiceError,
)
from core.product_core.model_result_memory_candidate import (
    CreateMemoryCandidateFromModelResult,
    ModelResultMemoryCandidateError,
)
from core.product_core.source_template_memory_candidate import (
    CreateMemoryCandidateFromSourceTemplateDocument,
    SourceTemplateMemoryCandidateError,
    serialize_source_template_memory_candidate_result,
)
from core.project_skill_core import (
    ProjectSkillReviewStagingSagaService,
    ProjectSkillReviewStagingServiceError,
)
from core.storage_provider import (
    SQLiteMemoryPublicationReviewStagingSagaStore,
    SQLiteProjectSkillReviewStagingSagaStore,
    SQLiteStructuredRecordStore,
)

from . import document_delivery_services as product_document_delivery_services
from . import document_visibility as product_document_visibility
from . import http as product_http
from . import memory_hierarchy as product_memory_hierarchy
from . import memory_import_review as product_memory_import_review
from . import memory_publication as product_memory_publication
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.post("/api/rebuild/model-results/{model_result_id:path}/memory-candidate")
async def model_result_memory_candidate_handoff(
    request: Request,
    container: ApiContainerDep,
    model_result_id: str,
) -> JSONResponse:
    _ = model_result_id
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    handoff = CreateMemoryCandidateFromModelResult(
        model_requests=ObjectStoreModelRequestRepository(store),
        model_results=ObjectStoreModelResultRepository(store),
        candidates=ObjectStoreMemoryCandidateRepository(store),
        namespace_id=settings.namespace_id,
    )
    try:
        result = handoff.execute(
            model_result_id,
            target_layer=product_http._optional_body_str(body, "target_layer") or "atom",
            candidate_type=product_http._optional_body_str(body, "candidate_type") or "answer_fact",
        )
    except ModelResultMemoryCandidateError as error:
        return product_http._json_response(
            400,
            {"detail": "model result memory candidate rejected", "reason": str(error), "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        {
            "status": "candidate_created",
            "project_id": result.project_id,
            "model_request_id": result.model_request_id,
            "model_result_id": result.model_result_id,
            "candidate_id": result.candidate_id,
            "candidate_status": result.status,
            "target_layer": result.target_layer,
            "memory_publication_state": "not_published",
            "blocked_operations": ["auto_promote_memory", "long_term_memory_publication"],
        },
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.post("/api/rebuild/documents/{document_id:path}/template-memory-candidate")
async def source_template_memory_candidate(
    request: Request,
    container: ApiContainerDep,
    document_id: str,
) -> JSONResponse:
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    project_id = product_document_visibility._document_request_project(request, body)
    documents = product_repositories._document_repository(container.root_dir, store, settings)
    document = documents.read(document_id)
    if not product_document_visibility._document_visible_in_project(documents, document, project_id):
        return product_document_visibility._document_not_found(document_id)
    if (isinstance(document, Mapping)
            and product_document_visibility._candidate_uses_source(document, product_document_visibility._workspace_review_sources(Path(container.root_dir)))):
        return product_http._json_response(409, {"detail": "candidate creation moved", "reason": "review in Workspace and Recognition"}, product_http._no_store_headers())
    expected_revision = product_http._optional_body_int(body, "document_revision")
    if expected_revision is None:
        expected_revision = product_http._optional_body_int(body, "expected_revision")
    if expected_revision is None:
        return product_http._json_response(
            400,
            {
                "detail": "source template memory candidate rejected",
                "reason": "document_revision is required",
                "actionable": True,
                "memory_publication_state": "not_published",
                "blocked_operations": ["auto_promote_memory", "long_term_memory_publication"],
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    try:
        result = CreateMemoryCandidateFromSourceTemplateDocument(
            documents=product_repositories._document_repository(container.root_dir, store, settings),
            candidates=ObjectStoreMemoryCandidateRepository(store),
            namespace_id=settings.namespace_id,
        ).execute(
            document_id,
            expected_revision=expected_revision,
            target_layer=product_http._optional_body_str(body, "target_layer"),
            candidate_type=product_http._optional_body_str(body, "candidate_type"),
        )
    except SourceTemplateMemoryCandidateError as error:
        status_code = 404 if "not found" in str(error) else 400
        return product_http._json_response(
            status_code,
            {
                "detail": "source template memory candidate rejected",
                "reason": str(error),
                "actionable": True,
                "memory_publication_state": "not_published",
                "blocked_operations": ["auto_promote_memory", "long_term_memory_publication"],
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        serialize_source_template_memory_candidate_result(result),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.post("/api/rebuild/sources/{source_id:path}/four-layer-candidates")
async def four_layer_memory_provider_candidates(
    request: Request,
    container: ApiContainerDep,
    source_id: str,
) -> JSONResponse:
    if source_id in product_document_visibility._workspace_review_sources(Path(container.root_dir)):
        return product_http._json_response(409, {"detail": "candidate creation moved", "reason": "review in Workspace and Recognition"}, product_http._no_store_headers())
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping):
        return product_http._json_response(400, {"detail": "request body must be a JSON object"})
    request_id = body.get("request_id")
    project_id = body.get("project_id", "default")
    if body.get("confirm_egress") is False:
        return product_http._json_response(400, {"detail": "memory candidate proposal requires explicit confirmation"})
    if not isinstance(project_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", project_id) is None:
        return product_http._json_response(400, {"detail": "project_id is invalid"})
    evidence_kind = body.get("evidence_kind", "source_content_read")
    evidence_id = body.get("evidence_id")
    if evidence_kind == "source_content_read" and evidence_id is None:
        evidence_id = f"content-read-{source_id}"
    if evidence_kind not in {"source_content_read", "media_processing_output"} or not isinstance(evidence_id, str) or not evidence_id.strip():
        return product_http._json_response(400, {"detail": "memory candidate evidence is invalid"})
    if request_id is None:
        request_id = re.sub(r"[^A-Za-z0-9_-]", "-", evidence_id)[-47:].strip("-") or "memory-candidate"
    if not isinstance(request_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", request_id) is None:
        return product_http._json_response(400, {"detail": "request_id is invalid for memory candidate proposal"})
    allowed_layers = body.get("allowed_layers", ["atom", "scenario", "series", "project_skill"])
    if not isinstance(allowed_layers, list):
        return product_http._json_response(400, {"detail": "allowed_layers must be a list"})
    grant_store = _memory_candidate_grant_store(request)
    grant_id = f"memory-evidence-grant-{project_id}-{request_id}"
    try:
        grant_store.issue(
            source_id=source_id, evidence_kind=evidence_kind, evidence_id=evidence_id,
            allowed_layers=allowed_layers, project_id=project_id, grant_id=grant_id,
        )
        runtime = get_or_build_ai_runtime(request, container)
        metadata = getattr(runtime, "composition_metadata", {})
        if not isinstance(metadata, Mapping) or metadata.get("memory_candidate_remote_usable") is not True:
            return product_http._json_response(409, {"detail": "memory candidate provider is unavailable", "actionable": True})
        turn = _memory_candidate_turn_request(project_id, request_id, grant_id)
        waiting = runtime.submit_turn(turn)
        if waiting.status == "waiting_approval":
            approval = next(event for event in reversed(tuple(runtime.events_after(waiting.turn_id))) if event.get("type") == "approval.required")
            waiting = runtime.apply_action(_memory_candidate_approval_action(waiting, approval, turn))
        if waiting.status != "completed":
            return product_http._json_response(409, {"detail": "memory candidate proposal failed", "actionable": True})
        presentation = runtime.presentation_for(waiting.turn_id)
        if not isinstance(presentation, Mapping):
            return product_http._json_response(409, {"detail": "memory candidate proposal receipt is unavailable", "actionable": True})
        return product_http._json_response(200, {
            "status": "provider_candidates_imported", "project_id": project_id, "source_id": source_id,
            "evidence_kind": evidence_kind, "provider_name": presentation.get("provider_id", "model-gateway"),
            "prompt_version": "four-layer-memory-candidate-v1", "prompt_contract_returned": False,
            "request_payload_persisted": False,
            "import_result": {
                "status": "candidates_imported", "candidate_ids": list(presentation.get("candidate_ids") or ()),
                "candidate_count": presentation.get("candidate_count", 0),
                "memory_publication_state": "candidates_created_not_published",
                "blocked_operations": ["long_term_memory_publication", "staging_memory_write", "auto_promote_memory", "provider_execution"],
            },
        })
    except (KeyError, StopIteration, TypeError, ValueError):
        return product_http._json_response(409, {"detail": "memory candidate proposal failed", "actionable": True})
    finally:
        grant_store.revoke(grant_id)


def _memory_candidate_grant_store(request: Request) -> FourLayerMemoryCandidateEvidenceGrantStore:
    store = getattr(request.app.state, "four_layer_memory_candidate_evidence_grant_store", None)
    if isinstance(store, FourLayerMemoryCandidateEvidenceGrantStore):
        return store
    store = FourLayerMemoryCandidateEvidenceGrantStore()
    request.app.state.four_layer_memory_candidate_evidence_grant_store = store
    return store


def _memory_candidate_turn_request(project_id: str, request_id: str, grant_id: str) -> dict[str, object]:
    identity = f"{project_id}-{request_id}"
    return {
        "schema_version": "1.0.0", "turn_id": f"turn-memory-candidate-{identity}",
        "session_id": f"session-memory-candidate-{identity}", "operation_id": request_id,
        "idempotency_key": f"memory-candidate-{identity}",
        "scope": {"kind": "project", "project_id": project_id, "series_id": None},
        "input": {"kind": "text", "text": "propose review-only memory candidates", "refs": [{"kind": "memory_evidence", "object_id": grant_id, "uri": f"crp://default/memory-candidate-evidence/{grant_id}"}]},
        "desired_outcome": FOUR_LAYER_MEMORY_CANDIDATE_OUTCOME,
        "privacy": {"mode": "remote_allowed", "allow_remote": True, "pii": "possible", "consent_refs": ["crp://default/consent/provider-egress-policy"], "retention": "local_durable"},
        "capability_policy": {"allowed": [FOUR_LAYER_MEMORY_EVIDENCE_CAPABILITY, FOUR_LAYER_MEMORY_PROPOSE_CAPABILITY], "denied": [], "require_approval": [FOUR_LAYER_MEMORY_PROPOSE_CAPABILITY]},
        "context_policy": {"include_project_skill": False, "include_memory": False, "include_session_history": False, "max_context_bytes": 8192},
        "approval_policy": {"mode": "explicit", "auto_approve_read_only": True},
        "created_at": "2026-08-23T00:00:00+00:00",
    }


def _memory_candidate_approval_action(waiting: object, approval: Mapping[str, object], turn: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "action_id": f"action-{turn['operation_id']}",
        "turn_id": str(getattr(waiting, "turn_id")), "type": "approve",
        "target_event_id": approval["event_id"], "reason": "user approved review-only memory candidate proposal",
        "actor": "user", "expected_sequence": int(getattr(waiting, "current_sequence")),
        "idempotency_key": f"approve-{turn['idempotency_key']}", "created_at": "2026-08-23T00:00:00+00:00",
    }


@router.get("/api/rebuild/memory-candidates/{candidate_id:path}/review")
@router.post("/api/rebuild/memory-candidates/{candidate_id:path}/review")
async def memory_candidate_review(
    request: Request,
    container: ApiContainerDep,
    candidate_id: str,
) -> JSONResponse:
    _ = candidate_id
    store, settings = product_repositories._object_store(container.root_dir)
    candidate = store.read("memory_candidates", candidate_id)
    if (isinstance(candidate, Mapping)
            and product_document_visibility._candidate_uses_source(candidate, product_document_visibility._workspace_review_sources(Path(container.root_dir)))):
        return product_http._json_response(
            404 if request.method == "GET" else 409,
            {"detail": "candidate review moved", "reason": "review in Workspace and Recognition"},
            product_http._no_store_headers(),
        )
    candidates = ObjectStoreMemoryCandidateRepository(store)
    reviewer = ReviewMemoryCandidate(candidates=candidates, memory=ObjectStoreMemoryStore(store), namespace_id=settings.namespace_id)
    records = SQLiteStructuredRecordStore(
        container.root_dir / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    )
    operations = SQLiteProjectSkillReviewStagingSagaStore(records)

    def promote_generic(
        candidate_id: str,
        *,
        target_layer: str,
        reason: str,
        reviewed_by: str,
        atom_type: str | None = None,
        tags: Sequence[str] = (),
        confidence: float = 0.7,
        series_id: str | None = None,
        scenario_ids: Sequence[str] = (),
        atom_ids: Sequence[str] = (),
    ) -> MemoryCandidateReviewResult:
        if reviewed_by != "user":
            raise MemoryCandidateReviewError("generic Memory review requires user")
        try:
            staging_records = product_memory_import_review._require_memory_publication_records(
                container=container,
                store=store,
                namespace_id=settings.namespace_id,
            )
        except AggregateRepositoryFactoryError as error:
            raise MemoryCandidateReviewError(str(error)) from error
        candidate = candidates.get(candidate_id)
        if candidate is None or candidate.get("target_layer") != target_layer:
            raise MemoryCandidateReviewError("Memory Candidate target_layer mismatch")
        product_memory_hierarchy._validate_memory_hierarchy_bindings(
            records=staging_records,
            candidates=candidates,
            candidate=candidate,
            target_layer=target_layer,
            series_id=series_id,
            atom_ids=atom_ids,
            scenario_ids=scenario_ids,
        )
        reviewed_at = datetime.now(timezone.utc).isoformat()
        if candidate.get("status") == "promoted":
            review = candidate.get("review") if isinstance(candidate.get("review"), Mapping) else {}
            reviewed_at = review.get("reviewed_at") if isinstance(review.get("reviewed_at"), str) else reviewed_at
            reason = review.get("reason") if isinstance(review.get("reason"), str) else reason
        try:
            operation = MemoryPublicationReviewStagingSagaService(
                store,
                staging_records,
                SQLiteMemoryPublicationReviewStagingSagaStore(staging_records),
            ).review_to_staging(
                candidate_id,
                review_reason=reason,
                reviewed_at=reviewed_at,
                atom_type=atom_type,
                tags=tags,
                confidence=confidence,
                series_id=series_id,
                scenario_ids=scenario_ids,
                atom_ids=atom_ids,
            )
        except (
            MemoryPublicationReviewStagingServiceConflict,
            MemoryPublicationReviewStagingServiceError,
        ) as error:
            raise MemoryCandidateReviewError(str(error)) from error
        return MemoryCandidateReviewResult(
            candidate_id=candidate_id,
            status="promoted",
            reviewed_by="user",
            reviewed_at=str(operation.context["reviewed_at"]),
            promoted_layer=target_layer,
            promoted_object_id=operation.evidence.draft_id,
        )

    def promote_to_atom(candidate_id: str, **kwargs) -> MemoryCandidateReviewResult:
        generic = promote_generic(candidate_id, target_layer="atom", **kwargs)
        return generic

    def promote_to_layer(candidate_id: str, *, target_layer: str, **kwargs) -> MemoryCandidateReviewResult:
        if target_layer in {"scenario", "series_memory"}:
            return promote_generic(candidate_id, target_layer=target_layer, **kwargs)
        raise MemoryCandidateReviewError(
            f"Memory Candidate promotion for layer {target_layer} requires its dedicated durable staging saga"
        )

    def promote_to_project_skill(candidate_id: str, *, reason: str, reviewed_by: str):
        if reviewed_by != "user":
            raise ProjectSkillReviewStagingServiceError("Project Skill review requires user")
        candidate = candidates.get(candidate_id)
        if candidate is None or candidate.get("target_layer") != "project_skill":
            raise ProjectSkillReviewStagingServiceError("Memory Candidate target_layer mismatch")
        reviewed_at = datetime.now(timezone.utc).isoformat()
        if candidate.get("status") == "promoted":
            review = candidate.get("review") if isinstance(candidate.get("review"), Mapping) else {}
            reviewed_at = review.get("reviewed_at") if isinstance(review.get("reviewed_at"), str) else reviewed_at
            reason = review.get("reason") if isinstance(review.get("reason"), str) else reason
        try:
            operation = ProjectSkillReviewStagingSagaService(store, records, operations).review_to_staging(
                candidate_id, review_reason=reason, reviewed_at=reviewed_at
            )
        except ProjectSkillReviewStagingServiceError as error:
            raise MemoryCandidateReviewError(str(error)) from error
        return MemoryCandidateReviewResult(
            candidate_id=candidate_id,
            status="promoted",
            reviewed_by="user",
            reviewed_at=str(operation.draft["reviewed_at"]),
            promoted_layer="project_skill",
            promoted_object_id=operation.evidence.draft_id,
        )
    response = ServeMemoryCandidateReviewEndpoint().execute(
        method=request.method,
        path=product_document_delivery_services._path_with_query(request),
        body=await product_http._json_body(request),
        get_candidate=candidates.get,
        reject_candidate=reviewer.reject,
        promote_to_atom=promote_to_atom,
        withdraw_candidate=reviewer.withdraw,
        promote_to_layer=promote_to_layer,
        promote_to_project_skill=promote_to_project_skill,
    )
    response_body = dict(response.body)
    if response.status_code == 200 and candidates.get(candidate_id) is not None:
        response_body["candidate_revision"] = store.revision("memory_candidates", candidate_id)
    return product_http._json_response(response.status_code, response_body, response.headers)


@router.post("/api/rebuild/memory-candidates/{candidate_id:path}/auto-publication")
async def memory_candidate_auto_publication(
    request: Request,
    container: ApiContainerDep,
    candidate_id: str,
) -> JSONResponse:
    store, settings = product_repositories._object_store(container.root_dir)
    candidate = store.read("memory_candidates", candidate_id)
    if (isinstance(candidate, Mapping)
            and product_document_visibility._candidate_uses_source(candidate, product_document_visibility._workspace_review_sources(Path(container.root_dir)))):
        return product_http._json_response(
            409,
            {"detail": "candidate publication moved", "reason": "review in Workspace and Recognition"},
            product_http._no_store_headers(),
        )
    body = await product_http._json_body(request)
    if isinstance(body, Mapping) and "command" in body:
        return product_http._json_response(
            400,
            {
                "detail": "memory candidate auto publication rejected",
                "reason": "memory candidate auto publication endpoint does not accept provider command",
                "actionable": True,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    try:
        result = AutoPublishMemoryCandidate(
            store,
            namespace_id=settings.namespace_id,
        ).execute(
            candidate_id=candidate_id,
            reason=product_http._optional_body_str(body, "reason"),
        )
    except AutoMemoryPublicationError as error:
        return product_http._json_response(
            400,
            {
                "detail": "memory candidate auto publication rejected",
                "reason": str(error),
                "actionable": True,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        serialize_auto_memory_publication_result(result),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.post("/api/rebuild/staging-atoms/{object_id:path}/publication")
@router.post("/api/rebuild/staging-scenarios/{object_id:path}/publication")
@router.post("/api/rebuild/staging-series-memory/{object_id:path}/publication")
@router.post("/api/rebuild/staging-project-skills/{object_id:path}/publication")
@router.post("/api/rebuild/memory-publications/{object_id:path}/rollback")
async def memory_publication(
    request: Request,
    container: ApiContainerDep,
    object_id: str,
) -> JSONResponse:
    store, settings = product_repositories._object_store(container.root_dir)
    path = product_document_delivery_services._path_with_query(request)
    if path.startswith("/api/rebuild/staging-project-skills/") or path.startswith(
        "/api/rebuild/memory-publications/memory-publication-project-skill-"
    ):
        return await product_memory_publication._project_skill_memory_publication(request, container.root_dir, store, settings, object_id)
    try:
        product_memory_import_review._require_memory_publication_records(
            container=container,
            store=store,
            namespace_id=settings.namespace_id,
        )
    except AggregateRepositoryFactoryError as error:
        return product_http._json_response(
            409,
            {"detail": "Memory publication rejected", "reason": str(error)},
            product_http._no_store_headers(),
        )
    return await product_memory_publication._sqlite_memory_publication(
        request,
        container.root_dir,
        settings,
        object_id,
    )
