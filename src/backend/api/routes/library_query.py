from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.library_query_runtime import (
    build_library_search_service,
    build_library_overview_search,
    build_related_memory_service,
)
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.aggregate_repository_factory import AggregateRepositoryFactoryError
from core.product_core.related_memory import RelatedMemoryError, RelatedMemoryQuery
from core.product_core.tag_index import (
    QueryTagFacets,
    QueryTaggedSources,
    serialize_tag_facets_result,
    serialize_tagged_sources_result,
)
from core.search_and_recall import LibrarySearchError
from core.product_core.library_overview import LibraryOverviewError
from core.product_core.library_overview_search import LibraryOverviewSearchError


router = APIRouter(tags=["rebuild-library-query"])


def _json_response(status_code: int, body: Mapping[str, Any]) -> JSONResponse:
    return JSONResponse(
        content=body,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/rebuild/library/tag-facets")
def library_tag_facets(request: Request, container: ApiContainerDep) -> JSONResponse:
    store, _settings = build_rebuild_object_store(container.root_dir)
    limit_text = request.query_params.get("limit", "50")
    try:
        limit = max(1, min(200, int(limit_text)))
    except ValueError:
        limit = 50
    project_id = request.query_params.get("project_id")
    try:
        result = QueryTagFacets(store).execute(limit=limit, project_id=project_id)
    except ValueError as error:
        return _json_response(
            400,
            {"detail": "tag facets query rejected", "reason": str(error)},
        )
    return _json_response(200, serialize_tag_facets_result(result))


@router.get("/api/rebuild/library/sources/by-tag")
def library_sources_by_tag(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    store, _settings = build_rebuild_object_store(container.root_dir)
    tag = request.query_params.get("tag", "").strip()
    if not tag:
        return _json_response(400, {"detail": "tag query parameter is required"})
    limit_text = request.query_params.get("limit", "100")
    try:
        limit = max(1, min(200, int(limit_text)))
    except ValueError:
        limit = 100
    try:
        result = QueryTaggedSources(store).execute(
            tag=tag,
            limit=limit,
            project_id=request.query_params.get("project_id"),
        )
    except ValueError as error:
        return _json_response(
            400,
            {"detail": "tagged sources query rejected", "reason": str(error)},
        )
    return _json_response(200, serialize_tagged_sources_result(result))


@router.get("/api/rebuild/library/search")
def library_search(request: Request, container: ApiContainerDep) -> JSONResponse:
    store, settings = build_rebuild_object_store(container.root_dir)
    query = request.query_params.get("q", "").strip()
    if not query:
        return _json_response(400, {"detail": "q query parameter is required"})
    if request.query_params.get("scope") == "overview":
        try:
            offset = int(request.query_params.get("offset", "0"))
            limit = int(request.query_params.get("limit", "30"))
            if offset < 0 or not 1 <= limit <= 100:
                raise ValueError
        except ValueError:
            return _json_response(400, {"detail": "offset or limit is invalid"})
        try:
            page = build_library_overview_search(container.root_dir, store, settings).execute(
                query=query,
                project_id=request.query_params.get("project_id") or None,
                filter_id=request.query_params.get("filter_id") or "all",
                tag=request.query_params.get("tag") or None,
                import_batch_id=request.query_params.get("import_batch_id") or None,
                offset=offset,
                limit=limit,
            )
        except (LibraryOverviewSearchError, LibraryOverviewError) as error:
            return _json_response(400, {"detail": "library overview search rejected", "reason": str(error)})
        except AggregateRepositoryFactoryError as error:
            return _json_response(409, {"detail": "library overview search rejected", "reason": str(error)})
        return _json_response(200, page.to_payload())
    limit_text = request.query_params.get("limit", "12")
    try:
        limit = max(1, min(50, int(limit_text)))
    except ValueError:
        limit = 12
    layers_text = request.query_params.get("layers")
    trust_text = request.query_params.get("trust")
    service = build_library_search_service(container.root_dir, store)
    try:
        result = service.search(
            query=query,
            project_id=request.query_params.get("project_id") or None,
            layers=tuple(layers_text.split(",")) if layers_text else None,
            trust_statuses=tuple(trust_text.split(",")) if trust_text else None,
            limit=limit,
        )
    except LibrarySearchError as error:
        return _json_response(
            400,
            {"detail": "library search rejected", "reason": str(error)},
        )
    return _json_response(
        200,
        {
            "status": result.status,
            "backend": result.backend,
            "query": result.query_text,
            "total": result.total,
            "index_stale": result.index_stale,
            "reason": result.reason,
            "hits": [
                {
                    "object_id": hit.object_id,
                    "layer": hit.layer,
                    "content": hit.content,
                    "source_refs": list(hit.source_refs),
                    "trust_status": hit.trust_status,
                    "score": hit.score,
                    "backend": hit.backend,
                }
                for hit in result.hits
            ],
        },
    )


@router.get("/api/rebuild/library/related")
def library_related(request: Request, container: ApiContainerDep) -> JSONResponse:
    store, settings = build_rebuild_object_store(container.root_dir)
    object_id = request.query_params.get("object_id") or ""
    layer = request.query_params.get("layer") or ""
    if not object_id:
        return _json_response(400, {"detail": "object_id is required"})
    if not layer:
        return _json_response(400, {"detail": "layer is required"})
    try:
        limit = int(request.query_params.get("limit") or "8")
    except ValueError:
        return _json_response(400, {"detail": "limit must be an integer"})
    try:
        service = build_related_memory_service(container.root_dir, store, settings)
    except AggregateRepositoryFactoryError as error:
        return _json_response(
            409,
            {"detail": "related memory rejected", "reason": str(error)},
        )
    try:
        hits = service.query(
            RelatedMemoryQuery(object_id=object_id, layer=layer, limit=limit)
        )
    except RelatedMemoryError as error:
        return _json_response(400, {"detail": str(error)})
    return _json_response(
        200,
        {
            "object_id": object_id,
            "layer": layer,
            "related": [hit.to_payload() for hit in hits],
        },
    )
