"""Memory import review ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep

from core.aggregate_repository_factory import (
    AggregateRepositoryFactory,
    AggregateRepositoryFactoryError,
)
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.product_core.external_persona_candidate_staging import (
    ExternalPersonaCandidateStagingError,
    StageExternalPersonaCandidate,
)
from core.product_core.memory_candidate_review import (
    MemoryCandidateReviewError,
    MemoryCandidateReviewResult,
)
from core.product_core.memory_publication_review_staging_saga_service import (
    MemoryPublicationReviewStagingSagaService,
    MemoryPublicationReviewStagingServiceConflict,
    MemoryPublicationReviewStagingServiceError,
)
from core.product_core.persona import ObjectStorePersonaRepository, PersonaConflictError
from core.storage_provider import (
    JsonObjectStore,
    SQLiteMemoryPublicationReviewStagingSagaStore,
    SQLiteStructuredRecordStore,
)

from . import document_visibility as product_document_visibility
from . import http as product_http
from . import library_persona as product_library_persona
from . import memory_hierarchy as product_memory_hierarchy
from . import memory_import_records as product_memory_import_records
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.post("/api/rebuild/memory/candidates/review")
async def memory_candidates_review(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    """候选确认 / 忽略 / 编辑 / 提升 L3 / 暂存 L4 Persona / 降级 L0。

    body: { candidate_id, action: confirm|ignore|edit|promote_l3|stage_l4_persona|demote_l0|assign_project, edits? }
    """
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping):
        return product_memory_import_records._stage16_error_response("candidate review rejected", "request body is required")
    candidate_id = str(body.get("candidate_id", "") or "")
    action = str(body.get("action", "") or "")
    edits = body.get("edits")
    if not candidate_id or not action:
        return product_memory_import_records._stage16_error_response(
            "candidate review rejected", "candidate_id and action are required"
        )
    if action not in (
        "confirm",
        "ignore",
        "edit",
        "promote_l3",
        "stage_l4_persona",
        "demote_l0",
        "assign_project",
    ):
        return product_memory_import_records._stage16_error_response(
            "candidate review rejected",
            "action must be one of "
            "confirm/ignore/edit/promote_l3/stage_l4_persona/demote_l0/assign_project, "
            f"got {action}",
        )

    candidate = product_memory_import_records._get_stage16_candidate(store, candidate_id)
    if candidate is None:
        return product_memory_import_records._stage16_error_response(
            "candidate review rejected", f"candidate {candidate_id} not found",
            status_code=404,
        )
    if product_document_visibility._candidate_uses_source(candidate, product_document_visibility._workspace_review_sources(Path(container.root_dir))):
        return product_memory_import_records._stage16_error_response(
            "candidate review moved", "review the source document and recognition candidate in Workspace",
            status_code=409,
        )
    if action == "assign_project":
        project_id = str(body.get("project_id", "") or "").strip()
        expected_revision = body.get("expected_revision")
        if body.get("confirm") is not True:
            return product_memory_import_records._stage16_error_response(
                "candidate project assignment rejected",
                "candidate project assignment requires confirm=true",
            )
        if not project_id:
            return product_memory_import_records._stage16_error_response(
                "candidate project assignment rejected",
                "project_id is required",
            )
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
            return product_memory_import_records._stage16_error_response(
                "candidate project assignment rejected",
                "expected_revision must be the current candidate revision",
            )
        if candidate.get("status") != "pending_review":
            return product_memory_import_records._stage16_error_response(
                "candidate project assignment rejected",
                "only pending_review candidates can receive a project assignment",
                status_code=409,
            )
        if not candidate.get("import_batch_id") or not candidate.get("portable_object_id"):
            return product_memory_import_records._stage16_error_response(
                "candidate project assignment rejected",
                "project assignment is limited to imported legacy package candidates",
                status_code=409,
            )
        existing_project = str(candidate.get("project_id", "") or "").strip()
        current_revision = store.revision(product_memory_import_records._MEMORY_CANDIDATES_COLLECTION, candidate_id)
        if existing_project:
            if existing_project != project_id:
                return product_memory_import_records._stage16_error_response(
                    "candidate project assignment conflicted",
                    "candidate already belongs to another project",
                    status_code=409,
                )
            response_candidate = product_memory_import_records._serialize_memory_candidate(candidate)
            return product_memory_import_records._stage16_ok_response({
                "candidate": response_candidate,
                "action": action,
                "status": "project_assigned",
                "candidate_revision": current_revision,
                "idempotent": True,
            })
        if project_id not in _known_memory_project_ids(store, exclude_candidate_id=candidate_id):
            return product_memory_import_records._stage16_error_response(
                "candidate project assignment rejected",
                "target project does not exist in the current Vault",
                status_code=409,
            )
        if current_revision != expected_revision:
            return product_memory_import_records._stage16_error_response(
                "candidate project assignment conflicted",
                "candidate revision changed before project assignment",
                status_code=409,
            )
        updated = dict(candidate)
        updated["project_id"] = project_id
        updated["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            store.write(
                product_memory_import_records._MEMORY_CANDIDATES_COLLECTION,
                candidate_id,
                updated,
                expected_revision=expected_revision,
            )
        except Exception:
            return product_memory_import_records._stage16_error_response(
                "candidate project assignment conflicted",
                "candidate changed while project assignment was being saved",
                status_code=409,
            )
        return product_memory_import_records._stage16_ok_response({
            "candidate": product_memory_import_records._serialize_memory_candidate(updated),
            "action": action,
            "status": "project_assigned",
            "candidate_revision": store.revision(
                product_memory_import_records._MEMORY_CANDIDATES_COLLECTION,
                candidate_id,
            ),
            "idempotent": False,
        })
    if action == "stage_l4_persona":
        scope = body.get("scope") or "global"
        expected_draft_revision = body.get("expected_draft_revision")
        expected_current_revision = body.get("expected_current_revision")
        if scope not in {"global", "series", "project"}:
            return product_memory_import_records._stage16_error_response(
                "candidate review rejected",
                "scope must be one of global|series|project",
            )
        if not all(
            isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
            for value in (expected_draft_revision, expected_current_revision)
        ):
            return product_memory_import_records._stage16_error_response(
                "candidate review rejected",
                "expected draft/current revisions are required",
            )
        repository = ObjectStorePersonaRepository(store)
        try:
            draft = StageExternalPersonaCandidate(
                repository,
                now=datetime.now(timezone.utc).isoformat(),
            ).execute(
                candidate,
                scope=str(scope),
                expected_draft_revision=int(expected_draft_revision),
                expected_current_revision=int(expected_current_revision),
            )
        except PersonaConflictError as error:
            return product_memory_import_records._stage16_error_response(
                "candidate review conflicted",
                str(error),
                status_code=409,
            )
        except ExternalPersonaCandidateStagingError as error:
            return product_memory_import_records._stage16_error_response(
                "candidate review rejected",
                str(error),
            )
        updated = {
            **dict(candidate),
            "status": "promoted_l4_draft",
            "layer": "L4",
            "target_layer": "persona",
            "review": {
                "status": "reviewed",
                "reviewer": "user",
                "comment": str(body.get("comment", "") or ""),
            },
            "promoted_object_id": draft.get("id"),
        }
        candidate_revision = store.revision(product_memory_import_records._MEMORY_CANDIDATES_COLLECTION, candidate_id)
        try:
            store.write(
                product_memory_import_records._MEMORY_CANDIDATES_COLLECTION,
                candidate_id,
                updated,
                expected_revision=candidate_revision,
            )
        except Exception:
            return product_memory_import_records._stage16_error_response(
                "candidate review conflicted",
                "candidate changed while L4 draft was being staged",
                status_code=409,
            )
        return product_memory_import_records._stage16_ok_response(
            {
                "candidate": product_memory_import_records._serialize_memory_candidate(updated),
                "action": action,
                "status": "promoted_l4_draft",
                "persona": product_library_persona._persona_management_payload(
                    repository,
                    str(scope),
                    digest=repository.review_digest(str(scope)),
                ),
            }
        )

    if action == "confirm" and candidate.get("target_layer") in {
        "atom",
        "scenario",
        "series_memory",
    }:
        try:
            promotion = _promote_imported_memory_candidate(
                container=container,
                store=store,
                namespace_id=settings.namespace_id,
                candidate_id=candidate_id,
                reason=str(body.get("comment", "") or "用户确认资产包导入候选进入 staging。"),
            )
        except MemoryCandidateReviewError as error:
            return product_memory_import_records._stage16_error_response(
                "candidate review rejected",
                str(error),
                status_code=409,
            )
        promoted = store.read(product_memory_import_records._MEMORY_CANDIDATES_COLLECTION, candidate_id) or candidate
        return product_memory_import_records._stage16_ok_response({
            "candidate": product_memory_import_records._serialize_memory_candidate(promoted),
            "action": action,
            "status": promotion.status,
            "promoted_layer": promotion.promoted_layer,
            "promoted_object_id": promotion.promoted_object_id,
            "publication_state": "staged_not_published",
        })

    new_status = {
        "confirm": "confirmed",
        "ignore": "ignored",
        "edit": "edited",
        "promote_l3": "promoted_l3",
        "stage_l4_persona": "promoted_l4_draft",
        "demote_l0": "demoted_l0",
    }[action]
    new_layer = candidate.get("layer", "L1")
    if action == "promote_l3":
        new_layer = "L3"
    elif action == "demote_l0":
        new_layer = "L0"

    updated = dict(candidate)
    updated["status"] = new_status
    updated["layer"] = new_layer
    if action == "edit" and isinstance(edits, Mapping):
        if "content" in edits:
            updated["content"] = edits["content"]
        if "summary" in edits:
            updated["summary"] = edits["summary"]
        if "tags" in edits:
            updated["tags"] = list(edits["tags"] or [])
    updated["review"] = {
        "status": "reviewed", "reviewer": "user", "comment": str(body.get("comment", "") or ""),
    }

    if not product_memory_import_records._update_stage16_candidate(store, updated):
        return product_memory_import_records._stage16_error_response(
            "candidate review rejected", "failed to update candidate"
        )

    return product_memory_import_records._stage16_ok_response({
        "candidate": product_memory_import_records._serialize_memory_candidate(updated),
        "action": action,
        "status": new_status,
    })


@router.get("/api/rebuild/memory/project-options")
def memory_candidate_project_options(
    container: ApiContainerDep,
) -> dict[str, object]:
    store, _settings = product_repositories._object_store(container.root_dir)
    return {
        "projects": sorted(
            _known_memory_project_ids(store, exclude_candidate_id="")
        ),
        "content_included": False,
        "read_only": True,
    }


def _promote_imported_memory_candidate(
    *,
    container: Any,
    store: JsonObjectStore,
    namespace_id: str,
    candidate_id: str,
    reason: str,
) -> MemoryCandidateReviewResult:
    candidates = ObjectStoreMemoryCandidateRepository(store)
    candidate = candidates.get(candidate_id)
    if candidate is None:
        raise MemoryCandidateReviewError(f"Memory Candidate not found: {candidate_id}")
    target_layer = str(candidate.get("target_layer", ""))
    if target_layer not in {"atom", "scenario", "series_memory"}:
        raise MemoryCandidateReviewError(
            "imported Memory Candidate target_layer is not supported"
        )
    if not isinstance(candidate.get("project_id"), str) or not str(
        candidate.get("project_id")
    ).strip():
        raise MemoryCandidateReviewError(
            "imported Memory Candidate has no project_id; choose a target project "
            "before confirming this legacy package candidate"
        )
    if candidate.get("status") == "promoted":
        review = candidate.get("review") if isinstance(candidate.get("review"), Mapping) else {}
        return MemoryCandidateReviewResult(
            candidate_id=candidate_id,
            status="promoted",
            reviewed_by=str(review.get("reviewed_by") or "user"),
            reviewed_at=str(review.get("reviewed_at") or ""),
            promoted_layer=target_layer,
            promoted_object_id=str(
                candidate.get("portable_object_id") or candidate_id
            ),
        )
    tags = tuple(
        value
        for value in candidate.get("tags", [])
        if isinstance(value, str) and value
    ) if isinstance(candidate.get("tags"), list) else ()
    confidence_value = candidate.get("confidence", 0.7)
    confidence = (
        float(confidence_value)
        if isinstance(confidence_value, (int, float))
        and not isinstance(confidence_value, bool)
        else 0.7
    )
    candidate_type = str(candidate.get("candidate_type", ""))
    atom_type = {
        "answer_fact": "fact",
        "answer_decision": "decision",
        "answer_action": "action",
        "other": "other",
    }.get(candidate_type) if target_layer == "atom" else None
    series_id = str(candidate.get("series_id", "") or "") or None
    atom_ids = tuple(
        value for value in candidate.get("atom_ids", [])
        if isinstance(value, str) and value
    ) if isinstance(candidate.get("atom_ids"), list) else ()
    scenario_ids = tuple(
        value for value in candidate.get("scenario_ids", [])
        if isinstance(value, str) and value
    ) if isinstance(candidate.get("scenario_ids"), list) else ()
    try:
        staging_records = _require_memory_publication_records(
            container=container,
            store=store,
            namespace_id=namespace_id,
        )
    except AggregateRepositoryFactoryError as error:
        raise MemoryCandidateReviewError(str(error)) from error
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


def _require_memory_publication_records(
    *,
    container: ApiContainerDep,
    store: JsonObjectStore,
    namespace_id: str,
) -> SQLiteStructuredRecordStore:
    resolution = AggregateRepositoryFactory(
        runtime_root=container.root_dir,
        namespace_id=namespace_id,
        json_store=store,
    ).memory_publication_authority_resolution()
    if resolution.records is None:
        raise AggregateRepositoryFactoryError(
            "durable SQLite Memory publication authority is unavailable"
        )
    return resolution.records


def _known_memory_project_ids(
    store: JsonObjectStore,
    *,
    exclude_candidate_id: str,
) -> set[str]:
    project_ids: set[str] = set()
    for collection in (
        "sources",
        "documents",
        "jobs",
        "project_skills",
        "memory_candidates",
        "memory_scenarios",
        "memory_series_memory",
        "staging_scenarios",
        "staging_series_memory",
    ):
        for item in store.list(collection):
            if (
                collection == "memory_candidates"
                and str(item.get("id", "")) == exclude_candidate_id
            ):
                continue
            direct = item.get("project_id")
            if isinstance(direct, str) and direct.strip():
                project_ids.add(direct.strip())
            metadata = item.get("metadata")
            if isinstance(metadata, Mapping):
                nested = metadata.get("project_id")
                if isinstance(nested, str) and nested.strip():
                    project_ids.add(nested.strip())
                nested_many = metadata.get("project_ids")
                if isinstance(nested_many, Sequence) and not isinstance(
                    nested_many, (str, bytes)
                ):
                    project_ids.update(
                        value.strip()
                        for value in nested_many
                        if isinstance(value, str) and value.strip()
                    )
            many = item.get("project_ids")
            if isinstance(many, Sequence) and not isinstance(many, (str, bytes)):
                project_ids.update(
                    value.strip()
                    for value in many
                    if isinstance(value, str) and value.strip()
                )
    return project_ids


@router.post("/api/rebuild/memory/candidates/conflict")
async def memory_candidates_conflict(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    """冲突解决：保留已有 / 接受新条目 / 合并 / 待定。

    body: { conflict_id, resolution: keep_existing|accept_incoming|merge|pending, merged_content? }
    """
    store, _settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping):
        return product_memory_import_records._stage16_error_response("conflict resolution rejected", "request body is required")
    conflict_id = str(body.get("conflict_id", "") or "")
    resolution = str(body.get("resolution", "") or "")
    if not conflict_id or not resolution:
        return product_memory_import_records._stage16_error_response(
            "conflict resolution rejected", "conflict_id and resolution are required"
        )
    # 同时接受两套枚举值（前端历史命名 + 规范命名），归一化到规范命名后存储
    _RESOLUTION_ALIASES = {
        "accept_new": "accept_incoming",
        "needs_review": "pending",
    }
    if resolution not in ("keep_existing", "accept_incoming", "merge", "pending", "accept_new", "needs_review"):
        return product_memory_import_records._stage16_error_response(
            "conflict resolution rejected",
            f"resolution must be one of keep_existing/accept_incoming/merge/pending, got {resolution}",
        )
    resolution = _RESOLUTION_ALIASES.get(resolution, resolution)

    conflict = store.read(product_memory_import_records._MEMORY_CONFLICTS_COLLECTION, conflict_id)
    if conflict is None:
        return product_memory_import_records._stage16_error_response(
            "conflict resolution rejected", f"conflict {conflict_id} not found",
            status_code=404,
        )

    updated = dict(conflict)
    updated["status"] = "resolved" if resolution != "pending" else "needs_review"
    updated["resolution"] = resolution
    if resolution == "merge" and isinstance(body.get("merged_content"), str):
        merged = dict(updated.get("existing", {}) or {})
        merged["content"] = body.get("merged_content")
        updated["merged"] = merged

    candidate_id = str(
        conflict.get("candidate_id")
        or (
            conflict.get("incoming", {}).get("id")
            if isinstance(conflict.get("incoming"), Mapping)
            else ""
        )
        or (
            conflict.get("existing", {}).get("id")
            if isinstance(conflict.get("existing"), Mapping)
            else ""
        )
    )
    review_sources = product_document_visibility._workspace_review_sources(Path(container.root_dir))
    if any(
        product_document_visibility._candidate_uses_source(item, review_sources)
        for item in (conflict.get("incoming"), conflict.get("existing"),
                     store.read(product_memory_import_records._MEMORY_CANDIDATES_COLLECTION, candidate_id) if candidate_id else None)
        if isinstance(item, Mapping)
    ):
        return product_memory_import_records._stage16_error_response(
            "conflict resolution moved", "review in Workspace and Recognition", status_code=409,
        )
    candidate_update: dict[str, object] | None = None
    if resolution == "accept_incoming" and isinstance(conflict.get("incoming"), Mapping):
        candidate_update = dict(conflict["incoming"])
    elif resolution == "merge" and isinstance(updated.get("merged"), Mapping):
        candidate_update = dict(updated["merged"])
    if candidate_update is not None and candidate_id:
        candidate_update["id"] = candidate_id
        candidate_update["memory_id"] = str(
            candidate_update.get("memory_id") or candidate_id
        )
        candidate_update["status"] = "candidate"
        candidate_update["group"] = "needs_review"
        try:
            store.write(
                product_memory_import_records._MEMORY_CANDIDATES_COLLECTION,
                candidate_id,
                candidate_update,
                expected_revision=None,
            )
        except Exception as exc:  # noqa: BLE001
            return product_memory_import_records._stage16_error_response(
                "conflict resolution rejected",
                f"failed to apply candidate resolution: {exc}",
                status_code=409,
            )

    try:
        store.write(
            product_memory_import_records._MEMORY_CONFLICTS_COLLECTION, conflict_id, updated, expected_revision=None,
        )
    except Exception as exc:  # noqa: BLE001
        return product_memory_import_records._stage16_error_response(
            "conflict resolution rejected", f"failed to update conflict: {exc}"
        )

    return product_memory_import_records._stage16_ok_response({
        "conflict": product_memory_import_records._serialize_conflict(updated),
        "resolution": resolution,
        "status": updated["status"],
    })
