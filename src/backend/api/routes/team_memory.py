from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.api.container import ApiContainerDep
from backend.team_memory import (
    TeamMemoryConflict,
    TeamMemoryError,
    TeamMemoryPreflightError,
    TeamMemoryProfileStore,
    run_team_memory_asset_inventory,
    run_team_memory_preflight,
    save_team_memory_secrets,
    serialize_team_memory_profile,
)
from core.product_core.team_memory_source_authority_saga import (
    CommitTeamMemoryStagingToSource,
    ObjectStoreTeamSourceAuthority,
    TeamMemorySourceAuthorityConflict,
    TeamMemorySourceAuthorityError,
)
from core.product_core.team_memory_candidate_import import (
    ObjectStoreTeamMemoryImportDraftRepository,
)
from core.product_core.team_memory_source_recovery import (
    AbandonConflictingTeamSourceCommit,
    ForgetTeamCreatedSource,
    diagnose_team_source_commit,
)
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.product_core.team_memory_source_candidate import (
    PrepareTeamMemorySourceCandidate,
    TeamMemorySourceCandidateConflict,
    TeamMemorySourceCandidateError,
)
from core.product_core.team_memory_source_extraction import (
    PrepareTeamMemorySourceExtraction,
    TeamMemorySourceExtractionConflict,
    TeamMemorySourceExtractionError,
)
from core.product_core.team_memory_source_staging import (
    TeamMemorySourceStagingError,
    TeamMemorySourceStagingRepository,
)
from core.storage_provider import JsonObjectStore
from backend.security.user_context import json_attribution


router = APIRouter()


class TeamMemoryProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    endpoint: str = Field(max_length=500)
    service_id: str = Field(max_length=128)
    team_id: str = Field(max_length=128)
    agent_id: str = Field(max_length=128)
    user_id: str = Field(max_length=128)
    expected_revision: int = Field(ge=0)
    confirm_enable: bool = False


class TeamMemorySecretsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_api_key: str = Field(min_length=8, max_length=4096)
    user_key: str = Field(min_length=8, max_length=4096)


class TeamMemoryPreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consented: bool


class TeamMemoryDisconnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    confirmed: bool


class TeamMemoryAssetInventoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consented: bool


class TeamSourceAbandonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_staging_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    confirmed: bool
    abandoned_at: str = Field(min_length=1, max_length=64)


class TeamSourceResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claimed_staging_revision: int = Field(ge=1)
    confirmed: bool
    completed_at: str = Field(min_length=1, max_length=64)


class TeamSourceForgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_staging_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    confirmed: bool
    forgotten_at: str = Field(min_length=1, max_length=64)


class TeamSourceCandidateStageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: str = Field(min_length=1, max_length=160)
    expected_staging_revision: int = Field(ge=1)
    expected_source_revision: int = Field(ge=1)
    confirmed: bool
    created_at: str = Field(min_length=1, max_length=64)


class TeamSourceCandidateDispositionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_candidate_revision: int = Field(ge=1)
    action: str = Field(pattern="^(reject|withdraw)$")
    reason: str = Field(min_length=1, max_length=500)
    confirmed: bool
    reviewed_at: str = Field(min_length=1, max_length=64)


class TeamSourceExtractionStageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: str = Field(min_length=1, max_length=160)
    item_id: str = Field(min_length=1, max_length=160)
    target_layer: str = Field(pattern="^(atom|scenario)$")
    expected_staging_revision: int = Field(ge=1)
    expected_source_revision: int = Field(ge=1)
    confirmed: bool
    created_at: str = Field(min_length=1, max_length=64)


@router.get("/api/rebuild/team-memory/source-staging")
def list_team_source_staging(
    container: ApiContainerDep,
) -> dict[str, object]:
    store = _team_source_store(container.root_dir)
    staging = TeamMemorySourceStagingRepository(store)
    sources = ObjectStoreTeamSourceAuthority(store)
    candidates = ObjectStoreMemoryCandidateRepository(store)
    items: list[dict[str, object]] = []
    for raw in store.list(staging.collection):
        staging_id = raw.get("id")
        if not isinstance(staging_id, str):
            continue
        record = staging.get(staging_id)
        if record is None:
            continue
        proposal = record.get("proposed_source")
        origin = record.get("origin")
        if not isinstance(proposal, Mapping) or not isinstance(origin, Mapping):
            continue
        status = record.get("status")
        receipt = record.get("receipt")
        recovery_status = None
        recovery_action = None
        source_revision = None
        if status == "committing":
            diagnosis = diagnose_team_source_commit(
                staging=staging,
                sources=sources,
                staging_id=staging_id,
            )
            recovery_status = diagnosis.status
            recovery_action = diagnosis.action
            source_revision = diagnosis.source_revision
        elif status in {"completed", "forgetting", "forgotten"}:
            if isinstance(receipt, Mapping):
                source_revision = receipt.get("source_revision")
        candidate_id = None
        candidate_status = None
        candidate_revision = None
        candidate_content_erased = False
        source_candidates: list[dict[str, object]] = []
        if status in {"completed", "forgetting"}:
            try:
                candidate_preview = _team_source_candidate_service(
                    store,
                    staging=staging,
                    sources=sources,
                    candidates=candidates,
                ).preview(staging_id)
                candidate_id = candidate_preview.candidate_id
                candidate = candidates.get(candidate_id)
                if candidate is not None:
                    candidate_status = candidate.get("status")
                    candidate_revision = store.revision(
                        candidates.collection,
                        candidate_id,
                    )
                    erasure = candidate.get("source_erasure")
                    candidate_content_erased = (
                        isinstance(erasure, Mapping)
                        and erasure.get("state") == "content_erased"
                    )
            except (TeamMemorySourceCandidateError, TeamMemorySourceCandidateConflict):
                candidate_id = None
            source_id = proposal.get("id")
            if isinstance(source_id, str):
                for candidate_value in candidates.list_by_project(
                    str(record.get("project_id") or "")
                ):
                    provenance = candidate_value.get("provenance")
                    if not isinstance(provenance, Mapping) or provenance.get("source_id") != source_id:
                        continue
                    candidate_value_id = candidate_value.get("id")
                    if not isinstance(candidate_value_id, str):
                        continue
                    erasure = candidate_value.get("source_erasure")
                    erased = (
                        isinstance(erasure, Mapping)
                        and erasure.get("state") == "content_erased"
                    )
                    source_candidates.append(
                        {
                            "candidate_id": candidate_value_id,
                            "candidate_revision": store.revision(
                                candidates.collection, candidate_value_id
                            ),
                            "status": candidate_value.get("status"),
                            "target_layer": candidate_value.get("target_layer"),
                            "content_erased": erased,
                        }
                    )
        items.append(
            {
                "staging_id": staging_id,
                "staging_revision": staging.revision(staging_id),
                "project_id": record.get("project_id"),
                "status": status,
                "source_id": proposal.get("id"),
                "asset_type": origin.get("asset_type"),
                "asset_version": origin.get("asset_version"),
                "updated_at": record.get("updated_at"),
                "recovery_status": recovery_status,
                "recovery_action": recovery_action,
                "source_revision": source_revision,
                "claimed_staging_revision": (
                    receipt.get("claimed_staging_revision")
                    if isinstance(receipt, Mapping)
                    else None
                ),
                "forget_requested_staging_revision": (
                    receipt.get("forget_requested_staging_revision")
                    if isinstance(receipt, Mapping)
                    else None
                ),
                "content_included": False,
                "candidate_id": candidate_id,
                "candidate_status": candidate_status,
                "candidate_revision": candidate_revision,
                "candidate_content_erased": candidate_content_erased,
                "source_candidates": source_candidates,
                "has_active_source_candidates": any(
                    not value["content_erased"] for value in source_candidates
                ),
            }
        )
    items.sort(
        key=lambda item: (
            str(item.get("updated_at") or ""),
            str(item.get("staging_id") or ""),
        ),
        reverse=True,
    )
    return {
        "status": "local_source_staging",
        "items": items,
        "total": len(items),
        "content_included": False,
        "network_called": False,
        "sync_available": False,
        "import_available": False,
        "export_available": False,
    }


@router.get(
    "/api/rebuild/team-memory/source-staging/{staging_id}/candidate-preview"
)
def preview_team_source_candidate(
    staging_id: str,
    container: ApiContainerDep,
) -> dict[str, object]:
    store = _team_source_store(container.root_dir)
    try:
        preview = _team_source_candidate_service(store).preview(staging_id)
    except TeamMemorySourceCandidateConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except TeamMemorySourceCandidateError as error:
        status_code = 404 if "not found" in str(error) else 400
        raise HTTPException(status_code=status_code, detail=str(error)) from error
    return {
        "preview_id": preview.preview_id,
        "candidate_id": preview.candidate_id,
        "staging_id": preview.staging_id,
        "staging_revision": preview.staging_revision,
        "source_id": preview.source_id,
        "source_revision": preview.source_revision,
        "source_content_sha256": preview.source_content_sha256,
        "project_id": preview.project_id,
        "target_layer": preview.target_layer,
        "candidate_type": preview.candidate_type,
        "proposed_content": preview.proposed_content,
        "difference": dict(preview.difference),
        "safety": dict(preview.safety),
        "content_included": True,
        "network_called": False,
    }


@router.post(
    "/api/rebuild/team-memory/source-staging/{staging_id}/candidate"
)
def stage_team_source_candidate(
    staging_id: str,
    request: TeamSourceCandidateStageRequest,
    container: ApiContainerDep,
) -> dict[str, object]:
    store = _team_source_store(container.root_dir)
    try:
        result = _team_source_candidate_service(store).stage(
            staging_id,
            **request.model_dump(),
        )
    except TeamMemorySourceCandidateConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except TeamMemorySourceCandidateError as error:
        status_code = 404 if "not found" in str(error) else 400
        raise HTTPException(status_code=status_code, detail=str(error)) from error
    return {
        "candidate_id": result.candidate_id,
        "status": result.status,
        "target_layer": result.target_layer,
        "replayed": result.replayed,
        "content_included": False,
        "publication_created": False,
        "automatic_recall_enabled": False,
        "network_called": False,
    }


@router.get(
    "/api/rebuild/team-memory/source-staging/{staging_id}/extraction-preview"
)
def preview_team_source_extraction(
    staging_id: str,
    container: ApiContainerDep,
) -> dict[str, object]:
    store = _team_source_store(container.root_dir)
    try:
        preview = _team_source_extraction_service(store).preview(staging_id)
    except TeamMemorySourceExtractionConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except TeamMemorySourceExtractionError as error:
        status_code = 404 if "not found" in str(error) else 400
        raise HTTPException(status_code=status_code, detail=str(error)) from error
    return {
        "preview_id": preview.preview_id,
        "staging_id": preview.staging_id,
        "staging_revision": preview.staging_revision,
        "source_id": preview.source_id,
        "source_revision": preview.source_revision,
        "source_content_sha256": preview.source_content_sha256,
        "project_id": preview.project_id,
        "items": [
            {
                "item_id": item.item_id,
                "target_layer": item.target_layer,
                "candidate_type": item.candidate_type,
                "title": item.title,
                "proposed_content": item.proposed_content,
                "source_locator": item.source_locator,
                "source_quote": item.source_quote,
                "start_char": item.start_char,
                "end_char": item.end_char,
                "paragraph_ids": list(item.paragraph_ids),
                "tags": list(item.tags),
            }
            for item in preview.items
        ],
        "safety": dict(preview.safety),
        "content_included": True,
        "network_called": False,
    }


@router.post(
    "/api/rebuild/team-memory/source-staging/{staging_id}/extraction-candidate"
)
def stage_team_source_extraction(
    staging_id: str,
    request: TeamSourceExtractionStageRequest,
    container: ApiContainerDep,
) -> dict[str, object]:
    store = _team_source_store(container.root_dir)
    try:
        result = _team_source_extraction_service(store).stage(
            staging_id,
            **request.model_dump(),
        )
    except TeamMemorySourceExtractionConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except TeamMemorySourceExtractionError as error:
        status_code = 404 if "not found" in str(error) else 400
        raise HTTPException(status_code=status_code, detail=str(error)) from error
    return {
        "candidate_id": result.candidate_id,
        "item_id": result.item_id,
        "status": result.status,
        "target_layer": result.target_layer,
        "replayed": result.replayed,
        "content_included": False,
        "publication_created": False,
        "automatic_recall_enabled": False,
        "network_called": False,
    }


@router.post(
    "/api/rebuild/team-memory/candidates/{candidate_id}/disposition"
)
def dispose_team_source_candidate(
    candidate_id: str,
    request: TeamSourceCandidateDispositionRequest,
    container: ApiContainerDep,
) -> dict[str, object]:
    store = _team_source_store(container.root_dir)
    try:
        result = _team_source_candidate_service(store).dispose(
            candidate_id,
            **request.model_dump(),
        )
    except TeamMemorySourceCandidateConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except TeamMemorySourceCandidateError as error:
        status_code = 404 if "not found" in str(error) else 400
        raise HTTPException(status_code=status_code, detail=str(error)) from error
    return {
        "operation_id": result.operation_id,
        "candidate_id": result.candidate_id,
        "status": result.status,
        "replayed": result.replayed,
        "content_erased": True,
        "content_included": False,
        "publication_created": False,
        "automatic_recall_enabled": False,
        "network_called": False,
    }


@router.post(
    "/api/rebuild/team-memory/source-staging/{staging_id}/resume"
)
def resume_team_source_commit(
    staging_id: str,
    request: TeamSourceResumeRequest,
    container: ApiContainerDep,
) -> dict[str, object]:
    store = _team_source_store(container.root_dir)
    try:
        result = CommitTeamMemoryStagingToSource(
            drafts=ObjectStoreTeamMemoryImportDraftRepository(store),
            staging=TeamMemorySourceStagingRepository(store),
            sources=ObjectStoreTeamSourceAuthority(store),
        ).execute(
            staging_id,
            expected_staging_revision=request.claimed_staging_revision,
            confirmed=request.confirmed,
            completed_at=request.completed_at,
        )
    except TeamMemorySourceAuthorityConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (TeamMemorySourceAuthorityError, TeamMemorySourceStagingError) as error:
        status_code = 404 if "not found" in str(error) else 400
        raise HTTPException(status_code=status_code, detail=str(error)) from error
    return {
        "operation_id": result.operation_id,
        "staging_id": result.staging_id,
        "source_id": result.source_id,
        "source_revision": result.source_revision,
        "status": result.status,
        "replayed": result.replayed,
        "content_included": False,
        "network_called": False,
    }


@router.get("/api/rebuild/team-memory/profile")
def get_team_memory_profile(container: ApiContainerDep) -> dict[str, object]:
    try:
        profile = TeamMemoryProfileStore(container.root_dir).load()
        return serialize_team_memory_profile(profile, secret_store=container.secret_store)
    except TeamMemoryError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.put("/api/rebuild/team-memory/profile")
def save_team_memory_profile(
    request: TeamMemoryProfileRequest,
    container: ApiContainerDep,
) -> dict[str, object]:
    try:
        profile = TeamMemoryProfileStore(container.root_dir).save(
            **request.model_dump(),
            secret_store=container.secret_store,
        )
        return serialize_team_memory_profile(profile, secret_store=container.secret_store)
    except TeamMemoryConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except TeamMemoryError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.put("/api/rebuild/team-memory/secrets")
def save_team_memory_secret_values(
    request: TeamMemorySecretsRequest,
    container: ApiContainerDep,
) -> dict[str, bool]:
    try:
        save_team_memory_secrets(container.secret_store, **request.model_dump())
    except TeamMemoryError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"has_service_api_key": True, "has_user_key": True}


@router.post("/api/rebuild/team-memory/disconnect")
def disconnect_team_memory(
    request: TeamMemoryDisconnectRequest,
    container: ApiContainerDep,
) -> dict[str, object]:
    try:
        profile = TeamMemoryProfileStore(container.root_dir).disconnect(
            **request.model_dump(),
            secret_store=container.secret_store,
        )
        return serialize_team_memory_profile(profile, secret_store=container.secret_store)
    except TeamMemoryConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except TeamMemoryError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/rebuild/team-memory/preflight")
async def preflight_team_memory(
    request: TeamMemoryPreflightRequest,
    container: ApiContainerDep,
) -> dict[str, object]:
    if request.consented is not True:
        raise HTTPException(status_code=400, detail="team memory preflight requires explicit consent")
    try:
        profile_store = TeamMemoryProfileStore(container.root_dir)
        result = await run_team_memory_preflight(
            profile_store.load(),
            secret_store=container.secret_store,
            client=getattr(container, "team_memory_preflight_client", None),
            resolver=getattr(container, "team_memory_preflight_resolver", None),
            boundary_revision_reader=lambda: profile_store.load().revision,
        )
    except TeamMemoryPreflightError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except TeamMemoryError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {
        "status": result.status,
        "endpoint_origin": result.endpoint_origin,
        "server_health": result.server_health,
        "server_version": result.server_version,
        "resolved_user_id": result.resolved_user_id,
        "capabilities": [dict(item) for item in result.capabilities],
        "sync_available": False,
        "import_available": False,
        "export_available": False,
    }


@router.post("/api/rebuild/team-memory/asset-inventory")
async def inspect_team_memory_asset_inventory(
    request: TeamMemoryAssetInventoryRequest,
    container: ApiContainerDep,
) -> dict[str, object]:
    if request.consented is not True:
        raise HTTPException(status_code=400, detail="team memory asset inventory requires explicit consent")
    try:
        profile_store = TeamMemoryProfileStore(container.root_dir)
        result = await run_team_memory_asset_inventory(
            profile_store.load(),
            secret_store=container.secret_store,
            client=getattr(container, "team_memory_asset_client", None),
            resolver=getattr(container, "team_memory_asset_resolver", None),
            boundary_revision_reader=lambda: profile_store.load().revision,
        )
    except TeamMemoryPreflightError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except TeamMemoryError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {
        "status": "read_only_asset_inventory",
        "endpoint_origin": result.endpoint_origin,
        "team_id": result.team_id,
        "agent_id": result.agent_id,
        "user_id": result.user_id,
        "counts": dict(result.counts),
        "items": [dict(item) for item in result.items],
        "persisted": False,
        "content_fetched": False,
        "sync_available": False,
        "import_available": False,
        "export_available": False,
    }


@router.get(
    "/api/rebuild/team-memory/source-staging/{staging_id}/recovery"
)
def diagnose_team_source_recovery(
    staging_id: str,
    container: ApiContainerDep,
) -> dict[str, object]:
    store = _team_source_store(container.root_dir)
    try:
        result = diagnose_team_source_commit(
            staging=TeamMemorySourceStagingRepository(store),
            sources=ObjectStoreTeamSourceAuthority(store),
            staging_id=staging_id,
        )
    except TeamMemorySourceAuthorityConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (TeamMemorySourceAuthorityError, TeamMemorySourceStagingError) as error:
        status_code = 404 if "not found" in str(error) else 400
        raise HTTPException(status_code=status_code, detail=str(error)) from error
    return {
        "staging_id": result.staging_id,
        "source_id": result.source_id,
        "status": result.status,
        "action": result.action,
        "source_revision": result.source_revision,
        "expected_content_sha256": result.expected_content_sha256,
        "observed_content_sha256": result.observed_content_sha256,
        "content_included": False,
        "network_called": False,
    }


@router.post(
    "/api/rebuild/team-memory/source-staging/{staging_id}/abandon"
)
def abandon_team_source_recovery(
    staging_id: str,
    request: TeamSourceAbandonRequest,
    container: ApiContainerDep,
) -> dict[str, object]:
    if request.confirmed is not True:
        raise HTTPException(
            status_code=400,
            detail="team source abandonment requires explicit confirmation",
        )
    store = _team_source_store(container.root_dir)
    staging = TeamMemorySourceStagingRepository(store)
    existing = staging.get(staging_id)
    try:
        record = AbandonConflictingTeamSourceCommit(
            staging=staging,
            sources=ObjectStoreTeamSourceAuthority(store),
        ).execute(
            staging_id,
            expected_staging_revision=request.expected_staging_revision,
            reason=request.reason,
            abandoned_at=request.abandoned_at,
        )
    except TeamMemorySourceAuthorityConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (TeamMemorySourceAuthorityError, TeamMemorySourceStagingError) as error:
        status_code = 404 if "not found" in str(error) else 400
        raise HTTPException(status_code=status_code, detail=str(error)) from error
    return _terminal_team_source_response(
        record,
        replayed=existing is not None and existing.get("status") == "abandoned",
    )


@router.post(
    "/api/rebuild/team-memory/source-staging/{staging_id}/hard-forget"
)
def hard_forget_team_source(
    staging_id: str,
    request: TeamSourceForgetRequest,
    container: ApiContainerDep,
) -> dict[str, object]:
    store = _team_source_store(container.root_dir)
    staging = TeamMemorySourceStagingRepository(store)
    try:
        record = staging.get(staging_id)
        effective_reason = request.reason
        if record is not None and record.get("status") in {
            "forgetting",
            "forgotten",
        }:
            receipt = record.get("receipt")
            if isinstance(receipt, Mapping) and isinstance(
                receipt.get("forget_reason"),
                str,
            ):
                effective_reason = receipt["forget_reason"]
        result = ForgetTeamCreatedSource(
            object_store=store,
            staging=staging,
        ).execute(
            staging_id,
            expected_staging_revision=request.expected_staging_revision,
            confirmed=request.confirmed,
            reason=effective_reason,
            forgotten_at=request.forgotten_at,
        )
    except TeamMemorySourceAuthorityConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (TeamMemorySourceAuthorityError, TeamMemorySourceStagingError) as error:
        status_code = 404 if "not found" in str(error) else 400
        raise HTTPException(status_code=status_code, detail=str(error)) from error
    return {
        "operation_id": result.operation_id,
        "staging_id": result.staging_id,
        "source_id": result.source_id,
        "status": result.status,
        "replayed": result.replayed,
        "content_included": False,
        "network_called": False,
    }


def _team_source_store(root_dir) -> JsonObjectStore:
    return JsonObjectStore(
        root_dir / ".rebuild-data",
        legacy_root=root_dir / "library",
        namespace_id="default",
        mutation_attribution=json_attribution(root_dir, 'default'),
    )


def _team_source_candidate_service(
    store: JsonObjectStore,
    *,
    staging: TeamMemorySourceStagingRepository | None = None,
    sources: ObjectStoreTeamSourceAuthority | None = None,
    candidates: ObjectStoreMemoryCandidateRepository | None = None,
) -> PrepareTeamMemorySourceCandidate:
    return PrepareTeamMemorySourceCandidate(
        staging=staging or TeamMemorySourceStagingRepository(store),
        sources=sources or ObjectStoreTeamSourceAuthority(store),
        candidates=candidates or ObjectStoreMemoryCandidateRepository(store),
    )


def _team_source_extraction_service(
    store: JsonObjectStore,
) -> PrepareTeamMemorySourceExtraction:
    return PrepareTeamMemorySourceExtraction(
        staging=TeamMemorySourceStagingRepository(store),
        sources=ObjectStoreTeamSourceAuthority(store),
        candidates=ObjectStoreMemoryCandidateRepository(store),
    )


def _terminal_team_source_response(
    record: Mapping[str, object],
    *,
    replayed: bool,
) -> dict[str, object]:
    proposal = record.get("proposed_source")
    receipt = record.get("receipt")
    source_id = proposal.get("id") if isinstance(proposal, Mapping) else None
    operation_id = (
        receipt.get("operation_id") if isinstance(receipt, Mapping) else None
    )
    return {
        "operation_id": operation_id,
        "staging_id": record.get("id"),
        "source_id": source_id,
        "status": record.get("status"),
        "replayed": replayed,
        "content_included": False,
        "network_called": False,
    }
