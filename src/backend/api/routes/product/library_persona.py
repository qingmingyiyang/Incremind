"""Library persona ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep

from core.aggregate_repository_factory import (
    AggregateRepositoryFactory,
    AggregateRepositoryFactoryError,
)
from core.product_core.persona import (
    ObjectStorePersonaRepository,
    PersonaConflictError,
    PersonaError,
    PersonaExtractor,
    RollbackPersonaRevision,
    UpdatePersonaConfirmation,
)
from core.storage_provider import JsonObjectStore, RebuildStorageSettings

from . import http as product_http
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.get("/api/rebuild/library/persona")
def library_persona(request: Request, container: ApiContainerDep) -> JSONResponse:
    """Return the L4 Persona management view for the requested scope.

    The top-level digest is the active review target (draft first), while
    ``current_digest`` is the only confirmed Persona visible to recall.
    """
    _ = request
    store, settings = product_repositories._object_store(container.root_dir)
    scope = request.query_params.get("scope") or "global"
    if scope not in {"global", "series", "project"}:
        return product_http._json_response(
            400,
            {"detail": "scope must be one of global|series|project"},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    repository = ObjectStorePersonaRepository(store)
    digest = repository.review_digest(scope)
    return product_http._json_response(
        200,
        _persona_management_payload(repository, scope, digest=digest),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.post("/api/rebuild/library/persona/distill")
def library_persona_distill(request: Request, container: ApiContainerDep) -> JSONResponse:
    """手动触发 Persona 蒸馏：从已确认 memory entries 生成草稿并持久化。

    L4 Persona 的写入路径。读正式结构化权威中的 memory_atoms /
    memory_scenarios / memory_series_memory 三个 collection 中
    trust_status=user_confirmed 的条目，调 PersonaExtractor.extract()
    生成草稿，再用 ObjectStorePersonaRepository.save() 持久化。

    成功后返回最新 digest（与 GET /api/rebuild/library/persona 形态一致）。
    """
    _ = request
    store, settings = product_repositories._object_store(container.root_dir)
    scope = request.query_params.get("scope") or "global"
    if scope not in {"global", "series", "project"}:
        return product_http._json_response(
            400,
            {"detail": "scope must be one of global|series|project"},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    try:
        confirmed_entries, authority = _persona_confirmed_entries(
            container=container,
            store=store,
            settings=settings,
        )
    except PersonaError as exc:
        return product_http._json_response(
            503,
            {"detail": "persona authority unavailable", "reason": str(exc)},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    if not confirmed_entries:
        return product_http._json_response(
            200,
            {
                "status": "skipped",
                "reason": "no_confirmed_entries",
                "authority": authority,
                "digest": _persona_management_payload(
                    ObjectStorePersonaRepository(store),
                    scope,
                ),
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    try:
        extractor = PersonaExtractor(now=datetime.now(timezone.utc).isoformat())
        record = extractor.extract(scope=scope, confirmed_entries=confirmed_entries)
        repository = ObjectStorePersonaRepository(store)
        persona_state = _persona_management_payload(repository, scope)
        repository.save(
            record,
            expected_draft_revision=persona_state["draft_cas_revision"],
            expected_current_revision=persona_state["current_cas_revision"],
        )
        digest = repository.review_digest(scope)
    except PersonaConflictError as exc:
        return product_http._json_response(
            409,
            {"detail": "persona distill conflicted", "reason": str(exc)},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    except PersonaError as exc:
        return product_http._json_response(
            200,
            {
                "status": "skipped",
                "reason": "extractor_error",
                "error": str(exc),
                "authority": authority,
                "digest": _persona_management_payload(
                    ObjectStorePersonaRepository(store),
                    scope,
                ),
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        {
            "status": "distilled",
            "entry_count": len(confirmed_entries),
            "authority": authority,
            "digest": _persona_management_payload(
                repository,
                scope,
                digest=digest,
            ),
        },
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.post("/api/rebuild/library/persona/confirm")
async def library_persona_confirm(request: Request, container: ApiContainerDep) -> JSONResponse:
    """用户确认或拒绝 pending 状态的 Persona 草稿。

    L4 Persona 草稿到 current 的双 CAS 确认路径。Body:
    - scope: 可选，默认 global
    - status: "confirmed" | "rejected"
    - actor: 可选，默认 "user"
    - reason: 必填，用户给出的确认/拒绝理由

    成功后返回最新 digest（与 GET /api/rebuild/library/persona 形态一致）。
    """
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    scope = (body.get("scope") if isinstance(body, Mapping) else None) or "global"
    if scope not in {"global", "series", "project"}:
        return product_http._json_response(
            400,
            {"detail": "scope must be one of global|series|project"},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    status = body.get("status") if isinstance(body, Mapping) else None
    if status not in {"confirmed", "rejected"}:
        return product_http._json_response(
            400,
            {"detail": "status must be confirmed or rejected"},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    reason = body.get("reason") if isinstance(body, Mapping) else None
    if not isinstance(reason, str) or not reason.strip():
        return product_http._json_response(
            400,
            {"detail": "reason is required"},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    actor = body.get("actor") or "user"
    if not isinstance(actor, str) or not actor.strip():
        actor = "user"
    expected_draft_revision = body.get("expected_draft_revision")
    expected_current_revision = body.get("expected_current_revision")
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in (expected_draft_revision, expected_current_revision)
    ):
        return product_http._json_response(
            400,
            {"detail": "expected draft/current revisions are required"},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    try:
        repository = ObjectStorePersonaRepository(store)
        use_case = UpdatePersonaConfirmation(
            repository=repository,
            now=datetime.now(timezone.utc).isoformat(),
        )
        digest = use_case.execute(
            scope=scope,
            status=status,
            actor=actor,
            reason=reason.strip(),
            expected_draft_revision=expected_draft_revision,
            expected_current_revision=expected_current_revision,
        )
    except PersonaConflictError as exc:
        return product_http._json_response(
            409,
            {"detail": "persona confirm conflicted", "reason": str(exc)},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    except PersonaError as exc:
        return product_http._json_response(
            400,
            {"detail": "persona confirm rejected", "reason": str(exc)},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        {
            "status": status,
            "scope": scope,
            "digest": _persona_management_payload(
                repository,
                scope,
                digest=digest,
            ),
        },
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.post("/api/rebuild/library/persona/rollback")
async def library_persona_rollback(request: Request, container: ApiContainerDep) -> JSONResponse:
    """回滚 Persona 到上一个或指定 revision。

    L4 Persona 已确认 current 的回滚路径。Body:
    - scope: 可选，默认 global
    - to_revision: 可选 int，目标 revision 号；缺省回滚到最近一个历史 revision
    - actor: 可选，默认 "user"
    - reason: 必填，回滚理由
    - expected_draft_revision/current_revision: 必填，避免过期页面覆盖新状态

    成功后返回最新 digest。
    """
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    scope = (body.get("scope") if isinstance(body, Mapping) else None) or "global"
    if scope not in {"global", "series", "project"}:
        return product_http._json_response(
            400,
            {"detail": "scope must be one of global|series|project"},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    reason = body.get("reason") if isinstance(body, Mapping) else None
    if not isinstance(reason, str) or not reason.strip():
        return product_http._json_response(
            400,
            {"detail": "reason is required"},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    to_revision_raw = body.get("to_revision") if isinstance(body, Mapping) else None
    to_revision: int | None = None
    if to_revision_raw is not None:
        if not isinstance(to_revision_raw, int) or isinstance(to_revision_raw, bool):
            return product_http._json_response(
                400,
                {"detail": "to_revision must be an integer"},
                {"Content-Type": "application/json", "Cache-Control": "no-store"},
            )
        to_revision = to_revision_raw
    actor = body.get("actor") or "user"
    if not isinstance(actor, str) or not actor.strip():
        actor = "user"
    expected_draft_revision = body.get("expected_draft_revision")
    expected_current_revision = body.get("expected_current_revision")
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in (expected_draft_revision, expected_current_revision)
    ):
        return product_http._json_response(
            400,
            {"detail": "expected draft/current revisions are required"},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    try:
        repository = ObjectStorePersonaRepository(store)
        use_case = RollbackPersonaRevision(
            repository=repository,
            now=datetime.now(timezone.utc).isoformat(),
        )
        digest = use_case.execute(
            scope=scope,
            to_revision=to_revision,
            actor=actor,
            reason=reason.strip(),
            expected_draft_revision=expected_draft_revision,
            expected_current_revision=expected_current_revision,
        )
    except PersonaConflictError as exc:
        return product_http._json_response(
            409,
            {"detail": "persona rollback conflicted", "reason": str(exc)},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    except PersonaError as exc:
        return product_http._json_response(
            400,
            {"detail": "persona rollback rejected", "reason": str(exc)},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        {
            "status": "rolled_back",
            "scope": scope,
            "digest": _persona_management_payload(
                repository,
                scope,
                digest=digest,
            ),
        },
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


def _persona_confirmed_entries(
    *,
    container: ApiContainerDep,
    store: JsonObjectStore,
    settings: RebuildStorageSettings,
) -> tuple[list[Mapping[str, object]], str]:
    try:
        resolution = AggregateRepositoryFactory(
            runtime_root=container.root_dir,
            namespace_id=settings.namespace_id,
            json_store=store,
        ).memory_publication_authority_resolution()
    except AggregateRepositoryFactoryError as error:
        raise PersonaError(str(error)) from error
    confirmed_entries: list[Mapping[str, object]] = []
    if resolution.records is not None:
        for collection in (
            "memory_atoms",
            "memory_scenarios",
            "memory_series_memory",
        ):
            confirmed_entries.extend(
                dict(record.payload)
                for record in resolution.records.list(collection)
                if record.payload.get("trust_status") == "user_confirmed"
            )
        return confirmed_entries, "sqlite:structured-records-v1"
    for collection in ("memory_atoms", "memory_scenarios", "memory_series_memory"):
        confirmed_entries.extend(
            dict(entry)
            for entry in store.list(collection)
            if isinstance(entry, Mapping)
            and entry.get("trust_status") == "user_confirmed"
        )
    return confirmed_entries, "json-object-store:legacy-fallback"


def _persona_management_payload(
    repository: ObjectStorePersonaRepository,
    scope: str,
    *,
    digest=None,
) -> dict[str, object]:
    active_digest = digest or repository.review_digest(scope)
    current_digest = repository.digest(scope)
    draft = repository.get_draft(scope)
    raw_current = repository.get(scope)
    legacy_draft = (
        draft is None
        and raw_current is not None
        and not current_digest.ready
    )
    physical_current_revision = repository.object_store.revision(
        repository.collection,
        f"persona-{scope}",
    )
    return {
        **active_digest.to_payload(),
        "layer": "l4_persona",
        "current_digest": current_digest.to_payload(),
        "draft_available": draft is not None or legacy_draft,
        "draft_cas_revision": (
            physical_current_revision
            if legacy_draft
            else repository.object_store.revision(
                repository.drafts_collection,
                f"persona-{scope}",
            )
        ),
        "current_cas_revision": (
            0 if legacy_draft else physical_current_revision
        ),
        "legacy_draft_in_current": legacy_draft,
    }
