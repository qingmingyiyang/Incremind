"""Settings ownership for the product API."""
from __future__ import annotations
from backend.shared.server_resources import RESOURCE_POOL

from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep

from core.product_core.auto_memory_publication import (
    AutoMemoryPublicationError,
    GetAutoMemoryPublicationSettings,
    SaveAutoMemoryPublicationSettings,
    serialize_auto_memory_publication_settings,
)
from core.product_core.local_asr_provider_settings import (
    GetLocalAsrProviderSettings,
    SaveLocalAsrProviderSettings,
)
from core.product_core.local_asr_provider_settings_endpoint import (
    ServeLocalAsrProviderSettingsEndpoint,
)
from core.product_core.local_document_text_extractor_settings import (
    GetLocalDocumentTextExtractorSettings,
    SaveLocalDocumentTextExtractorSettings,
)
from core.product_core.local_document_text_extractor_settings_endpoint import (
    ServeLocalDocumentTextExtractorSettingsEndpoint,
)
from core.product_core.local_ocr_provider_settings import (
    GetLocalOcrProviderSettings,
    SaveLocalOcrProviderSettings,
)
from core.product_core.local_ocr_provider_settings_endpoint import (
    ServeLocalOcrProviderSettingsEndpoint,
)
from core.product_core.local_video_provider_settings import (
    GetLocalVideoProviderSettings,
    SaveLocalVideoProviderSettings,
)
from core.product_core.local_video_provider_settings_endpoint import (
    ServeLocalVideoProviderSettingsEndpoint,
)
from core.product_core.transcript_summary_adapter import (
    GetTranscriptSummarySettings,
    SaveTranscriptSummarySettings,
    TranscriptSummaryError,
    serialize_transcript_summary_settings,
)

from . import document_delivery_services as product_document_delivery_services
from . import http as product_http
from . import providers as product_providers
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.get("/api/rebuild/providers/deepseek/status")
def deepseek_four_layer_provider_status(container: ApiContainerDep) -> JSONResponse:
    body = product_providers._deepseek_provider_status(container)
    return product_http._json_response(200, body, {"Content-Type": "application/json", "Cache-Control": "no-store"})


@router.get("/api/rebuild/settings/local-ocr-provider")
@router.put("/api/rebuild/settings/local-ocr-provider")
async def local_ocr_provider_settings(request: Request, container: ApiContainerDep) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    response = ServeLocalOcrProviderSettingsEndpoint().execute(
        method=request.method,
        path=product_document_delivery_services._path_with_query(request),
        body=await product_http._json_body(request),
        get_settings=GetLocalOcrProviderSettings(store).execute,
        save_settings=SaveLocalOcrProviderSettings(store).execute,
    )
    return product_http._json_response(response.status_code, response.body, response.headers)


@router.get("/api/rebuild/settings/local-asr-provider")
@router.put("/api/rebuild/settings/local-asr-provider")
async def local_asr_provider_settings(request: Request, container: ApiContainerDep) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    pool = RESOURCE_POOL.get()
    model_root = pool.model_path('faster-whisper') if pool is not None else None
    response = ServeLocalAsrProviderSettingsEndpoint().execute(
        method=request.method,
        path=product_document_delivery_services._path_with_query(request),
        body=await product_http._json_body(request),
        get_settings=GetLocalAsrProviderSettings(store, model_root=model_root).execute,
        save_settings=SaveLocalAsrProviderSettings(store, model_root=model_root).execute,
    )
    return product_http._json_response(response.status_code, response.body, response.headers)


@router.get("/api/rebuild/settings/local-video-provider")
@router.put("/api/rebuild/settings/local-video-provider")
async def local_video_provider_settings(request: Request, container: ApiContainerDep) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    response = ServeLocalVideoProviderSettingsEndpoint().execute(
        method=request.method,
        path=product_document_delivery_services._path_with_query(request),
        body=await product_http._json_body(request),
        get_settings=GetLocalVideoProviderSettings(store).execute,
        save_settings=SaveLocalVideoProviderSettings(store).execute,
    )
    return product_http._json_response(response.status_code, response.body, response.headers)


@router.get("/api/rebuild/settings/local-document-text-extractor")
@router.put("/api/rebuild/settings/local-document-text-extractor")
async def local_document_text_extractor_settings(request: Request, container: ApiContainerDep) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    response = ServeLocalDocumentTextExtractorSettingsEndpoint().execute(
        method=request.method,
        path=product_document_delivery_services._path_with_query(request),
        body=await product_http._json_body(request),
        get_settings=GetLocalDocumentTextExtractorSettings(store).execute,
        save_settings=SaveLocalDocumentTextExtractorSettings(store).execute,
    )
    return product_http._json_response(response.status_code, response.body, response.headers)


@router.get("/api/rebuild/settings/transcript-summary-provider")
@router.put("/api/rebuild/settings/transcript-summary-provider")
async def transcript_summary_provider_settings(request: Request, container: ApiContainerDep) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    if request.method.upper() == "GET":
        return product_http._json_response(
            200,
            serialize_transcript_summary_settings(GetTranscriptSummarySettings(store).execute()),
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping):
        return product_http._json_response(
            400,
            {"detail": "request body must be a JSON object"},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    try:
        settings = SaveTranscriptSummarySettings(store).execute(
            enabled=product_http._required_body_bool(body, "enabled"),
            command=body.get("command") or [],
            provider_name=product_http._optional_body_str(body, "provider_name") or "local-semantic-extractive-summary",
            timeout_seconds=body.get("timeout_seconds", 600.0),
            confirm_enable=product_http._optional_body_bool(body, "confirm_enable") is True,
        )
    except (TranscriptSummaryError, ValueError) as error:
        return product_http._json_response(
            400,
            {
                "detail": "transcript summary provider settings rejected",
                "reason": str(error),
                "actionable": True,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        serialize_transcript_summary_settings(settings),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.get("/api/rebuild/settings/auto-memory-publication")
@router.put("/api/rebuild/settings/auto-memory-publication")
async def auto_memory_publication_settings(request: Request, container: ApiContainerDep) -> JSONResponse:
    store, _settings = product_repositories._object_store(container.root_dir)
    if request.method.upper() == "GET":
        return product_http._json_response(
            200,
            serialize_auto_memory_publication_settings(GetAutoMemoryPublicationSettings(store).execute()),
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    body = await product_http._json_body(request)
    try:
        settings = SaveAutoMemoryPublicationSettings(store).execute(
            enabled=product_http._required_body_bool(body, "enabled"),
            confirm_enable=product_http._optional_body_bool(body, "confirm_enable") is True,
            allowed_layers=product_http._optional_body_str_list(body, "allowed_layers")
            or ("atom", "scenario", "series_memory", "project_skill"),
        )
    except (AutoMemoryPublicationError, ValueError) as error:
        return product_http._json_response(
            400,
            {
                "detail": "auto memory publication settings rejected",
                "reason": str(error),
                "actionable": True,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        serialize_auto_memory_publication_settings(settings),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )
