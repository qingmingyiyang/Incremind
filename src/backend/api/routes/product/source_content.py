"""Source content ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.security import SafeTextNetworkAdapter

from core.effect_log import EffectClass, EffectWorkflowHandler
from core.product_core.developer_studio_config import GetDeveloperStudioConfig
from core.product_core.inspiration_system import (
    InspirationSystemError,
    RecordInspirationFromSource,
    serialize_inspiration_record_result,
)
from core.product_core.local_document_text_extractor_settings import (
    RunConfiguredLocalDocumentTextExtractorForSource,
)
from core.product_core.local_document_text_extractor_settings_endpoint import (
    ServeLocalDocumentTextExtractorRunEndpoint,
)
from core.product_core.series_memory_skill_draft import (
    CreateSeriesMemorySkillDraftsFromSourceStructure,
    SeriesMemorySkillDraftError,
    serialize_series_memory_skill_draft_result,
)
from core.product_core.source_content_read import (
    BookmarkCollectionWebContentReadResult,
    ReadBookmarkCollectionWebContent,
    ReadLinkWebContent,
    ReadSourceTextContent,
    SourceContentReadError,
    SourceContentReadResult,
    serialize_bookmark_collection_web_content_read_result,
    serialize_source_content_read_result,
)
from core.product_core.source_content_read_endpoint import ServeSourceContentReadEndpoint
from core.product_core.source_file_authorization_endpoint import (
    ServeSourceFileAuthorizationEndpoint,
)
from core.product_core.source_series_assignment import (
    ConfirmSourceSeriesAssignment,
    SourceSeriesAssignmentError,
    serialize_source_series_assignment_result,
)
from core.product_core.source_structuring import (
    SourceStructuringError,
    StructureSourceContent,
    serialize_source_structuring_result,
)

from . import developer_prompt_catalog as product_developer_prompt_catalog
from . import document_delivery_services as product_document_delivery_services
from . import http as product_http
from . import repositories as product_repositories
from . import source_authorization as product_source_authorization

router = APIRouter(tags=["rebuild-product-core"])


_URL_TEXT_NETWORK_ADAPTER = SafeTextNetworkAdapter()


@router.post("/api/rebuild/sources/{source_id:path}/file-authorization")
async def source_file_authorization(
    request: Request,
    container: ApiContainerDep,
    source_id: str,
) -> JSONResponse:
    _ = source_id
    store, settings = product_repositories._object_store(container.root_dir)
    response = ServeSourceFileAuthorizationEndpoint().execute(
        method=request.method,
        path=product_document_delivery_services._path_with_query(request),
        body=await product_http._json_body(request),
        authorize_file=product_source_authorization._source_file_authorizer(store, namespace_id=settings.namespace_id),
    )
    return product_http._json_response(response.status_code, response.body, response.headers)


@router.post("/api/rebuild/sources/{source_id:path}/document-text")
async def local_document_text_extractor_run(
    request: Request,
    container: ApiContainerDep,
    source_id: str,
) -> JSONResponse:
    _ = source_id
    store, settings = product_repositories._object_store(container.root_dir)
    extractor = RunConfiguredLocalDocumentTextExtractorForSource(
        store, namespace_id=settings.namespace_id,
    )
    effects = EffectWorkflowHandler(
        request.app.state.effect_runtime.runner,
        store,
        namespace_id=settings.namespace_id,
    )

    def run_extractor(*, source_id: str) -> SourceContentReadResult:
        return effects.execute(
            operation_id=f"document-text:{source_id}:extract",
            session_id=f"document-text:{source_id}",
            root_id=f"document-text:{source_id}",
            step_key="extract",
            kind="workbench_auto_document_extract",
            intent_ref=(
                f"crp://{settings.namespace_id}/workflow-intents/"
                f"document-text/{source_id}/extract"
            ),
            gate_decision_id="document-text:v2",
            rev_set={"handler_revision": "document-extractor-v1"},
            payload={"source_id": source_id},
            effect_class=EffectClass.IDEMPOTENT,
            invoke=lambda: extractor.execute(source_id=source_id),
            encode=serialize_source_content_read_result,
            decode=lambda value: SourceContentReadResult(
                **{
                    **dict(value),
                    "activity_refs": tuple(value.get("activity_refs", ())),
                }
            ),
        )

    response = ServeLocalDocumentTextExtractorRunEndpoint().execute(
        method=request.method,
        path=product_document_delivery_services._path_with_query(request),
        body=await product_http._json_body(request),
        run_extractor=run_extractor,
    )
    return product_http._json_response(response.status_code, response.body, response.headers)


@router.post("/api/rebuild/sources/{source_id:path}/content-read")
async def source_text_content_read(
    request: Request,
    container: ApiContainerDep,
    source_id: str,
) -> JSONResponse:
    _ = source_id
    store, settings = product_repositories._object_store(container.root_dir)
    response = ServeSourceContentReadEndpoint().execute(
        method=request.method,
        path=product_document_delivery_services._path_with_query(request),
        read_content=ReadSourceTextContent(
            store,
            namespace_id=settings.namespace_id,
        ).execute,
    )
    return product_http._json_response(response.status_code, response.body, response.headers)


@router.post("/api/rebuild/sources/{source_id:path}/web-content")
async def link_web_content_read(
    request: Request,
    container: ApiContainerDep,
    source_id: str,
) -> JSONResponse:
    _ = source_id
    store, settings = product_repositories._object_store(container.root_dir)
    reader = ReadLinkWebContent(
        store, namespace_id=settings.namespace_id, fetch_url=_fetch_url_text,
    )
    effects = EffectWorkflowHandler(
        request.app.state.effect_runtime.runner, store,
        namespace_id=settings.namespace_id,
    )

    def read_content(*, source_id: str) -> SourceContentReadResult:
        return effects.execute(
            operation_id=f"web-content:{source_id}:read",
            session_id=f"web-content:{source_id}", root_id=f"web-content:{source_id}",
            step_key="read", kind="workbench_auto_fetch_url",
            intent_ref=f"crp://{settings.namespace_id}/workflow-intents/web-content/{source_id}/read",
            gate_decision_id="web-content:v2",
            rev_set={"handler_revision": "safe-text-fetch-v1"},
            payload={"source_id": source_id}, effect_class=EffectClass.QUERYABLE,
            invoke=lambda: reader.execute(source_id=source_id),
            encode=serialize_source_content_read_result,
            decode=lambda value: SourceContentReadResult(
                **{**dict(value), "activity_refs": tuple(value.get("activity_refs", ()))},
            ),
        )

    response = ServeSourceContentReadEndpoint().execute(
        method=request.method,
        path=product_document_delivery_services._path_with_query(request).replace("/web-content", "/content-read"),
        read_content=read_content,
    )
    return product_http._json_response(response.status_code, response.body, response.headers)


@router.post("/api/rebuild/sources/{source_id:path}/collection-web-content")
async def bookmark_collection_web_content_read(
    request: Request,
    container: ApiContainerDep,
    source_id: str,
) -> JSONResponse:
    store, settings = product_repositories._object_store(container.root_dir)
    reader = ReadBookmarkCollectionWebContent(
        store, namespace_id=settings.namespace_id, fetch_url=_fetch_url_text,
    )
    effects = EffectWorkflowHandler(
        request.app.state.effect_runtime.runner, store,
        namespace_id=settings.namespace_id,
    )
    try:
        result = effects.execute(
            operation_id=f"collection-web-content:{source_id}:read",
            session_id=f"collection-web-content:{source_id}",
            root_id=f"collection-web-content:{source_id}",
            step_key="read", kind="workbench_auto_fetch_url",
            intent_ref=(
                f"crp://{settings.namespace_id}/workflow-intents/"
                f"collection-web-content/{source_id}/read"
            ),
            gate_decision_id="collection-web-content:v2",
            rev_set={"handler_revision": "safe-text-fetch-v1"},
            payload={"source_id": source_id}, effect_class=EffectClass.QUERYABLE,
            invoke=lambda: reader.execute(source_id=source_id),
            encode=serialize_bookmark_collection_web_content_read_result,
            decode=_decode_bookmark_collection_web_content_result,
        )
    except SourceContentReadError as error:
        status_code = 404 if str(error) == "source not found" else 400
        return product_http._json_response(
            status_code,
            {
                "detail": "bookmark collection web content read rejected",
                "reason": str(error),
                "actionable": True,
                "memory_publication_state": "not_published",
                "blocked_operations": [
                    "cookie_read",
                    "video_downloader_execution",
                    "model_provider_execution",
                    "memory_candidate_auto_creation",
                    "long_term_memory_publication",
                ],
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        serialize_bookmark_collection_web_content_read_result(result),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.post("/api/rebuild/sources/{source_id:path}/structure-content")
async def source_content_structure(
    request: Request,
    container: ApiContainerDep,
    source_id: str,
) -> JSONResponse:
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    organization_prompt_refs = product_developer_prompt_catalog._developer_studio_prompt_refs(
        store,
        ("pt-classification", "pt-summary", "pt-tags", "pt-longterm-organize"),
    )
    # 旧 task_model_map 仅用于保留历史 model_profile_refs 追踪元数据。
    # 它不解析或调用 Provider；生产模型选择只由 Model Route Registry 决定。
    developer_studio_config = GetDeveloperStudioConfig(store).execute()
    try:
        result = StructureSourceContent(store, namespace_id=settings.namespace_id).execute(
            source_id=source_id,
            content_read_id=product_http._optional_body_str(body, "content_read_id"),
            prompt_context=organization_prompt_refs,
            task_model_map=developer_studio_config.task_model_map,
        )
    except SourceStructuringError as error:
        status_code = 404 if str(error) == "source not found" else 400
        return product_http._json_response(
            status_code,
            {
                "detail": "source content structuring rejected",
                "reason": str(error),
                "actionable": True,
                "memory_publication_state": "not_published",
                "blocked_operations": [
                    "model_provider_execution",
                    "automatic_series_write",
                    "memory_candidate_auto_creation",
                    "long_term_memory_publication",
                ],
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        serialize_source_structuring_result(result),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.post("/api/rebuild/sources/{source_id:path}/series-assignment")
async def source_series_assignment(
    request: Request,
    container: ApiContainerDep,
    source_id: str,
) -> JSONResponse:
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    try:
        result = ConfirmSourceSeriesAssignment(store, namespace_id=settings.namespace_id).execute(
            source_id=source_id,
            confirm=body.get("confirm") is True,
            series_name=product_http._optional_body_str(body, "series_name"),
            reason=product_http._optional_body_str(body, "reason"),
        )
        layered_drafts = CreateSeriesMemorySkillDraftsFromSourceStructure(
            store,
            namespace_id=settings.namespace_id,
        ).execute(
            source_id=source_id,
            project_id=product_http._optional_body_str(body, "project_id"),
        )
    except SourceSeriesAssignmentError as error:
        status_code = 404 if str(error) == "source not found" else 400
        return product_http._json_response(
            status_code,
            {
                "detail": "source series assignment rejected",
                "reason": str(error),
                "actionable": True,
                "memory_publication_state": "not_published",
                "blocked_operations": [
                    "automatic_series_write",
                    "model_provider_execution",
                    "memory_candidate_auto_creation",
                    "long_term_memory_publication",
                ],
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    except SeriesMemorySkillDraftError as error:
        status_code = 404 if str(error) == "source not found" else 400
        return product_http._json_response(
            status_code,
            {
                "detail": "source series assignment accepted but layered draft creation failed",
                "reason": str(error),
                "actionable": True,
                "memory_publication_state": "not_published",
                "blocked_operations": [
                    "automatic_memory_publication",
                    "auto_promote_memory",
                    "model_provider_execution",
                    "project_skill_overwrite",
                ],
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    body = serialize_source_series_assignment_result(result)
    body["layered_memory_drafts"] = serialize_series_memory_skill_draft_result(layered_drafts)
    return product_http._json_response(
        200,
        body,
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.post("/api/rebuild/sources/{source_id:path}/inspiration")
async def source_inspiration_record(
    request: Request,
    container: ApiContainerDep,
    source_id: str,
) -> JSONResponse:
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    try:
        result = RecordInspirationFromSource(store, namespace_id=settings.namespace_id).execute(
            source_id=source_id,
            content_read_id=product_http._optional_body_str(body, "content_read_id"),
            text=product_http._optional_body_text(body, "text"),
            project_id=product_http._optional_body_str(body, "project_id"),
            series_name=product_http._optional_body_str(body, "series_name"),
            theme_hint=product_http._optional_body_str(body, "theme_hint"),
        )
    except InspirationSystemError as error:
        status_code = 404 if str(error) == "source not found" else 400
        return product_http._json_response(
            status_code,
            {
                "detail": "source inspiration recording rejected",
                "reason": str(error),
                "actionable": True,
                "memory_publication_state": "not_published",
                "blocked_operations": [
                    "model_provider_execution",
                    "automatic_long_term_memory_publication",
                    "external_agent_write",
                ],
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        serialize_inspiration_record_result(result),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.post("/api/rebuild/sources/{source_id:path}/series-memory-skill-drafts")
async def source_series_memory_skill_drafts(
    request: Request,
    container: ApiContainerDep,
    source_id: str,
) -> JSONResponse:
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    try:
        result = CreateSeriesMemorySkillDraftsFromSourceStructure(
            store,
            namespace_id=settings.namespace_id,
        ).execute(
            source_id=source_id,
            project_id=product_http._optional_body_str(body, "project_id"),
        )
    except SeriesMemorySkillDraftError as error:
        status_code = 404 if str(error) == "source not found" else 400
        return product_http._json_response(
            status_code,
            {
                "detail": "series memory and project skill draft rejected",
                "reason": str(error),
                "actionable": True,
                "memory_publication_state": "not_published",
                "blocked_operations": [
                    "automatic_memory_publication",
                    "auto_promote_memory",
                    "model_provider_execution",
                    "project_skill_overwrite",
                ],
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        serialize_series_memory_skill_draft_result(result),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


def _fetch_url_text(url: str) -> str:
    return _URL_TEXT_NETWORK_ADAPTER.fetch_text(url)


def _decode_bookmark_collection_web_content_result(
    value: Mapping[str, object],
) -> BookmarkCollectionWebContentReadResult:
    children = tuple(
        SourceContentReadResult(
            **{
                **dict(child),
                "activity_refs": tuple(child.get("activity_refs", ())),
            }
        )
        for child in value.get("child_results", ())
        if isinstance(child, Mapping)
    )
    return BookmarkCollectionWebContentReadResult(
        **{
            **dict(value),
            "child_source_ids": tuple(value.get("child_source_ids", ())),
            "child_results": children,
            "blocked_operations": tuple(value.get("blocked_operations", ())),
        }
    )
