from __future__ import annotations

"""Single source of truth for local ASR defaults.

Leaf module: imports nothing from sibling product_core modules so the
settings -> provider -> audio_asset_transcriber dependency chain stays
acyclic. Every module that needs a local ASR default value must reference
it from here instead of redefining a literal.
"""

LOCAL_ASR_PROVIDER_NAME = "local-command-asr"
DEFAULT_ASR_MODEL_PROFILE = "large-v3-turbo"
DEFAULT_ASR_MODEL_NAME = "large-v3-turbo"
DEFAULT_ASR_TIMEOUT_SECONDS = 7200.0
LOCAL_ASR_MODEL_OPTIONS: tuple[dict[str, object], ...] = (
    {"profile": "small", "model_name": "small", "label": "Small", "recommended": False},
    {"profile": "medium", "model_name": "medium", "label": "Medium", "recommended": False},
    {"profile": "large-v3", "model_name": "large-v3", "label": "Large V3", "recommended": False},
    {"profile": "large-v3-turbo", "model_name": "large-v3-turbo", "label": "Large V3 Turbo", "recommended": True},
)


__all__ = [
    "LOCAL_ASR_PROVIDER_NAME",
    "DEFAULT_ASR_MODEL_PROFILE",
    "DEFAULT_ASR_MODEL_NAME",
    "DEFAULT_ASR_TIMEOUT_SECONDS",
    "LOCAL_ASR_MODEL_OPTIONS",
]
