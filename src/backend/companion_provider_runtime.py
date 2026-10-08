from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass

from backend.model_runtime import normalize_litellm_provider, resolve_model_gateway_runtime
from core.model_gateway import ModelGatewayPort, ModelRequest
from core.companion_core import CompanionModelRouter, companion_provider_instruction, parse_companion_chat_envelope


class CompanionLiteLLMProvider:
    def __init__(self, gateway: ModelGatewayPort, model_name: str = "unconfigured") -> None:
        self.gateway = gateway
        self.model_name = model_name

    def generate(self, request: Mapping[str, object]) -> Mapping[str, object]:
        messages = request.get("messages")
        if not isinstance(messages, (list, tuple)):
            raise RuntimeError("companion provider request is invalid")
        if not messages or any(not isinstance(item, Mapping) for item in messages):
            raise RuntimeError("companion provider messages are invalid")
        structured_messages = [dict(item) for item in messages]
        route_key = request.get("route_key") or "companion.chat"
        instruction = companion_provider_instruction(route_key)
        structured_messages.insert(max(0, len(structured_messages) - 1), {"role": "system", "content": instruction})
        if route_key == "companion.vision":
            image = request.get("image_payload")
            if not isinstance(image, Mapping) or image.get("media_type") not in {"image/jpeg", "image/png"} or not isinstance(image.get("bytes"), bytes):
                raise RuntimeError("companion vision image is invalid")
            final = structured_messages[-1]
            text = final.get("content")
            if not isinstance(text, str):
                raise RuntimeError("companion vision prompt is invalid")
            encoded = base64.b64encode(image["bytes"]).decode("ascii")
            structured_messages[-1] = {"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": f"data:{image['media_type']};base64,{encoded}", "detail": "low"}},
            ]}
        result = self.gateway.invoke(ModelRequest(
            capability="vision" if route_key == "companion.vision" else "text",
            input="",
            parameters={
                "messages": structured_messages,
                "temperature": 0.7,
                "max_tokens": max(1, int(request.get("max_output_chars", 4_000)) // 4),
                "timeout": max(1.0, int(request.get("timeout_ms", 60_000)) / 1_000),
            },
            privacy_scope="remote_allowed",
        ))
        raw = result.output
        if not isinstance(raw, str):
            raise RuntimeError("companion model result must be text")
        text, affect = parse_companion_chat_envelope(raw) if route_key == "companion.chat" else (raw.strip(), "neutral")
        return {"text": text, "affect": affect, "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}}


@dataclass(frozen=True, slots=True)
class CompanionModelRuntime:
    router: CompanionModelRouter
    model_name: str


def build_companion_model_runtime(container: object, route_key: str) -> CompanionModelRuntime:
    provider, capabilities, consented, enabled = resolve_companion_provider_runtime(container, route_key)
    return CompanionModelRuntime(
        router=CompanionModelRouter(
            provider=provider,
            provider_capabilities=capabilities,
            egress_consented=consented,
            enabled_routes={route_key: enabled},
        ),
        model_name=provider.model_name if provider is not None else "local-template-v1",
    )


def resolve_companion_provider_runtime(
    container: object,
    route_key: str = "companion.chat",
) -> tuple[CompanionLiteLLMProvider | None, tuple[str, ...], bool, bool]:
    purpose = {
        "companion.diary": "companion_diary",
        "companion.vision": "companion_vision",
        "companion.event": "companion_event",
        "companion.ambient": "companion_ambient",
    }.get(route_key, "companion_chat")
    resolution = resolve_model_gateway_runtime(
        container,
        route_key,
        egress_purpose=purpose,
        egress_categories=("image_frame", "instructions") if route_key == "companion.vision" else ("instructions", "source_excerpt"),
    )
    capabilities = ("vision",) if resolution.adapter_kind == "openai-compatible-vision" else (("text_generation",) if route_key != "companion.vision" else ())
    provider = CompanionLiteLLMProvider(resolution.gateway, resolution.model_name) if resolution.gateway is not None else None
    return provider, capabilities if provider is not None else (), resolution.egress_consented, resolution.enabled


def _litellm_provider_name(value: object) -> str:
    return normalize_litellm_provider(value)
