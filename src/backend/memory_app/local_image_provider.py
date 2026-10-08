"""Reuse saved local OCR commands and the optional Linux offline adapter."""
import os

from core.product_core.local_ocr_provider import LocalCommandImageOcrAdapter
from core.product_core.local_ocr_provider_settings import GetLocalOcrProviderSettings, _runtime_command
from core.product_core.rapidocr_provider import RapidOcrImageAdapter
from .workspace_audio import build_rebuild_object_store


def local_image_provider(runtime_root, references=None):
    store, _ = build_rebuild_object_store(runtime_root)
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
    store, _ = build_rebuild_object_store(runtime_root)
    settings = GetLocalOcrProviderSettings(store).execute()
    return {'provider': settings.provider_name, 'status': settings.status}
