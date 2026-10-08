"""Providers ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.model_route_context import (
    list_model_route_provider_contexts,
    model_route_provider_context_from_record,
)
from backend.providers import ProviderRegistry
from backend.security import (
    DEFAULT_PROVIDER_EGRESS_CATEGORIES,
    DEFAULT_PROVIDER_EGRESS_MAX_BYTES,
    DEFAULT_PROVIDER_EGRESS_PURPOSES,
    ProviderEgressPolicyStore,
)
from backend.shared.llm.base_url import (
    normalize_provider_base_url,
    resolve_openai_compatible_api_base_url,
)

from core.product_core.local_memory_vault import GetProviderBoundary, serialize_provider_boundary
from core.product_core.model_route_migration import ProviderContext

from . import http as product_http
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


DEFAULT_DEEPSEEK_ENDPOINT_URL = "https://api.deepseek.com/chat/completions"


DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"


@router.get("/api/rebuild/provider-boundary")
def provider_boundary(request: Request, container: ApiContainerDep) -> JSONResponse:
    """阶段 6：Provider 边界总览。

    返回本地 Provider、外部 Provider、哪些内容从未离开本机、
    哪些内容可能离开本机、未配置能力列表。
    """
    store, settings = product_repositories._object_store(container.root_dir)
    use_case = GetProviderBoundary(store)
    try:
        result = use_case.execute()
        body = serialize_provider_boundary(result)
        return product_http._json_response(200, body, product_http._no_store_headers())
    except Exception as error:  # noqa: BLE001 - 边界用例不应抛出
        return product_http._json_response(400, {"detail": str(error)}, product_http._no_store_headers())


def _resolve_provider_for_rebuild_role_record(container: ApiContainerDep, provider_id: str) -> Mapping[str, object]:
    try:
        return ProviderRegistry(container.root_dir).get_readonly(
            provider_id, fallback=_provider_registry_fallback(container),
        )
    except KeyError:
        return _resolve_deepseek_provider_record(container)


def _model_route_runtime_contexts(
    container: ApiContainerDep,
) -> tuple[dict[str, ProviderContext], list[ProviderContext]]:
    providers = list_model_route_provider_contexts(container)
    record = _resolve_provider_for_rebuild_role_record(container, "intake-main-model")
    compatibility = model_route_provider_context_from_record(container.root_dir, record)
    return {
        "intake.classification": compatibility,
        "memory.project_routing": compatibility,
        "search.answer": compatibility,
    }, providers


def _provider_egress_guard(container: ApiContainerDep, record: Mapping[str, object]):
    policy = ProviderEgressPolicyStore(container.root_dir)
    base_url = resolve_openai_compatible_api_base_url(str(record.get("base_url") or ""))
    manifest = policy.manifest(
        provider_id=str(record["provider_id"]),
        endpoint=base_url or "http://127.0.0.1/disabled",
        purposes=DEFAULT_PROVIDER_EGRESS_PURPOSES,
        payload_categories=DEFAULT_PROVIDER_EGRESS_CATEGORIES,
        max_payload_bytes=DEFAULT_PROVIDER_EGRESS_MAX_BYTES,
    )

    def guard(purpose: str, categories: tuple[str, ...], payload_bytes: int):
        lease = policy.authorize(
            manifest,
            purpose=purpose,
            payload_categories=categories,
            payload_bytes=payload_bytes,
        )
        return lambda status, error_code=None: lease.finish(status, error_code=error_code)

    return guard


def _deepseek_provider_status(container: ApiContainerDep) -> Mapping[str, Any]:
    record = _resolve_deepseek_provider_record(container)
    secret_name = f"provider:{record['provider_id']}"
    selected_secret_name = _deepseek_api_key_secret_name(container, record)
    credential_source = (
        "secret"
        if selected_secret_name == secret_name
        else "deepseek_secret"
        if selected_secret_name == "provider:deepseek"
        else "missing"
    )
    has_key = credential_source != "missing"
    return {
        "status": "ready" if has_key else "missing_key",
        "provider_id": record["provider_id"],
        "provider_name": record["name"],
        "llm_provider": record["llm_provider"],
        "model": record["model"] or DEFAULT_DEEPSEEK_MODEL,
        "endpoint_url": _provider_endpoint_url(record),
        "has_api_key": has_key,
        "credential_source": credential_source,
        "secret_name": secret_name,
        "selected_secret_name": selected_secret_name,
        "fallback_secret_name": "provider:deepseek",
        "env_var": None,
        "key_material_returned": False,
        "request_payload_persisted": False,
        "next_step": (
            "可以从已完成 Source output 生成四层候选。"
            if has_key
            else f"在设置页保存 {secret_name} API Key。"
        ),
    }


def _deepseek_api_key_secret_name(container: ApiContainerDep, record: Mapping[str, object]) -> str | None:
    secret_name = f"provider:{record['provider_id']}"
    secret_store = getattr(container, "secret_store", None)
    has_secret = getattr(secret_store, "has_secret", None)
    if callable(has_secret) and has_secret(secret_name):
        return secret_name
    if secret_name != "provider:deepseek" and callable(has_secret) and has_secret("provider:deepseek"):
        return "provider:deepseek"
    return None


def _resolve_deepseek_provider_record(container: ApiContainerDep) -> Mapping[str, object]:
    registry = ProviderRegistry(container.root_dir)
    providers = registry.list_readonly(fallback=_provider_registry_fallback(container))
    enabled = [item for item in providers if item.get("enabled") is True]
    for item in enabled:
        if item.get("provider_id") == "deepseek":
            return item
    for item in enabled:
        if item.get("is_active") is True and str(item.get("llm_provider", "")).lower() == "deepseek":
            return item
    for item in enabled:
        if str(item.get("llm_provider", "")).lower() == "deepseek":
            return item
    return {
        "provider_id": "deepseek",
        "name": "DeepSeek",
        "llm_provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "api_path": "/chat/completions",
        "model": DEFAULT_DEEPSEEK_MODEL,
        "enabled": True,
        "is_active": False,
    }


def _provider_registry_fallback(container: ApiContainerDep) -> dict[str, object]:
    settings_service = getattr(container, "settings_service", None)
    if settings_service is not None and hasattr(settings_service, "get_provider_settings"):
        current = settings_service.get_provider_settings()
        return {
            "name": "默认供应商",
            "llm_provider": current.llm_provider,
            "base_url": current.openai_base_url,
            "api_path": "/chat/completions",
            "model": current.openai_model,
            "models": [current.openai_model] if current.openai_model else [],
            "enabled": True,
        }
    return {
        "name": "默认供应商",
        "llm_provider": "openai",
        "base_url": "",
        "api_path": "/chat/completions",
        "model": "",
        "models": [],
        "enabled": True,
    }


def _provider_endpoint_url(record: Mapping[str, object]) -> str:
    base_url = normalize_provider_base_url(str(record.get("base_url") or "").strip())
    api_path = "/" + str(record.get("api_path") or "chat/completions").strip().strip("/")
    if not base_url:
        return DEFAULT_DEEPSEEK_ENDPOINT_URL
    return f"{base_url.rstrip('/')}{api_path}"
