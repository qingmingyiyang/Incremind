"""Shared model weights never own per-user results, credentials or stores."""
from concurrent.futures import ThreadPoolExecutor

import pytest


def test_concurrent_model_acquisition_loads_each_identity_once_and_keeps_results_separate(tmp_path):
    from backend.shared.server_resources import SharedResources, resource_context, shared_model
    loads = []
    class Engine:
        def encode(self, text):
            return [text]
    pool = SharedResources(tmp_path)
    def obtain(name):
        def load():
            loads.append(name)
            return Engine()
        with resource_context(pool):
            return shared_model('embedding', ('model', 'cpu'), load)
    with ThreadPoolExecutor(max_workers=4) as workers:
        engines = list(workers.map(obtain, ('a', 'b', 'c', 'd')))
    assert len(loads) == 1 and all(engine is engines[0] for engine in engines)
    assert engines[0].encode('甲资料') == ['甲资料']
    assert engines[1].encode('乙资料') == ['乙资料']
    with resource_context(pool):
        model = shared_model('recognition', ('model', 'cpu'), Engine)
    assert model is not engines[0]
    assert pool.model_root == tmp_path / 'models'


def test_desktop_does_not_share_or_change_load_and_failed_load_is_not_cached(tmp_path):
    from backend.shared.server_resources import SharedResources, resource_context, shared_model
    loads = []
    def load():
        loads.append('load')
        return object()
    assert shared_model('embedding', ('model',), load) is not shared_model('embedding', ('model',), load)
    assert loads == ['load', 'load']
    pool = SharedResources(tmp_path)
    with resource_context(pool):
        def fail():
            raise RuntimeError('provider_unavailable')
        with pytest.raises(RuntimeError, match='provider_unavailable'):
            shared_model('embedding', ('model',), fail)
        first = shared_model('embedding', ('model',), load)
        assert shared_model('embedding', ('model',), load) is first
    assert len(loads) == 3


def test_actual_fastembed_adapters_share_load_and_serialize_lazy_inference(tmp_path, monkeypatch):
    from backend.shared.server_resources import SharedResources, resource_context
    from backend.video_summary.infrastructure.agent_memory import fastembed_adapter as module
    loads, inputs = [], []
    class Provider:
        def __init__(self, **options):
            loads.append(options)
            self.active = 0
        def embed(self, texts, **options):
            self.active += 1
            assert self.active == 1
            try:
                for text in texts:
                    inputs.append(text)
                    yield [len(text)]
            finally:
                self.active -= 1
        def query_embed(self, query):
            return self.embed([query])
    monkeypatch.setattr(module, '_load_text_embedding_cls', lambda: Provider)
    pool = SharedResources(tmp_path)
    assets = pool.model_root / 'fastembed' / 'bge-small-zh-v1.5'
    assets.mkdir(parents=True)
    for name in ('config.json', 'model_optimized.onnx', 'special_tokens_map.json', 'tokenizer_config.json', 'tokenizer.json'):
        (assets / name).write_text('{}')
    with resource_context(pool):
        a = module.FastEmbedEmbedding(model_name='BAAI/bge-small-zh-v1.5', cache_dir=str(tmp_path / 'users' / 'a'))
        b = module.FastEmbedEmbedding(model_name='BAAI/bge-small-zh-v1.5', cache_dir=str(tmp_path / 'users' / 'b'))
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda value: value[0]._get_query_embedding(value[1]), ((a, '甲资料'), (b, '乙的资料'))))
    assert results == [[3.0], [4.0]] and sorted(inputs) == ['乙的资料', '甲资料']
    assert len(loads) == 1
    assert loads[0]['cache_dir'] == str(pool.model_root / 'fastembed')


def test_actual_whisper_adapters_share_model_and_keep_language_and_input_separate(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from backend.shared.server_resources import SharedResources, resource_context
    from backend.video_summary.infrastructure.faster_whisper_transcriber import FasterWhisperTranscriber
    loads, calls = [], []
    class Provider:
        def __init__(self, *args, **options):
            loads.append((args, options))
        def transcribe(self, path, **options):
            calls.append((path, options))
            segment = SimpleNamespace(start=0, end=1, text=path.rsplit('/', 1)[-1])
            return iter([segment]), SimpleNamespace(language=options['language'], duration=1)
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=Provider))
    pool = SharedResources(tmp_path)
    assets = pool.model_root / 'faster-whisper' / 'tiny'
    assets.mkdir(parents=True)
    (assets / 'model.bin').write_bytes(b'fake-provider-assets')
    with resource_context(pool):
        a = FasterWhisperTranscriber('tiny', 'cpu', 'int8', 'accurate', language='zh')
        b = FasterWhisperTranscriber('tiny', 'cpu', 'int8', 'accurate', language='en')
    one = a.transcribe(tmp_path / 'users/a.wav', tmp_path / 'users/a/output')
    two = b.transcribe(tmp_path / 'users/b.wav', tmp_path / 'users/b/output')
    assert one.language == 'zh' and two.language == 'en'
    assert one.segments[0].text != two.segments[0].text
    assert len(loads) == 1 and len(calls) == 2
    assert loads[0][0] == (str(assets),)


def test_missing_server_weights_do_not_invoke_provider_or_download(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from backend.shared.server_resources import SharedResources, resource_context
    from backend.video_summary.infrastructure.agent_memory import fastembed_adapter
    from backend.video_summary.infrastructure.faster_whisper_transcriber import FasterWhisperTranscriber
    calls = []
    def provider(*args, **kwargs):
        calls.append('provider')
        return object()
    monkeypatch.setattr(fastembed_adapter, '_load_text_embedding_cls', lambda: provider)
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=provider))
    with resource_context(SharedResources(tmp_path)):
        with pytest.raises(ValueError, match='shared_model_not_installed'):
            fastembed_adapter.FastEmbedEmbedding(model_name='BAAI/bge-small-zh-v1.5')
        with pytest.raises(ValueError, match='shared_model_not_installed'):
            FasterWhisperTranscriber('tiny', 'cpu', 'int8', 'accurate')
    assert calls == []
