"""Reuse saved local OCR commands and the optional Linux offline adapter."""
import os
from functools import lru_cache
from pathlib import Path

from core.product_core.local_ocr_provider import LocalCommandImageOcrAdapter
from core.product_core.local_ocr_provider_settings import GetLocalOcrProviderSettings, _runtime_command
from core.product_core.rapidocr_provider import RapidOcrImageAdapter
from .workspace_audio import build_rebuild_object_store


def _settings_store(runtime_root):
    return _cached_store(str(runtime_root))


@lru_cache(maxsize=16)
def _cached_store(root):
    # Only the OCR settings record is read here, and it never routes through the
    # Source Asset authority. Model settings ask for this status on every privacy
    # check; rebuilding the store each time re-read the storage config and the
    # authority database hundreds of times per turn.
    return build_rebuild_object_store(Path(root))[0]


def local_image_provider(runtime_root, references=None):
    store = _settings_store(runtime_root)
    saved = store.read('local_ocr_provider_settings', 'default')
    if saved is None and os.name != 'nt':
        return RapidOcrImageAdapter(references, model_root=runtime_root / 'data' / 'models' / 'rapidocr')
    settings = GetLocalOcrProviderSettings(store).execute()
    return LocalCommandImageOcrAdapter(references, command=_runtime_command(settings.command),
        enabled=settings.enabled, provider_name=settings.provider_name)


def local_image_status(runtime_root):
    adapter = local_image_provider(runtime_root)
    if isinstance(adapter, RapidOcrImageAdapter):
        return {'provider': 'rapidocr', 'status': adapter.status()}
    settings = GetLocalOcrProviderSettings(_settings_store(runtime_root)).execute()
    return {'provider': settings.provider_name, 'status': settings.status}
