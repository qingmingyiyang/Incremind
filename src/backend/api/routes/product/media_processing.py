"""Media processing ownership for the product API."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.video_auto_effect_runtime import VideoAutoEffectRuntime

from core.effect_log import InvalidEffectTransition
from core.product_core.source_output_memory_candidate import CreateMemoryCandidateFromSourceOutput
from core.product_core.source_output_memory_candidate_endpoint import (
    ServeSourceOutputMemoryCandidateEndpoint,
)
from core.product_core.video_workflow_endpoint import (
    ServeAudioAssetTranscriptionEndpoint,
    ServeTranscriptSummaryEndpoint,
    ServeVideoAudioExtractionEndpoint,
)

from . import document_delivery_services as product_document_delivery_services
from . import document_visibility as product_document_visibility
from . import http as product_http
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.post("/api/rebuild/sources/{source_id:path}/audio-track")
async def video_audio_extraction(
    request: Request,
    container: ApiContainerDep,
    source_id: str,
) -> JSONResponse:
    _ = source_id
    store, settings = product_repositories._object_store(container.root_dir)
    runtime = VideoAutoEffectRuntime(
        store, namespace_id=settings.namespace_id,
        effect_runner=request.app.state.effect_runtime.runner,
        gate_decision_id="video-audio-extraction:v1",
    )
    response = ServeVideoAudioExtractionEndpoint().execute(
        method=request.method,
        path=product_document_delivery_services._path_with_query(request),
        body=await product_http._json_body(request),
        extract_audio=lambda **_kwargs: runtime.extract_audio(source_id),
    )
    return product_http._json_response(response.status_code, response.body, response.headers)


@router.post("/api/rebuild/audio-assets/{audio_asset_id:path}/transcription")
async def audio_asset_transcription(
    request: Request,
    container: ApiContainerDep,
    audio_asset_id: str,
) -> JSONResponse:
    _ = audio_asset_id
    store, settings = product_repositories._object_store(container.root_dir)
    asset = store.read("audio_asset_refs", audio_asset_id)
    source_id = str((asset or {}).get("source_id") or "")
    runtime = VideoAutoEffectRuntime(
        store, namespace_id=settings.namespace_id,
        effect_runner=request.app.state.effect_runtime.runner,
        gate_decision_id="audio-asset-transcription:v1",
    )
    request_body = await product_http._json_body(request)
    response = await asyncio.to_thread(
        ServeAudioAssetTranscriptionEndpoint().execute,
        method=request.method,
        path=product_document_delivery_services._path_with_query(request),
        body=request_body,
        transcribe_audio=lambda **_kwargs: runtime.transcribe_audio(
            source_id, audio_asset_id,
        ),
    )
    return product_http._json_response(response.status_code, response.body, response.headers)


@router.post("/api/rebuild/sources/{source_id:path}/memory-candidate")
async def source_output_memory_candidate(
    request: Request,
    container: ApiContainerDep,
    source_id: str,
) -> JSONResponse:
    _ = source_id
    store, settings = product_repositories._object_store(container.root_dir)
    if source_id in product_document_visibility._workspace_review_sources(Path(container.root_dir)):
        return product_http._json_response(409, {"detail": "candidate creation moved", "reason": "review in Workspace and Recognition"}, product_http._no_store_headers())
    creator = CreateMemoryCandidateFromSourceOutput(store, namespace_id=settings.namespace_id)
    response = ServeSourceOutputMemoryCandidateEndpoint().execute(
        method=request.method,
        path=product_document_delivery_services._path_with_query(request),
        body=await product_http._json_body(request),
        create_from_content_read=creator.execute_from_content_read,
        create_from_media_output=creator.execute_from_media_output,
    )
    return product_http._json_response(response.status_code, response.body, response.headers)


@router.post("/api/rebuild/audio-assets/{audio_asset_id:path}/transcription/cancel")
async def cancel_audio_asset_transcription(
    request: Request,
    container: ApiContainerDep,
    audio_asset_id: str,
) -> JSONResponse:
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping) or body.get("confirm_cancel") is not True:
        return product_http._json_response(400, {"detail": "confirm_cancel=true is required"}, {"Cache-Control": "no-store"})
    store, _settings = product_repositories._object_store(container.root_dir)
    clean_asset_id = audio_asset_id.strip()
    asset = store.read("audio_asset_refs", clean_asset_id)
    if asset is None:
        return product_http._json_response(404, {"detail": "audio asset not found"}, {"Cache-Control": "no-store"})
    source_id = str(asset.get("source_id") or "").strip()
    operation_id = f"audio-auto:{source_id}:{clean_asset_id}:transcribe"
    try:
        request.app.state.effect_runtime.log.request_cancellation(
            operation_id,
            request_ref=f"api://audio-assets/{clean_asset_id}/transcription/cancel",
            now=int(datetime.now(timezone.utc).timestamp()),
        )
    except KeyError:
        return product_http._json_response(404, {"detail": "audio transcription Effect not found"}, {"Cache-Control": "no-store"})
    except InvalidEffectTransition:
        return product_http._json_response(409, {"detail": "audio transcription Effect is already terminal"}, {"Cache-Control": "no-store"})
    return product_http._json_response(202, {"status": "cancel_requested", "operation_id": operation_id}, {"Cache-Control": "no-store"})


@router.post("/api/rebuild/media-processing-outputs/{output_id:path}/summary")
async def transcript_summary(
    request: Request,
    container: ApiContainerDep,
    output_id: str,
) -> JSONResponse:
    _ = output_id
    store, settings = product_repositories._object_store(container.root_dir)
    output = store.read("media_processing_outputs", output_id)
    source_id = str((output or {}).get("source_id") or "")
    runtime = VideoAutoEffectRuntime(
        store, namespace_id=settings.namespace_id,
        effect_runner=request.app.state.effect_runtime.runner,
        gate_decision_id="transcript-summary:v1",
    )
    request_body = await product_http._json_body(request)
    response = await asyncio.to_thread(
        ServeTranscriptSummaryEndpoint().execute,
        method=request.method,
        path=product_document_delivery_services._path_with_query(request),
        body=request_body,
        summarize_transcript=lambda **_kwargs: runtime.summarize_transcript(
            source_id, output_id,
        ),
    )
    return product_http._json_response(response.status_code, response.body, response.headers)
