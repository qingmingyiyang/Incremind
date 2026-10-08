"""只替换编码器边界，真实专用线程、优先队列和批次保持执行。"""
from backend.memory_app.v2.embedding_settings import vector_policy
from threading import Event

import pytest

from backend.memory_app.local_vectors import LocalVectorError, VectorWorker


def test_worker_loads_once_and_query_runs_before_queued_documents(tmp_path):
    entered, release = Event(), Event()
    calls, loads = [], []

    class Encoder:
        def encode(self, texts, input_type, *, policy):
            calls.append((tuple(texts), input_type))
            if texts[0] == '占用':
                entered.set()
                assert release.wait(5)
            return [[1.0] + [0.0] * 255 for _ in texts], len(texts)

    def load(directory):
        loads.append(directory)
        return Encoder()

    worker = VectorWorker(tmp_path, loader=load, policy=vector_policy())
    assert loads == [] and worker.thread is None
    try:
        first = worker.submit(['占用'])
        assert entered.wait(5)
        documents = worker.submit(['资料'])
        query = worker.submit(['查询'], 'query')
        release.set()
        assert first.result(5)['data'][0]['index'] == 0
        assert query.result(5)['usage']['prompt_tokens'] == 1
        assert documents.result(5)['data'][0]['embedding'] == [1.0] + [0.0] * 255
        assert calls == [(('占用',), 'document'), (('查询',), 'query'), (('资料',), 'document')]
        assert len(loads) == 1 and worker.pending == 0
    finally:
        release.set()
        worker.close()
    assert not worker.thread.is_alive()


def test_worker_queue_is_bounded_and_failed_encode_releases_capacity(tmp_path):
    entered, release = Event(), Event()

    class Encoder:
        def encode(self, texts, input_type, *, policy):
            entered.set()
            assert release.wait(5)
            raise ValueError('synthetic_encoder_failure')

    worker = VectorWorker(tmp_path, loader=lambda directory: Encoder(), queue_limit=1, policy=vector_policy())
    try:
        first = worker.submit(['占用'])
        assert entered.wait(5)
        with pytest.raises(LocalVectorError, match='local_vector_queue_full'):
            worker.submit(['第二条'])
        release.set()
        with pytest.raises(ValueError, match='synthetic_encoder_failure'):
            first.result(5)
        second = worker.submit(['释放后'])
        with pytest.raises(ValueError, match='synthetic_encoder_failure'):
            second.result(5)
    finally:
        release.set()
        worker.close()


def test_worker_yields_between_document_batches_for_query(tmp_path):
    entered, release = Event(), Event()
    calls = []

    class Encoder:
        def encode(self, texts, input_type, *, policy):
            calls.append((tuple(texts), input_type))
            if len(calls) == 1:
                entered.set()
                assert release.wait(5)
            return [[1.0] + [0.0] * 255 for _ in texts], len(texts)

    worker = VectorWorker(tmp_path, loader=lambda directory: Encoder(), policy=vector_policy())
    try:
        texts = [str(index) for index in range(vector_policy()['batch_size'] + 1)]
        documents = worker.submit(texts)
        assert entered.wait(5)
        query = worker.submit(['查询'], 'query')
        release.set()
        assert len(documents.result(5)['data']) == len(texts)
        assert query.result(5)['data'][0]['index'] == 0
        assert [kind for _, kind in calls] == ['document', 'query', 'document']
        assert calls[-1][0] == (texts[-1],)
    finally:
        release.set()
        worker.close()
