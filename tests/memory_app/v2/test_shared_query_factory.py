import os
from pathlib import Path
from uuid import uuid4

import pytest


def test_real_retrieve_preserves_cache_positions_after_normalization():
    base = Path(__file__).resolve().parents[3] / 'work' / ('g09-cache-' + uuid4().hex[:8])
    base.mkdir(parents=True)
    base = base.resolve(strict=True)
    previous_root = os.environ.get('CHRIPTMAS_APP_ROOT')
    os.environ['CHRIPTMAS_APP_ROOT'] = str(base)
    try:
        _check_cache_positions(base)
    finally:
        if previous_root is None:
            os.environ.pop('CHRIPTMAS_APP_ROOT', None)
        else:
            os.environ['CHRIPTMAS_APP_ROOT'] = previous_root


def _check_cache_positions(base):
    from backend.recognition_retrieval import SQLiteEmbeddingCache, HttpEmbeddingProvider, retrieve
    from backend.recognition_retrieval.service import _entry
    from backend.memory_app.retrieval_models import ConfiguredTransport
    from backend.memory_app.v2.shared_rendered_preparation import RenderedEmbeddingBatch

    projections = [{'id': identity, 'revision': 1, 'project_id': 'cache-project', 'content': '  同文片段\n\n'}
        for identity in ('entry-a', 'entry-b')]
    provider = HttpEmbeddingProvider(ConfiguredTransport('not-sent'),
        'http://127.0.0.1:9/v1/embeddings', 'cache-io-only')
    batch, seen = RenderedEmbeddingBatch(projections), []

    class PreparedBatchObserved(BaseException):
        pass

    cache = SQLiteEmbeddingCache(str(base / 'normalization.sqlite3'))
    try:
        # 手动向量只验证缓存命中和候选位置，检索在调用 embed 之前明确终止。
        cache.write(model_id=provider.cache_identity, entry=_entry(projections[0]), vector=(1.0,))

        class CacheObserver:
            def read(self, *, model_id, entry):
                value = cache.read(model_id=model_id, entry=entry)
                batch.observe(entry, value)
                seen.append(entry)
                if len(seen) == len(projections):
                    raise PreparedBatchObserved()
                return value

            def write(self, *, model_id, entry, vector):
                cache.write(model_id=model_id, entry=entry, vector=vector)

        with pytest.raises(PreparedBatchObserved):
            retrieve('cache-project', ' \n提问正文 \t', projections, limit=2, vector_candidate_limit=2,
                keyword_enabled=False, embedding_provider=provider, embedding_cache=CacheObserver())
        assert [entry.content for entry in seen] == ['同文片段', '同文片段']
        assert batch.missing_ids == ('entry-b',)
        assert all(projection['content'] == '  同文片段\n\n' for projection in projections)
    finally:
        cache.close()
