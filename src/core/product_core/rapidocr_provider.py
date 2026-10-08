"""Optional offline RapidOCR adapter using explicitly installed local assets.

RapidOCR 3.9.0 downloads defaults when model_path or rec_keys_path is absent.
Every asset is therefore checked before constructing its lazy engine. Reference:
https://github.com/RapidAI/RapidOCR/tree/v3.9.0/python/rapidocr (Apache-2.0).
"""
from importlib.util import find_spec
from pathlib import Path

from .local_ocr_provider import LocalCommandImageOcrAdapter, LocalOcrProviderError
from .media_processing_queue import MediaOcrAdapterResult


class RapidOcrImageAdapter:
    def __init__(self, object_store=None, *, model_root):
        self.object_store = object_store
        self.model_root = Path(model_root)
        self._engine = None

    def _assets(self):
        root = self.model_root.resolve()
        files = {key: (root / name).resolve() for key, name in (
            ('Det.model_path', 'det.onnx'), ('Cls.model_path', 'cls.onnx'),
            ('Rec.model_path', 'rec.onnx'), ('Rec.rec_keys_path', 'keys.txt'),
            ('Global.font_path', 'font.ttf'))}
        if any(not path.is_relative_to(root) or not path.is_file() for path in files.values()):
            return None
        return {key: str(path) for key, path in files.items()}

    def status(self):
        try:
            installed = find_spec('rapidocr') is not None and find_spec('onnxruntime') is not None
        except (ImportError, ValueError):
            installed = False
        return 'ready' if installed and self._assets() is not None else 'unavailable'

    def extract_text(self, *, source, job):
        if self.status() != 'ready':
            raise LocalOcrProviderError('rapidocr_unavailable')
        if source.get('type') != 'image' or job.get('required_capability') != 'ocr':
            raise LocalOcrProviderError('rapidocr_requires_authorized_image')
        path, authorization = LocalCommandImageOcrAdapter(
            self.object_store, command=(), enabled=True)._authorized_image_path(source)
        try:
            if self._engine is None:
                from rapidocr import RapidOCR
                self._engine = RapidOCR(params={**self._assets(),
                    'EngineConfig.onnxruntime.use_cuda': False,
                    'Global.log_level': 'critical'})
            result = self._engine(str(path))
            lines = getattr(result, 'txts', None)
            if not isinstance(lines, (list, tuple)) or any(not isinstance(line, str) for line in lines):
                raise ValueError('invalid_ocr_result')
            text = '\n'.join(lines).strip()
            if not text:
                raise ValueError('empty_ocr_result')
        except Exception:
            raise LocalOcrProviderError('rapidocr_processing_failed') from None
        return MediaOcrAdapterResult(text=text, provider='rapidocr', confidence=None,
            metadata={'local_processing': True, 'remote_processing': False,
                'image_reference': authorization['image_reference'],
                'authorization_id': authorization['id'], 'path_stored_in_output': False})
