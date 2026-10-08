from __future__ import annotations

from pathlib import Path
from typing import Mapping, Protocol

from backend.providers import ProviderRegistry
from backend.security import (
    DEFAULT_PROVIDER_EGRESS_CATEGORIES,
    DEFAULT_PROVIDER_EGRESS_MAX_BYTES,
    DEFAULT_PROVIDER_EGRESS_PURPOSES,
    ProviderEgressPolicyStore,
)
from backend.shared.llm.base_url import resolve_openai_compatible_api_base_url
from core.product_core.model_route_migration import ProviderContext


class ModelRouteContainerPort(Protocol):
    root_dir: Path
    settings_service: object


def provider_fallback(container: ModelRouteContainerPort) -> dict[str, object]:
    settings_service = getattr(container, "settings_service", None)
    if hasattr(settings_service, "get_provider_settings"):
        current = settings_service.get_provider_settings()
        return {
            "name": "OpenAI Compatible",
            "llm_provider": current.llm_provider,
            "base_url": current.openai_base_url,
            "api_path": "/chat/completions",
            "model": current.openai_model,
            "models": [current.openai_model] if current.openai_model else [],
            "enabled": True,
        }
    return {
        "name": "OpenAI Compatible",
        "llm_provider": "openai",
        "base_url": "",
        "api_path": "/chat/completions",
        "model": "",
        "models": [],
        "enabled": True,
    }


def provider_egress_manifest(item: Mapping[str, object], policy: ProviderEgressPolicyStore):
    base_url = resolve_openai_compatible_api_base_url(str(item.get("base_url") or ""))
    endpoint = base_url if base_url else "http://127.0.0.1/disabled"
    return policy.manifest(
        provider_id=str(item["provider_id"]),
        endpoint=endpoint,
        purposes=DEFAULT_PROVIDER_EGRESS_PURPOSES,
        payload_categories=DEFAULT_PROVIDER_EGRESS_CATEGORIES,
        max_payload_bytes=DEFAULT_PROVIDER_EGRESS_MAX_BYTES,
    )


def model_route_provider_context(
    container: ModelRouteContainerPort,
    route: Mapping[str, object],
) -> tuple[dict[str, object], bool]:
    provider_id = str(route.get("provider_id") or "")
    provider = ProviderRegistry(container.root_dir).get_readonly(
        provider_id,
        fallback=provider_fallback(container),
    )
    context = model_route_provider_context_from_record(container.root_dir, provider)
    return dict(context.record), context.egress_consented


def model_route_provider_context_from_record(
    root_dir: Path,
    record: Mapping[str, object],
) -> ProviderContext:
    provider = dict(record)
    policy = ProviderEgressPolicyStore(root_dir)
    manifest = provider_egress_manifest(provider, policy)
    return ProviderContext(record=provider, egress_consented=policy.is_consented(manifest))


def list_model_route_provider_contexts(container: ModelRouteContainerPort) -> list[ProviderContext]:
    registry = ProviderRegistry(container.root_dir)
    policy = ProviderEgressPolicyStore(container.root_dir)
    return [
        ProviderContext(record=item, egress_consented=policy.is_consented(provider_egress_manifest(item, policy)))
        for item in registry.list_readonly(fallback=provider_fallback(container))
    ]
