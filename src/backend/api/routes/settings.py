from __future__ import annotations

from threading import Lock
from threading import Thread
from time import time
from uuid import uuid4
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
import httpx

from backend.api.container import ApiContainerDep
from backend.api.provider_credentials import store_provider_credential
from backend.api.contracts import (
    FasterWhisperModelResponse,
    ModelRouteBatchUpdateResponse,
    ModelRouteDetailResponse,
    ModelRouteDraftRequest,
    ModelRouteListResponse,
    ModelRoutePreviewResponse,
    ProviderModelsResponse,
    ProviderDisconnectPreviewResponse,
    ProviderEgressConsentRequest,
    ProviderEgressConsentResponse,
    ProviderRecordRequest,
    ProviderRecordResponse,
    ProviderSecretStatusResponse,
    ProviderSettingsResponse,
    RagModelResponse,
    SaveProviderSecretRequest,
    TestProviderSettingsResponse,
    UpdateProviderRecordRequest,
    UpdateModelRouteRequest,
    UpdateModelRouteBatchRequest,
    UpdateProviderSettingsRequest,
    UpdateWorkspaceSettingsRequest,
    WorkspaceSettingsResponse,
)
from backend.api.sse import stream_progress_events
from backend.providers import ProviderRegistry
from backend.model_provider_health import ModelProviderHealthError, ModelProviderHealthStore
from backend.model_route_context import model_route_provider_context
from core.product_core.model_dispatch_authority import model_dispatch_authority_fence
from core.product_core.model_route_registry import (
    ModelRouteRegistry,
    ModelRouteRegistryConflict,
    ModelRouteRegistryError,
    ModelRouteRegistryNotFound,
)
from backend.security import (
    DEFAULT_PROVIDER_EGRESS_CATEGORIES,
    DEFAULT_PROVIDER_EGRESS_MAX_BYTES,
    DEFAULT_PROVIDER_EGRESS_PURPOSES,
    ProviderEgressError,
    ProviderEgressPolicyStore,
    SecretEgressBroker,
    build_provider_egress_guard,
)
from backend.shared.llm.base_url import resolve_openai_compatible_api_base_url
from backend.video_summary.infrastructure.settings import load_settings
from core.effect_log import (
    EffectClass,
    EffectHandlerRegistration,
    EffectIntent,
    EffectState,
    EffectWorkflowHandler,
)

router = APIRouter()
_ASR_DOWNLOAD_LOCK = Lock()
_ACTIVE_ASR_DOWNLOADS: set[str] = set()


def _provider_secret_headers(
    container: object, *, provider_id: str, url: str, purpose: str,
    boundary_revision: str,
) -> dict[str, str]:
    host = urlsplit(url).hostname
    if not host:
        raise ProviderEgressError("Provider endpoint host is unavailable")
    project_id = f"provider:{provider_id}"
    def current_revision(project: str) -> str:
        if project != project_id:
            raise ProviderEgressError("Provider secret project drifted")
        item = ProviderRegistry(container.root_dir).get_readonly(
            provider_id, fallback=_provider_fallback(container),
        )
        policy = ProviderEgressPolicyStore(container.root_dir)
        return _provider_egress_manifest(item, policy).manifest_id
    broker = SecretEgressBroker(
        container.secret_store, boundary_revision_reader=current_revision,
    )
    lease = broker.grant(
        project_id=project_id, secret_ref=project_id, purpose=purpose,
        allowed_hosts=(host,), boundary_revision=boundary_revision, ttl_seconds=30,
    )
    try:
        return broker.inject_header(
            lease, project_id=project_id, purpose=purpose,
            boundary_revision=boundary_revision, url=url,
            header_name="Authorization", prefix="Bearer ",
        )
    finally:
        broker.revoke(lease.lease_id)


def _provider_secret_value(
    container: object, *, provider_id: str, url: str, purpose: str,
    boundary_revision: str,
) -> str:
    headers = _provider_secret_headers(
        container, provider_id=provider_id, url=url, purpose=purpose,
        boundary_revision=boundary_revision,
    )
    authorization = headers.get("Authorization", "")
    return authorization.removeprefix("Bearer ")


@router.get("/api/settings", response_model=WorkspaceSettingsResponse)
def get_workspace_settings(container: ApiContainerDep) -> WorkspaceSettingsResponse:
    try:
        settings = container.settings_service.get_workspace_settings()
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return WorkspaceSettingsResponse(
        theme=settings.theme,
        show_takeaways=settings.show_takeaways,
        profile_name=settings.profile_name,
        font_scale=settings.font_scale,
        content_density=settings.content_density,
        transcript_enhancement_enabled=settings.transcript_enhancement_enabled,
        asr_model_quality=settings.asr_model_quality,
        transcription_mode=settings.transcription_mode,
        rag_embedding_device=settings.rag_embedding_device,
        rag_max_hits=settings.rag_max_hits,
        rag_rerank_enabled=settings.rag_rerank_enabled,
        window_tokens=settings.window_tokens,
        answer_detail_level=settings.answer_detail_level,
        reasoning_effort=settings.reasoning_effort,
        talk_custom_prompt=settings.talk_custom_prompt,
        video_generation_concurrency=settings.video_generation_concurrency,
        web_search_enabled=settings.web_search_enabled,
        chaoxing_request_delay_seconds=settings.chaoxing_request_delay_seconds,
        chaoxing_init_course_delay_seconds=settings.chaoxing_init_course_delay_seconds,
    )


@router.put("/api/settings", response_model=WorkspaceSettingsResponse)
async def update_workspace_settings(
    request: UpdateWorkspaceSettingsRequest,
    container: ApiContainerDep,
) -> WorkspaceSettingsResponse:
    try:
        settings = container.settings_service.update_workspace_settings(
            theme=request.theme,
            show_takeaways=request.show_takeaways,
            profile_name=request.profile_name,
            font_scale=request.font_scale,
            content_density=request.content_density,
            transcript_enhancement_enabled=request.transcript_enhancement_enabled,
            asr_model_quality=request.asr_model_quality,
            transcription_mode=request.transcription_mode,
            rag_embedding_device=request.rag_embedding_device,
            rag_max_hits=request.rag_max_hits,
            rag_rerank_enabled=request.rag_rerank_enabled,
            window_tokens=request.window_tokens,
            answer_detail_level=request.answer_detail_level,
            reasoning_effort=request.reasoning_effort,
            talk_custom_prompt=request.talk_custom_prompt,
            video_generation_concurrency=request.video_generation_concurrency,
            web_search_enabled=request.web_search_enabled,
            chaoxing_request_delay_seconds=request.chaoxing_request_delay_seconds,
            chaoxing_init_course_delay_seconds=request.chaoxing_init_course_delay_seconds,
        )
        container.invalidate_agent_graph_service()
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    container.generate_video_summary.update_video_generation_concurrency(
        settings.video_generation_concurrency
    )
    container.chaoxing_importer.configure_delays(
        request_delay_seconds=settings.chaoxing_request_delay_seconds,
        init_course_delay_seconds=settings.chaoxing_init_course_delay_seconds,
    )

    return WorkspaceSettingsResponse(
        theme=settings.theme,
        show_takeaways=settings.show_takeaways,
        profile_name=settings.profile_name,
        font_scale=settings.font_scale,
        content_density=settings.content_density,
        transcript_enhancement_enabled=settings.transcript_enhancement_enabled,
        asr_model_quality=settings.asr_model_quality,
        transcription_mode=settings.transcription_mode,
        rag_embedding_device=settings.rag_embedding_device,
        rag_max_hits=settings.rag_max_hits,
        rag_rerank_enabled=settings.rag_rerank_enabled,
        window_tokens=settings.window_tokens,
        answer_detail_level=settings.answer_detail_level,
        reasoning_effort=settings.reasoning_effort,
        talk_custom_prompt=settings.talk_custom_prompt,
        video_generation_concurrency=settings.video_generation_concurrency,
        web_search_enabled=settings.web_search_enabled,
        chaoxing_request_delay_seconds=settings.chaoxing_request_delay_seconds,
        chaoxing_init_course_delay_seconds=settings.chaoxing_init_course_delay_seconds,
    )


@router.get("/api/provider-settings", response_model=ProviderSettingsResponse)
def get_provider_settings(container: ApiContainerDep) -> ProviderSettingsResponse:
    try:
        env_settings = container.settings_service.get_provider_settings()
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return ProviderSettingsResponse(
        llm_provider=env_settings.llm_provider,
        openai_base_url=env_settings.openai_base_url,
        openai_model=env_settings.openai_model,
        has_openai_api_key=env_settings.has_openai_api_key,
        openai_api_key_masked=env_settings.openai_api_key_masked,
        hf_endpoint=env_settings.hf_endpoint,
    )


@router.get("/api/provider-settings/openai-api-key", response_model=ProviderSecretStatusResponse)
def get_provider_openai_api_key(container: ApiContainerDep) -> ProviderSecretStatusResponse:
    return ProviderSecretStatusResponse(has_api_key=container.settings_service.has_openai_api_key())


@router.get("/api/providers", response_model=list[ProviderRecordResponse])
def list_providers(container: ApiContainerDep) -> list[ProviderRecordResponse]:
    registry = ProviderRegistry(container.root_dir)
    return [
        _provider_response(item, container)
        for item in registry.list_readonly(fallback=_provider_fallback(container))
    ]


@router.get("/api/model-routes", response_model=ModelRouteListResponse)
def list_model_routes(container: ApiContainerDep) -> ModelRouteListResponse:
    try:
        return ModelRouteListResponse(**ModelRouteRegistry(container.root_dir).list())
    except ModelRouteRegistryError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.put("/api/model-routes/batch", response_model=ModelRouteBatchUpdateResponse)
def update_model_route_batch(
    request: UpdateModelRouteBatchRequest,
    container: ApiContainerDep,
) -> ModelRouteBatchUpdateResponse:
    routes = ModelRouteRegistry(container.root_dir)
    assignments = []
    try:
        for assignment in request.assignments:
            values = assignment.model_dump(exclude={"route_key"})
            provider, consented = model_route_provider_context(container, values)
            assignments.append((assignment.route_key, values, provider, consented))
        result = routes.update_batch(
            assignments,
            expected_registry_revision=request.expected_registry_revision,
        )
    except KeyError as error:
        raise HTTPException(status_code=409, detail="模型路由引用的供应商不存在。") from error
    except ModelRouteRegistryConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ModelRouteRegistryError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return ModelRouteBatchUpdateResponse(**result)


@router.get("/api/model-routes/{route_key}", response_model=ModelRouteDetailResponse)
def get_model_route(route_key: str, container: ApiContainerDep) -> ModelRouteDetailResponse:
    routes = ModelRouteRegistry(container.root_dir)
    try:
        result = routes.get(route_key)
        provider, consented = model_route_provider_context(container, result["route"])
        routes.validate_provider_reference(result["route"], provider=provider, egress_consented=consented)
    except ModelRouteRegistryNotFound as error:
        raise HTTPException(status_code=404, detail="模型路由不存在。") from error
    except KeyError as error:
        raise HTTPException(status_code=409, detail="模型路由引用的供应商已不存在。") from error
    except ModelRouteRegistryError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return ModelRouteDetailResponse(**result)


@router.post("/api/model-routes/{route_key}/preview", response_model=ModelRoutePreviewResponse)
def preview_model_route(
    route_key: str,
    request: ModelRouteDraftRequest,
    container: ApiContainerDep,
) -> ModelRoutePreviewResponse:
    routes = ModelRouteRegistry(container.root_dir)
    values = request.model_dump()
    try:
        provider, consented = model_route_provider_context(container, values)
        result = routes.preview(route_key, values, provider=provider, egress_consented=consented)
    except KeyError as error:
        raise HTTPException(status_code=409, detail="模型路由引用的供应商不存在。") from error
    except ModelRouteRegistryError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return ModelRoutePreviewResponse(**result)


@router.put("/api/model-routes/{route_key}", response_model=ModelRouteDetailResponse)
def update_model_route(
    route_key: str,
    request: UpdateModelRouteRequest,
    container: ApiContainerDep,
) -> ModelRouteDetailResponse:
    routes = ModelRouteRegistry(container.root_dir)
    values = request.model_dump(exclude={"expected_registry_revision"})
    try:
        provider, consented = model_route_provider_context(container, values)
        result = routes.update(
            route_key,
            values,
            expected_registry_revision=request.expected_registry_revision,
            provider=provider,
            egress_consented=consented,
        )
    except KeyError as error:
        raise HTTPException(status_code=409, detail="模型路由引用的供应商不存在。") from error
    except ModelRouteRegistryConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ModelRouteRegistryError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return ModelRouteDetailResponse(**result)


@router.post("/api/providers", response_model=ProviderRecordResponse, status_code=201)
def create_provider(
    request: ProviderRecordRequest,
    container: ApiContainerDep,
) -> ProviderRecordResponse:
    registry = ProviderRegistry(container.root_dir)
    try:
        item = registry.create(request.model_dump(), fallback=_provider_fallback(container))
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _provider_response(item, container)


@router.patch("/api/providers/{provider_id}", response_model=ProviderRecordResponse)
def update_provider(
    provider_id: str,
    request: UpdateProviderRecordRequest,
    container: ApiContainerDep,
) -> ProviderRecordResponse:
    registry = ProviderRegistry(container.root_dir)
    try:
        item = registry.update(
            provider_id,
            request.model_dump(exclude_none=True),
            fallback=_provider_fallback(container),
        )
        if item["is_active"]:
            _apply_active_provider(item, container)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="供应商不存在。") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return _provider_response(item, container)


@router.post("/api/providers/{provider_id}/activate", response_model=ProviderRecordResponse)
def activate_provider(provider_id: str, container: ApiContainerDep) -> ProviderRecordResponse:
    registry = ProviderRegistry(container.root_dir)
    try:
        item = registry.get(provider_id, fallback=_provider_fallback(container))
        if not item["enabled"]:
            raise ValueError("已停用的供应商不能激活。")
        item = registry.activate(provider_id, fallback=_provider_fallback(container))
        _apply_active_provider(item, container)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="供应商不存在。") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return _provider_response(item, container)


@router.get(
    "/api/providers/{provider_id}/disconnect-preview",
    response_model=ProviderDisconnectPreviewResponse,
)
def provider_disconnect_preview(
    provider_id: str,
    container: ApiContainerDep,
) -> ProviderDisconnectPreviewResponse:
    registry = ProviderRegistry(container.root_dir)
    try:
        provider = registry.get_readonly(
            provider_id, fallback=_provider_fallback(container),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="供应商不存在。") from error

    route_state, referenced_routes = _provider_route_references(container, provider_id)
    policy = ProviderEgressPolicyStore(container.root_dir)
    manifest = _provider_egress_manifest(provider, policy)
    egress_consented = policy.is_consented(manifest)
    replacements = []
    for item in registry.list_readonly(fallback=_provider_fallback(container)):
        if str(item.get("provider_id") or "") == provider_id or item.get("enabled") is not True:
            continue
        item_manifest = _provider_egress_manifest(item, policy)
        item_has_key = container.secret_store.has_secret(f"provider:{item['provider_id']}")
        item_ready = bool(
            item.get("model")
            and (not item_manifest.external or item_has_key)
            and (not item_manifest.external or policy.is_consented(item_manifest))
        )
        replacements.append(
            {
                "provider_id": str(item["provider_id"]),
                "name": str(item["name"]),
                "model": str(item.get("model") or ""),
                "ready": item_ready,
            }
        )
    blockers = []
    if provider.get("is_active") is True:
        blockers.append("active_default")
    if referenced_routes:
        blockers.append("route_reference")
    return ProviderDisconnectPreviewResponse(
        provider_id=provider_id,
        name=str(provider["name"]),
        is_active=provider.get("is_active") is True,
        has_api_key=container.secret_store.has_secret(f"provider:{provider_id}"),
        egress_external=manifest.external,
        egress_consented=egress_consented,
        route_registry_revision=int(route_state.get("registry_revision") or 0),
        referenced_routes=referenced_routes,
        replacement_candidates=sorted(replacements, key=lambda item: item["provider_id"]),
        delete_blockers=blockers,
        can_delete=not blockers,
        disconnect_effect=(
            "model_features_paused"
            if provider.get("is_active") is True or referenced_routes
            else "provider_connection_removed"
        ),
    )


@router.delete("/api/providers/{provider_id}", status_code=204)
def delete_provider(provider_id: str, container: ApiContainerDep) -> None:
    with model_dispatch_authority_fence(container.root_dir):
        registry = ProviderRegistry(container.root_dir)
        try:
            _route_state, route_references = _provider_route_references(container, provider_id)
            referenced_routes = [str(route["route_key"]) for route in route_references]
            if referenced_routes:
                labels = "、".join(referenced_routes)
                raise ValueError(f"该供应商仍被模型路由使用（{labels}），请先调整路由。")
            registry.delete(provider_id, fallback=_provider_fallback(container))
        except KeyError as error:
            raise HTTPException(status_code=404, detail="供应商不存在。") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        container.secret_store.delete(f"provider:{provider_id}")
        ProviderEgressPolicyStore(container.root_dir).revoke(provider_id)
        _invalidate_provider_health(container, provider_id)
        container.invalidate_agent_graph_service()


@router.get("/api/providers/{provider_id}/models", response_model=ProviderModelsResponse)
def fetch_provider_models(
    provider_id: str, request: Request, container: ApiContainerDep,
) -> ProviderModelsResponse:
    registry = ProviderRegistry(container.root_dir)
    try:
        item = registry.get_readonly(
            provider_id, fallback=_provider_fallback(container),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="供应商不存在。") from error
    base_url = resolve_openai_compatible_api_base_url(item["base_url"])
    if not base_url:
        raise HTTPException(status_code=400, detail="请先填写模型服务地址。")
    policy = ProviderEgressPolicyStore(container.root_dir)
    manifest = _provider_egress_manifest(item, policy)
    try:
        policy.validate(
            manifest,
            purpose="model_discovery",
            payload_categories=("provider_metadata",),
            payload_bytes=0,
        )
    except ProviderEgressError as error:
        raise HTTPException(status_code=403, detail=f"模型外发未授权：{error}") from error
    operation_id = f"provider-model-discovery:{provider_id}:{uuid4()}"
    receipt_store, namespace_id = _provider_model_receipt_store(container)
    workflow = EffectWorkflowHandler(
        request.app.state.effect_runtime.runner,
        receipt_store,
        namespace_id=namespace_id,
    )
    intent = EffectIntent(
        session_id=f"provider-settings:{provider_id}",
        root_id=f"provider-settings:{provider_id}",
        step_key="discover_models",
        kind="provider_model_discovery",
        intent_ref=f"provider://{provider_id}/models",
        gate_decision_id=manifest.manifest_id,
        rev_set={"provider_revision": _provider_revision(item)},
        payload={"provider_id": provider_id},
        effect_class=EffectClass.QUERYABLE,
        operation_id_override=operation_id,
    )
    try:
        settled = request.app.state.effect_runtime.execute(intent, now=int(time()))
    except ProviderEgressError as error:
        raise HTTPException(status_code=403, detail=f"模型外发未授权：{error}") from error
    except ProviderModelDiscoveryError as error:
        raise HTTPException(status_code=503, detail=f"获取模型失败：{error}") from error
    if settled.state is not EffectState.SETTLED_OK:
        raise HTTPException(status_code=503, detail="模型发现操作正在恢复，请稍后重试。")
    result = workflow.load_result(operation_id, lambda value: dict(value))
    models = [str(value) for value in result.get("models", [])]
    if not models:
        raise HTTPException(status_code=502, detail="供应商未返回可识别的模型列表。")
    return ProviderModelsResponse(provider_id=provider_id, models=models)


def _provider_model_receipt_store(container: ApiContainerDep):
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store

    store, settings = build_rebuild_object_store(container.root_dir)
    return store, settings.namespace_id


class ProviderModelDiscoveryError(RuntimeError):
    pass


def register_provider_model_discovery_handler(effect_runtime, container) -> None:
    """Load the restart-safe Provider transport into the Core Handler registry."""

    receipt_store, namespace_id = _provider_model_receipt_store(container)
    workflow = EffectWorkflowHandler(
        effect_runtime.runner, receipt_store, namespace_id=namespace_id,
    )

    def resolve(effect):
        prefix = "provider-settings:"
        provider_id = effect.root_id[len(prefix):] if effect.root_id.startswith(prefix) else ""
        if not provider_id or effect.intent_ref != f"provider://{provider_id}/models":
            raise ProviderModelDiscoveryError("Provider Effect identity 无法重建")
        try:
            item = ProviderRegistry(container.root_dir).get_readonly(
                provider_id, fallback=_provider_fallback(container),
            )
        except KeyError as error:
            raise ProviderModelDiscoveryError("供应商不存在") from error
        frozen_revision = str(effect.rev_set.get("provider_revision") or "")
        current_revision = _provider_revision(item)
        if current_revision != frozen_revision:
            raise ProviderModelDiscoveryError("供应商 revision 已漂移")
        base_url = resolve_openai_compatible_api_base_url(str(item.get("base_url") or ""))
        if not base_url:
            raise ProviderModelDiscoveryError("模型服务地址不可用")
        policy = ProviderEgressPolicyStore(container.root_dir)
        manifest = _provider_egress_manifest(item, policy)
        if manifest.manifest_id != effect.gate_decision_id:
            raise ProviderModelDiscoveryError("Provider Egress Gate 已漂移")
        return provider_id, item, base_url, policy, manifest

    def handle(effect) -> str:
        provider_id, _item, base_url, policy, manifest = resolve(effect)
        lease = policy.authorize(
            manifest,
            purpose="model_discovery",
            payload_categories=("provider_metadata",),
            payload_bytes=0,
        )
        headers = _provider_secret_headers(
            container, provider_id=provider_id, url=f"{base_url}/models",
            purpose="model_discovery", boundary_revision=manifest.manifest_id,
        )
        try:
            response = httpx.get(f"{base_url}/models", headers=headers, timeout=15.0)
            response.raise_for_status()
            payload = response.json()
            models = sorted({
                str(entry.get("id", "")).strip()
                for entry in payload.get("data", [])
                if isinstance(entry, dict) and str(entry.get("id", "")).strip()
            })
            lease.finish("succeeded")
        except (httpx.HTTPError, ValueError, TypeError) as error:
            lease.finish("failed", error_code="provider_request_failed")
            raise ProviderModelDiscoveryError(str(error)) from error
        return workflow.record_result(effect, {"models": models}, encode=lambda value: value)

    def probe(effect):
        state, outcome_ref = workflow.verify_effect(effect)
        if state is not EffectState.PLANNED:
            return state, outcome_ref
        try:
            _provider_id, _item, _base_url, policy, manifest = resolve(effect)
        except ProviderModelDiscoveryError as error:
            return EffectState.SETTLED_ERR, f"provider.discovery.drift:{error}"
        if not policy.is_consented(manifest):
            return EffectState.UNKNOWN, "provider.discovery.consent_revoked"
        if effect.attempt >= 3:
            return EffectState.UNKNOWN, "provider.discovery.retry_budget_exhausted"
        return EffectState.PLANNED, None

    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind="provider_model_discovery",
        effect_class=EffectClass.QUERYABLE,
        handler=handle,
        probe=probe,
    ))


@router.post("/api/providers/{provider_id}/egress-consent", response_model=ProviderEgressConsentResponse)
def grant_provider_egress_consent(
    provider_id: str,
    request: ProviderEgressConsentRequest,
    container: ApiContainerDep,
) -> ProviderEgressConsentResponse:
    registry = ProviderRegistry(container.root_dir)
    try:
        provider = registry.get(provider_id, fallback=_provider_fallback(container))
        policy = ProviderEgressPolicyStore(container.root_dir)
        manifest = _provider_egress_manifest(provider, policy)
        policy.grant(manifest, manifest_id=request.manifest_id, confirm=request.confirm)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="供应商不存在。") from error
    except ProviderEgressError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return ProviderEgressConsentResponse(
        provider_id=provider_id,
        egress_manifest=manifest.public_dict(consented=policy.is_consented(manifest)),
    )


@router.delete("/api/providers/{provider_id}/egress-consent", response_model=ProviderEgressConsentResponse)
def revoke_provider_egress_consent(
    provider_id: str,
    container: ApiContainerDep,
) -> ProviderEgressConsentResponse:
    registry = ProviderRegistry(container.root_dir)
    try:
        provider = registry.get(provider_id, fallback=_provider_fallback(container))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="供应商不存在。") from error
    policy = ProviderEgressPolicyStore(container.root_dir)
    manifest = _provider_egress_manifest(provider, policy)
    policy.revoke(provider_id)
    return ProviderEgressConsentResponse(
        provider_id=provider_id,
        egress_manifest=manifest.public_dict(consented=False),
    )


@router.post("/api/providers/{provider_id}/secret", response_model=ProviderSecretStatusResponse)
def save_provider_secret(
    provider_id: str,
    request: SaveProviderSecretRequest,
    container: ApiContainerDep,
) -> ProviderSecretStatusResponse:
    # Desktop production accepts provider credentials exclusively through the
    # authenticated preload capture path. Keeping this JSON endpoint available
    # there would let a renderer bypass the capture boundary.
    from backend.api.desktop_session import desktop_session

    if desktop_session() is not None:
        raise HTTPException(status_code=403, detail="provider_secret_desktop_ipc_required")
    with model_dispatch_authority_fence(container.root_dir):
        api_key = request.api_key.strip()
        store_provider_credential(container, provider_id, api_key)
    return ProviderSecretStatusResponse(has_api_key=True)


@router.delete("/api/providers/{provider_id}/secret", response_model=ProviderSecretStatusResponse)
def delete_provider_secret(provider_id: str, container: ApiContainerDep) -> ProviderSecretStatusResponse:
    with model_dispatch_authority_fence(container.root_dir):
        registry = ProviderRegistry(container.root_dir)
        try:
            provider = registry.get(provider_id, fallback=_provider_fallback(container))
        except KeyError as error:
            raise HTTPException(status_code=404, detail="供应商不存在。") from error
        container.secret_store.delete(f"provider:{provider_id}")
        if provider["is_active"]:
            container.settings_service.delete_openai_api_key()
        _invalidate_provider_health(container, provider_id)
        container.invalidate_agent_graph_service()
    return ProviderSecretStatusResponse(has_api_key=False)


@router.post("/api/providers/{provider_id}/test", response_model=TestProviderSettingsResponse)
def test_provider_secret(
    provider_id: str,
    request: UpdateProviderSettingsRequest,
    container: ApiContainerDep,
) -> TestProviderSettingsResponse:
    registry = ProviderRegistry(container.root_dir)
    try:
        provider = registry.get(provider_id, fallback=_provider_fallback(container))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="供应商不存在。") from error
    policy = ProviderEgressPolicyStore(container.root_dir)
    manifest = _provider_egress_manifest(
        {**provider, "base_url": request.openai_base_url},
        policy,
    )
    # The egress manifest is the sole authority for keyless loopback. Do not
    # even read a stored secret for this path, and never let a request-supplied
    # key turn into a misleading local credential.
    stored_key = ""
    if manifest.external and not request.openai_api_key:
        stored_key = _provider_secret_value(
            container, provider_id=provider_id,
            url=resolve_openai_compatible_api_base_url(request.openai_base_url),
            purpose="connection_test", boundary_revision=manifest.manifest_id,
        )
    resolved_request = request.model_copy(update={
        "openai_api_key": "" if not manifest.external else request.openai_api_key or stored_key,
    })
    try:
        lease = policy.authorize(
            manifest,
            purpose="connection_test",
            payload_categories=("provider_metadata",),
            payload_bytes=_provider_test_payload_bytes(resolved_request),
        )
    except ProviderEgressError as error:
        raise HTTPException(status_code=403, detail=f"模型外发未授权：{error}") from error
    return _execute_provider_settings_test(
        resolved_request,
        container,
        lease,
        provider_id=provider_id,
        anonymous=not manifest.external,
    )


def _provider_fallback(container: ApiContainerDep) -> dict[str, object]:
    if hasattr(container.settings_service, "get_provider_settings"):
        current = container.settings_service.get_provider_settings()
        llm_provider = current.llm_provider
        base_url = current.openai_base_url
        model = current.openai_model
    else:
        llm_provider, base_url, model = "openai", "", ""
    return {
        "name": "默认供应商",
        "llm_provider": llm_provider,
        "base_url": base_url,
        "api_path": "/chat/completions",
        "model": model,
        "models": [model] if model else [],
        "enabled": True,
    }


def _provider_response(item: dict[str, object], container: ApiContainerDep) -> ProviderRecordResponse:
    provider_id = str(item["provider_id"])
    policy = ProviderEgressPolicyStore(container.root_dir)
    manifest = _provider_egress_manifest(item, policy)
    return ProviderRecordResponse(
        **item,
        has_api_key=container.secret_store.has_secret(f"provider:{provider_id}"),
        egress_manifest=manifest.public_dict(consented=policy.is_consented(manifest)),
    )


def _provider_route_references(
    container: ApiContainerDep,
    provider_id: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    route_state = ModelRouteRegistry(container.root_dir).list()
    references = [
        {
            "route_key": str(route.get("route_key") or ""),
            "model_name": str(route.get("model_name") or ""),
            "enabled": route.get("enabled") is True,
        }
        for route in route_state.get("routes", [])
        if str(route.get("provider_id") or "") == provider_id
    ]
    return route_state, references


def _provider_egress_manifest(item: dict[str, object], policy: ProviderEgressPolicyStore):
    base_url = resolve_openai_compatible_api_base_url(str(item.get("base_url") or ""))
    endpoint = base_url if base_url else "http://127.0.0.1/disabled"
    return policy.manifest(
        provider_id=str(item["provider_id"]),
        endpoint=endpoint,
        purposes=DEFAULT_PROVIDER_EGRESS_PURPOSES,
        payload_categories=DEFAULT_PROVIDER_EGRESS_CATEGORIES,
        max_payload_bytes=DEFAULT_PROVIDER_EGRESS_MAX_BYTES,
    )


def _provider_revision(item: dict[str, object]) -> str:
    """Freeze the persisted metadata revision used by Provider Effects."""

    return str(item.get("updated_at") or item.get("created_at") or "current")


def _invalidate_provider_health(container: ApiContainerDep, provider_id: str) -> None:
    try:
        ModelProviderHealthStore(container.root_dir).invalidate_provider(provider_id)
    except (ModelProviderHealthError, OSError, RuntimeError):
        # Health is advisory and must not block a credential authority change.
        return


def _apply_active_provider(item: dict[str, object], container: ApiContainerDep) -> None:
    with model_dispatch_authority_fence(container.root_dir):
        provider_id = str(item["provider_id"])
        if provider_id != "openai":
            container.secret_store.delete("provider:openai")
        container.settings_service.update_provider_settings(
            llm_provider=str(item["llm_provider"]),
            openai_base_url=str(item["base_url"]),
            openai_model=str(item["model"]),
            openai_api_key=None,
            hf_endpoint=None,
        )
        container.invalidate_agent_graph_service()


@router.put("/api/provider-settings", response_model=ProviderSettingsResponse)
def update_provider_settings(
    request: UpdateProviderSettingsRequest,
    container: ApiContainerDep,
) -> ProviderSettingsResponse:
    try:
        with model_dispatch_authority_fence(container.root_dir):
            env_settings = container.settings_service.update_provider_settings(
                llm_provider=request.llm_provider,
                openai_base_url=request.openai_base_url,
                openai_model=request.openai_model,
                openai_api_key=request.openai_api_key,
                hf_endpoint=request.hf_endpoint,
            )
            container.invalidate_agent_graph_service()
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return ProviderSettingsResponse(
        llm_provider=env_settings.llm_provider,
        openai_base_url=env_settings.openai_base_url,
        openai_model=env_settings.openai_model,
        has_openai_api_key=env_settings.has_openai_api_key,
        openai_api_key_masked=env_settings.openai_api_key_masked,
        hf_endpoint=env_settings.hf_endpoint,
    )


@router.post("/api/provider-settings/test", response_model=TestProviderSettingsResponse)
def test_provider_settings(
    request: UpdateProviderSettingsRequest,
    container: ApiContainerDep,
) -> TestProviderSettingsResponse:
    policy = ProviderEgressPolicyStore(container.root_dir)
    item = {
        "provider_id": "openai",
        "base_url": request.openai_base_url,
        "api_path": "/chat/completions",
    }
    manifest = _provider_egress_manifest(item, policy)
    try:
        lease = policy.authorize(
            manifest,
            purpose="connection_test",
            payload_categories=("provider_metadata",),
            payload_bytes=_provider_test_payload_bytes(request),
        )
    except ProviderEgressError as error:
        raise HTTPException(status_code=403, detail=f"模型外发未授权：{error}") from error
    resolved_request = request
    if manifest.external and not request.openai_api_key:
        resolved_request = request.model_copy(update={"openai_api_key": _provider_secret_value(
            container, provider_id="openai",
            url=resolve_openai_compatible_api_base_url(request.openai_base_url),
            purpose="connection_test", boundary_revision=manifest.manifest_id,
        )})
    return _execute_provider_settings_test(
        resolved_request, container, lease, provider_id="openai", anonymous=not manifest.external,
    )


def _execute_provider_settings_test(
    request,
    container: ApiContainerDep,
    lease,
    *,
    provider_id: str,
    anonymous: bool = False,
) -> TestProviderSettingsResponse:
    endpoint = resolve_openai_compatible_api_base_url(request.openai_base_url)
    egress_guard = build_provider_egress_guard(
        container.root_dir,
        provider_id=provider_id,
        endpoint=endpoint,
    )
    try:
        response = container.settings_service.test_provider_settings(
            llm_provider=request.llm_provider,
            openai_base_url=request.openai_base_url,
            openai_model=request.openai_model,
            openai_api_key=request.openai_api_key,
            hf_endpoint=request.hf_endpoint,
            egress_guard=egress_guard,
            anonymous=anonymous,
        )
    except ValueError as error:
        lease.finish("failed", error_code="provider_configuration_invalid")
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        lease.finish("failed", error_code="provider_request_failed")
        raise HTTPException(status_code=503, detail=_provider_test_error_message(error)) from error
    except Exception as error:
        lease.finish("failed", error_code="provider_request_failed")
        raise HTTPException(status_code=503, detail=_provider_test_error_message(error)) from error

    lease.finish("succeeded")
    return TestProviderSettingsResponse(ok=True, message=f"模型连接成功：{response}")


def _provider_test_error_message(error: Exception) -> str:
    text = f"{type(error).__name__}: {error}".lower()
    if any(token in text for token in ("authentication", "unauthorized", "invalid api key", "status code: 401", "status_code=401")):
        return "API Key 无效或已失效。"
    if any(token in text for token in ("model_not_found", "model not found", "unknown model", "status code: 404", "status_code=404")):
        return "模型名称不可用，请选择该供应商支持的模型。"
    if any(token in text for token in ("insufficient", "balance", "quota", "payment required", "status code: 402", "status_code=402")):
        return "模型账户余额或调用额度不足。"
    if any(token in text for token in ("timeout", "timed out", "模型超时")):
        return "模型连接超时，请稍后重试。"
    if any(token in text for token in ("connection", "dns", "certificate", "ssl", "tls", "network")):
        return "无法连接模型服务，请检查网络和接口地址。"
    if "consent_required" in text:
        return "当前模型地址尚未获得连接测试授权。"
    return "模型服务返回异常，请检查供应商状态后重试。"


def _provider_test_payload_bytes(request: UpdateProviderSettingsRequest) -> int:
    payload = {
        "llm_provider": request.llm_provider,
        "openai_base_url": request.openai_base_url,
        "openai_model": request.openai_model,
        "hf_endpoint": request.hf_endpoint,
    }
    return len(str(payload).encode("utf-8"))


@router.get("/api/asr/faster-whisper/models", response_model=list[FasterWhisperModelResponse])
def list_faster_whisper_models(container: ApiContainerDep) -> list[FasterWhisperModelResponse]:
    try:
        settings = load_settings(container.config_path, container.root_dir)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return [
        _to_faster_whisper_model_response(model, container)
        for model in container.faster_whisper_model_manager.list_models(settings.asr.faster_whisper.model_size)
    ]


@router.post("/api/asr/faster-whisper/models/{model_id}/download", response_model=FasterWhisperModelResponse)
def download_faster_whisper_model(model_id: str, container: ApiContainerDep) -> FasterWhisperModelResponse:
    if not container.faster_whisper_model_manager.is_supported(model_id):
        raise HTTPException(status_code=400, detail=f"unsupported faster-whisper model '{model_id}'")

    task_id = _build_model_download_task_id(model_id)
    should_start = False
    with _ASR_DOWNLOAD_LOCK:
        if task_id not in _ACTIVE_ASR_DOWNLOADS:
            _ACTIVE_ASR_DOWNLOADS.add(task_id)
            should_start = True

    if should_start:
        reporter = container.model_download_progress_tracker.create_reporter(task_id)
        Thread(
            target=_run_faster_whisper_model_download,
            args=(model_id, task_id, container, reporter),
            daemon=True,
        ).start()

    settings = load_settings(container.config_path, container.root_dir)
    downloaded_model = next(
        model
        for model in container.faster_whisper_model_manager.list_models(settings.asr.faster_whisper.model_size)
        if model.id == model_id
    )
    return _to_faster_whisper_model_response(downloaded_model, container)


@router.get("/api/asr/faster-whisper/models/{model_id}/download/progress")
async def stream_faster_whisper_model_download_progress(
    model_id: str,
    container: ApiContainerDep,
) -> StreamingResponse:
    task_id = _build_model_download_task_id(model_id)
    return StreamingResponse(
        stream_progress_events(
            tracker=container.model_download_progress_tracker,
            task_id=task_id,
            terminal_statuses={"completed", "failed", "cancelled"},
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.get("/api/rag/models", response_model=list[RagModelResponse])
def list_rag_models(container: ApiContainerDep) -> list[RagModelResponse]:
    return [_to_rag_model_response(model) for model in container.rag_model_manager.list_models()]


@router.post("/api/rag/models/{model_key}/download", response_model=RagModelResponse)
def download_rag_model(model_key: str, container: ApiContainerDep) -> RagModelResponse:
    try:
        status = container.rag_model_manager.start_download(model_key)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return _to_rag_model_response(status)


@router.get("/api/rag/models/{model_key}/download/progress")
async def stream_rag_model_download_progress(
    model_key: str,
    container: ApiContainerDep,
) -> StreamingResponse:
    try:
        task_id = container.rag_model_manager.stream_task_id(model_key)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return StreamingResponse(
        stream_progress_events(
            tracker=container.rag_model_manager.progress_tracker,
            task_id=task_id,
            terminal_statuses={"completed", "failed", "cancelled"},
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


def _build_model_download_task_id(model_id: str) -> str:
    return f"asr-download/{model_id}"


def _run_faster_whisper_model_download(model_id: str, task_id: str, container: ApiContainerDep, reporter) -> None:
    try:
        container.faster_whisper_model_manager.download(model_id, progress_reporter=reporter)
    except Exception as error:
        reporter.failed(str(error))
    finally:
        with _ASR_DOWNLOAD_LOCK:
            _ACTIVE_ASR_DOWNLOADS.discard(task_id)


def _to_faster_whisper_model_response(model, container: ApiContainerDep) -> FasterWhisperModelResponse:
    snapshot = container.model_download_progress_tracker.get_snapshot(_build_model_download_task_id(model.id))
    status = snapshot.status
    if status == "idle" and model.downloaded:
        status = "ready"
    elif status not in {"completed", "failed", "cancelled", "idle"}:
        status = "downloading"
    return FasterWhisperModelResponse(
        id=model.id,
        label=model.label,
        downloaded=model.downloaded,
        current=model.current,
        recommended=model.recommended,
        status=status,
        progress=snapshot.progress,
        detail=snapshot.detail,
        error=snapshot.error,
    )


def _to_rag_model_response(model) -> RagModelResponse:
    return RagModelResponse(
        key=model.key,
        label=model.label,
        repo_id=model.repo_id,
        local_path=model.local_path,
        purpose=model.purpose,
        downloaded=model.downloaded,
        status=model.status,
        progress=model.progress,
        detail=model.detail,
        error=model.error,
    )
    SaveProviderSecretRequest,
