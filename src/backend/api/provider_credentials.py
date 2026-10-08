"""Provider credential lifecycle shared by desktop capture and legacy routes."""

from __future__ import annotations

from fastapi import HTTPException

from backend.model_provider_health import ModelProviderHealthError, ModelProviderHealthStore
from backend.providers import ProviderRegistry
from core.product_core.model_dispatch_authority import model_dispatch_authority_fence


def store_provider_credential(container, provider_id: str, value: str) -> int:
    """Validate, store and activate a provider credential as one authority action."""

    normalized = value.strip()
    if not normalized:
        raise HTTPException(status_code=422, detail="API Key 不能为空。")
    with model_dispatch_authority_fence(container.root_dir):
        provider = _provider(container, provider_id)
        secret_ref = f"provider:{provider_id}"
        container.secret_store.set(secret_ref, normalized)
        if provider["is_active"]:
            container.secret_store.set("provider:openai", normalized)
        _invalidate_provider_health(container, provider_id)
        container.invalidate_agent_graph_service()
        return container.secret_store.get_generation(secret_ref)


def _provider(container, provider_id: str) -> dict[str, object]:
    fallback = _provider_fallback(container)
    try:
        return ProviderRegistry(container.root_dir).get(provider_id, fallback=fallback)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="供应商不存在。") from error


def _provider_fallback(container) -> dict[str, object]:
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


def _invalidate_provider_health(container, provider_id: str) -> None:
    try:
        ModelProviderHealthStore(container.root_dir).invalidate_provider(provider_id)
    except (ModelProviderHealthError, OSError, RuntimeError):
        return
