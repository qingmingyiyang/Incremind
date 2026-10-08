"""Immutable, protocol-scoped capabilities for the existing chat gateway.

Reference: pi-ai's per-model compatibility declarations and DeepSeek's
https://api-docs.deepseek.com/api/create-chat-completion/ (checked 2026-10-01).
Unknown endpoints retain the existing bounded format probing; a proxy's model
name alone is not evidence of its wire protocol.
"""
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from .model_prices import ModelPrices, official_prices


@dataclass(frozen=True)
class ModelCapabilities:
    structured_modes: tuple[str, ...] = ("schema", "json_object", "prompt")
    developer_role: bool = True
    stream_usage: bool = True
    max_tokens_field: str = "max_tokens"
    reasoning_efforts: tuple[str, ...] = ("low", "medium", "high")
    prices: ModelPrices | None = None
    background_resume: bool = field(default=False, repr=False)

    def __post_init__(self):
        if (not self.structured_modes or "prompt" not in self.structured_modes
                or any(mode not in {"schema", "json_object", "prompt"} for mode in self.structured_modes)
                or len(set(self.structured_modes)) != len(self.structured_modes)
                or self.max_tokens_field not in {"max_tokens", "max_completion_tokens"}
                or type(self.developer_role) is not bool or type(self.stream_usage) is not bool
                or type(self.background_resume) is not bool
                or self.prices is not None and not isinstance(self.prices, ModelPrices)
                or any(level not in {"minimal", "low", "medium", "high", "xhigh", "max"}
                       for level in self.reasoning_efforts)):
            raise ValueError("invalid model capability declaration")


def resolve_model_capabilities(provider: str, model: str, base_url: str) -> ModelCapabilities:
    # Provider aliases and a model name do not establish proxy capabilities.
    if provider.strip().lower() in {"openai", "deepseek"} and urlsplit(base_url).hostname == "api.deepseek.com":
        return ModelCapabilities(structured_modes=("json_object", "prompt"),
            developer_role=False, reasoning_efforts=("minimal", "low", "medium", "high", "xhigh", "max"),
            prices=official_prices(provider, model, base_url))
    return ModelCapabilities()
