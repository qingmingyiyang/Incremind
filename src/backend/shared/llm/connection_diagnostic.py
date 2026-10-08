"""Leaf-level provider connection diagnostics.

This module deliberately owns only the settings-time transport composition.
It must not depend on the model runtime, Session state, or Agent context.
"""
from __future__ import annotations

from collections.abc import Callable

from backend.shared.llm.base_url import resolve_openai_compatible_api_base_url
from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway


def normalize_litellm_provider(value: object) -> str:
    provider = str(value or "openai").strip().lower()
    return "openai" if provider == "custom_openai" else provider


def run_model_connection_diagnostic(
    *,
    provider: str,
    model_name: str,
    base_url: str,
    api_key_provider: Callable[[], str] | None,
    anonymous: bool,
    reasoning_effort: str | None,
    egress_guard: object,
) -> str:
    """Run an explicit settings diagnostic through the sole transport composer.

    A connection diagnostic is not a model Turn and creates no Session facts.
    Its candidate credential remains call-scoped and the caller owns neither
    the LiteLLM transport nor a reusable gateway.
    """
    gateway = LiteLLMCompletionGateway(
        provider=normalize_litellm_provider(provider),
        model=model_name,
        base_url=resolve_openai_compatible_api_base_url(base_url),
        api_key=None,
        api_key_provider=None if anonymous else api_key_provider,
        anonymous=anonymous,
        reasoning_effort=reasoning_effort,
        egress_guard=egress_guard,
        egress_purpose="connection_test",
    )
    response = gateway.test_connection()
    return response or "ok"
