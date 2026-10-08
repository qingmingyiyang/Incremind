from __future__ import annotations

from pathlib import Path

from backend import companion_runtime, companion_voice_asr_runtime
from backend.companion_runtime_layout import build_companion_object_store
from core.product_core.local_asr_provider_settings import SaveLocalAsrProviderSettings


def test_settings_loader_reads_persisted_settings_and_reloads_each_call(tmp_path: Path) -> None:
    store = build_companion_object_store(tmp_path)
    SaveLocalAsrProviderSettings(store).execute(enabled=False, command=[])
    loader = companion_voice_asr_runtime.build_voice_asr_settings_loader(tmp_path)

    assert loader().enabled is False

    SaveLocalAsrProviderSettings(store).execute(
        enabled=True, command=("faster-whisper",), confirm_enable=True,
    )
    assert loader().enabled is True


def test_settings_loader_falls_back_to_shared_asr_defaults(tmp_path: Path) -> None:
    loader = companion_voice_asr_runtime.build_voice_asr_settings_loader(tmp_path)

    settings = loader()

    assert settings.enabled is False
    assert settings.status == "disabled"
    assert settings.model_profile == "large-v3-turbo"
    assert settings.timeout_seconds == 7200.0


def test_voice_builder_uses_the_external_asr_runtime_adapter() -> None:
    assert companion_runtime.build_voice_asr_settings_loader.__module__ == "backend.companion_voice_asr_runtime"
