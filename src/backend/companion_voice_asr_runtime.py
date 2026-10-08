from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from backend.companion_runtime_layout import build_companion_object_store
from core.product_core.local_asr_provider_settings import GetLocalAsrProviderSettings, LocalAsrProviderSettings


def build_voice_asr_settings_loader(root_dir: Path) -> Callable[[], LocalAsrProviderSettings]:
    """Wire the voice ASR settings loader against the companion vault.

    The returned callable re-reads the persisted settings on every call so
    runtime enable/disable changes are observed without rebuilding the
    service; enable/status validation stays in the domain service.
    """
    settings = GetLocalAsrProviderSettings(build_companion_object_store(root_dir))
    return settings.execute


__all__ = ["build_voice_asr_settings_loader"]
