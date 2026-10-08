from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from backend.companion_runtime_layout import build_companion_object_store
from core.companion_core import CHARACTER_PROMPT_ID, default_character_prompt
from core.product_core.developer_studio_config import GetDeveloperStudioConfig
from core.product_core.prompt_activation import resolve_active_prompt


def load_active_character_prompt(runtime_root: Path) -> tuple[str, int]:
    try:
        config = GetDeveloperStudioConfig(build_companion_object_store(runtime_root)).execute()
        prompt = resolve_active_prompt(config, CHARACTER_PROMPT_ID)
        if isinstance(prompt, Mapping):
            content = prompt.get("content")
            activation = getattr(config, "prompt_activation", {})
            revision = int(activation.get("revision", 0)) if isinstance(activation, Mapping) else 0
            if isinstance(content, str) and content.strip():
                return content.strip(), max(1, revision)
    except Exception:
        pass
    return default_character_prompt()


__all__ = ["load_active_character_prompt"]
