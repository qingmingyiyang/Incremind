from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace

from core.product_core import local_asr_defaults
from core.product_core.audio_asset_transcriber import (
    AUDIO_ASSET_ASR_MODEL_OPTIONS,
    DEFAULT_AUDIO_ASSET_ASR_MODEL_NAME,
    DEFAULT_AUDIO_ASSET_ASR_MODEL_PROFILE,
    GetAudioAssetTranscriberSettings,
)
from core.product_core.local_asr_provider import LocalCommandAudioTranscriptionAdapter
from core.product_core.local_asr_provider_settings import (
    DEFAULT_ASR_MODEL_NAME,
    DEFAULT_ASR_MODEL_PROFILE,
    GetLocalAsrProviderSettings,
    LOCAL_ASR_MODEL_OPTIONS,
)


def _empty_store() -> SimpleNamespace:
    return SimpleNamespace(read=lambda _collection, _record_id: None)


def test_defaults_module_is_the_single_source_for_scattered_aliases() -> None:
    assert DEFAULT_ASR_MODEL_PROFILE is local_asr_defaults.DEFAULT_ASR_MODEL_PROFILE
    assert DEFAULT_ASR_MODEL_NAME is local_asr_defaults.DEFAULT_ASR_MODEL_NAME
    assert LOCAL_ASR_MODEL_OPTIONS is local_asr_defaults.LOCAL_ASR_MODEL_OPTIONS
    assert DEFAULT_AUDIO_ASSET_ASR_MODEL_PROFILE is local_asr_defaults.DEFAULT_ASR_MODEL_PROFILE
    assert DEFAULT_AUDIO_ASSET_ASR_MODEL_NAME is local_asr_defaults.DEFAULT_ASR_MODEL_NAME
    assert AUDIO_ASSET_ASR_MODEL_OPTIONS is local_asr_defaults.LOCAL_ASR_MODEL_OPTIONS


def test_defaults_module_defines_one_shared_model_catalog() -> None:
    profiles = [option["profile"] for option in local_asr_defaults.LOCAL_ASR_MODEL_OPTIONS]
    assert profiles == ["small", "medium", "large-v3", "large-v3-turbo"]
    assert local_asr_defaults.DEFAULT_ASR_MODEL_PROFILE == "large-v3-turbo"
    assert local_asr_defaults.DEFAULT_ASR_MODEL_NAME == "large-v3-turbo"
    assert local_asr_defaults.DEFAULT_ASR_TIMEOUT_SECONDS == 7200.0


def test_adapter_defaults_align_with_settings_defaults() -> None:
    adapter_defaults = {
        field.name: field.default
        for field in fields(LocalCommandAudioTranscriptionAdapter)
        if field.default is not field.default_factory  # type: ignore[comparison-overlap]
    }
    assert adapter_defaults["provider_name"] == local_asr_defaults.LOCAL_ASR_PROVIDER_NAME
    assert adapter_defaults["timeout_seconds"] == local_asr_defaults.DEFAULT_ASR_TIMEOUT_SECONDS
    assert adapter_defaults["model_profile"] == local_asr_defaults.DEFAULT_ASR_MODEL_PROFILE
    assert adapter_defaults["model_name"] == local_asr_defaults.DEFAULT_ASR_MODEL_NAME


def test_settings_and_audio_asset_defaults_share_one_timeout() -> None:
    settings = GetLocalAsrProviderSettings(_empty_store()).execute()
    audio_asset = GetAudioAssetTranscriberSettings(_empty_store()).execute()

    assert settings.timeout_seconds == local_asr_defaults.DEFAULT_ASR_TIMEOUT_SECONDS
    assert audio_asset.timeout_seconds == local_asr_defaults.DEFAULT_ASR_TIMEOUT_SECONDS
    assert settings.model_options == audio_asset.model_options
