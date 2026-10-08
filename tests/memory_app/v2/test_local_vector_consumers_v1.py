"""真实领域来源、缓存与内核回执；只替换可选依赖探针和编码器边界。"""
from backend.memory_app.v2.embedding_settings import vector_policy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Event

import pytest

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.model_costs import attempt_cost
from backend.memory_app import local_vectors
from backend.memory_app.local_vector_assets import FILES
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.contextual_chunk_vectors import chunk_vector_scores
from backend.memory_app.v2.embedding_index import EmbeddingIndex
from backend.memory_app.v2.embedding_settings import EmbeddingSettings
from backend.memory_app.v2.layers import summary_of
from backend.memory_app.v2.memory_turn import MemoryTurn
from backend.memory_app.v2.privacy import set_private_project
from backend.security.secrets import InMemorySecretStore
from backend.recognition import WorkScope, RecognitionConflict
from backend.recognition_retrieval import RecognitionRetrievalError
from core.search_and_recall.evidence_windows import split_evidence_chunks
from tests.memory_app.v2.test_workbench_ask import env as env, add_document
from tests.memory_app.v2.kernel_receipts import wire_receipts, requests


def marker_assets(root):
    directory = local_vectors.model_directory(root / 'data' / 'models')
    for name in (*FILES, 'model.safetensors'):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'encoder boundary fixture')
    manifest = {'model': 'google/embeddinggemma-2', 'derivation': 'text-only@1', 'tensor_count': 413,
        'files': {name: {'size': (directory / name).stat().st_size} for name in (*FILES, 'model.safetensors')}}
    (directory / 'embedding-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return directory


@pytest.fixture
def local_query(env, monkeypatch):
    directory = marker_assets(env.root)
    calls = []
    class Encoder:
        def __init__(self, directory):
            self.directory = directory
        def encode(self, texts, input_type, *, policy):
            calls.append((tuple(texts), input_type))
            return [[1.0] + [0.0] * 255 for _ in texts], 5 * len(texts)
    monkeypatch.setattr(local_vectors, 'SentenceEncoder', Encoder)
    monkeypatch.setattr(local_vectors, 'dependencies_available', lambda: True)
    monkeypatch.setattr('backend.memory_app.v2.embedding_settings.dependencies_available', lambda: True)
    models = ModelConfiguration(env.records, env.root, InMemorySecretStore())
    owner = EmbeddingSettings(env.records, models._local_models_root)
    models.bind_embedding(owner.project, vector_policy)
    query = env.domains.query
    query.models = models
    network_calls = []
    def disconnected_network(*args, **kwargs):
        network_calls.append((args, kwargs))
        raise AssertionError('local vectors reached external HTTP')
    # 仅断开外部 HTTP 边界，真实本机 transport、队列及内核照常执行。
    monkeypatch.setattr('backend.memory_app.retrieval_models.httpx.Client', disconnected_network)
    query._v1_network_calls = network_calls
    try:
        yield query, calls
    finally:
        local_vectors.worker_for(directory, policy=vector_policy()).close()
        models.close()


def private_scores(env, query, *, identity=None):
    if identity is None:
        identity, _ = add_document(env, summary='合成摘要', body='合成事实。' * 180)
        set_private_project(env.records, 'alpha', True, expected_revision=0)
    entry = next(item for item in query.query_entries('alpha') if item['id'] == identity)
    scope = WorkScope('local-user', 'alpha')
    snapshot = query.original_snapshot(scope, entry, SourceEgressService(env.records))
    markdown = env.documents.markdown(identity)
    projection = query.retrieval_index.indexed_entries('alpha')[2][identity]
    summary = projection['summary']
    slot, span = next((slot, span) for slot, span in enumerate(projection['spans'])
        if span['layer'] == 'L1' and span['start'] == summary_of(markdown)[2])
    start = span['start']
    chunks = split_evidence_chunks(markdown[start:span['end']])
    scores = chunk_vector_scores(query, 'alpha', entry, scope, snapshot, chunks,
        layer='L1', span_start=start, span_slot=slot, summary=summary, question='合成事实是什么？')
    return scores, chunks


def test_private_local_vectors_have_real_kernel_receipts_and_zero_cost(env, local_query):
    query, calls = local_query
    identity, _ = add_document(env, summary='合成摘要', body='合成事实。' * 180)
    set_private_project(env.records, 'alpha', True, expected_revision=0)
    assert EmbeddingIndex(query).run() > 0
    assert all(kind == 'document' for _, kind in calls)
    background_calls = len(calls)
    scores, chunks = private_scores(env, query, identity=identity)
    assert scores == {index: pytest.approx(1.0) for index in range(len(chunks))}
    assert [kind for _, kind in calls[background_calls:]] == ['query']
    receipts = wire_receipts(env.records)
    assert len(receipts) == 2 and all(row['status'] == 'succeeded' for row in receipts)
    assert all(row['provider_id'] == 'local' for row in receipts)
    frozen = requests(env.records)
    assert len(frozen) == 2 and all(row['privacy']['mode'] == 'local_only' for row in frozen)
    assert len(env.records.list('v2_model_wire_prices')) == 2
    assert all(attempt_cost(env.records, row) == {'currency': 'CNY', 'amount': '0'} for row in receipts)
    assert all(row['attempt_number'] == 1 for row in receipts) and query._v1_network_calls == []


def test_background_index_reuses_real_consumer_cache_and_preserves_facts(env, local_query):
    query, calls = local_query
    identity, _ = add_document(env, summary='合成摘要', body='合成事实。' * 180)
    set_private_project(env.records, 'alpha', True, expected_revision=0)
    before = {collection: env.records.list(collection) for collection in (
        'documents', 'workspace_items', 'recognitions', 'recognition_experiences')}
    index = EmbeddingIndex(query)
    count = index.run()
    assert count > 0 and all(kind == 'document' for _, kind in calls)
    progress = env.records.read('v2_embedding_index', 'default').payload
    assert progress['progress'] == {'done': count, 'total': count}
    assert progress['reason_code'] is None
    previous_calls = len(calls)
    assert index.run() == 0 and len(calls) == previous_calls
    scores, chunks = private_scores(env, query, identity=identity)
    assert scores == {slot: pytest.approx(1.0) for slot in range(len(chunks))}
    assert [kind for _, kind in calls[previous_calls:]] == ['query']
    assert {collection: env.records.list(collection) for collection in before} == before
    assert query._v1_network_calls == []


@pytest.mark.parametrize('change', ['mode', 'privacy', 'configuration'])
def test_local_consumer_rejects_flight_authority_changes_without_success(env, local_query, monkeypatch, change):
    query, calls = local_query
    if change == 'configuration':
        EmbeddingSettings(env.records, query.models._local_models_root).update_mode(
            mode='local', expected_revision=0)
    identity, _ = add_document(env, summary='合成摘要', body='合成事实。' * 180)
    set_private_project(env.records, 'alpha', True, expected_revision=0)
    assert EmbeddingIndex(query).run() > 0
    assert all(kind == 'document' for _, kind in calls)
    background_calls = len(calls)
    original = local_vectors.SentenceEncoder.encode
    changed = False
    def encode(encoder, texts, input_type, *, policy):
        nonlocal changed
        result = original(encoder, texts, input_type, policy=policy)
        if not changed:
            changed = True
            if change == 'mode':
                EmbeddingSettings(env.records, query.models._local_models_root).update_mode(
                    mode='remote', expected_revision=0)
            elif change == 'privacy':
                row = env.records.read('v2_private_scopes', 'alpha')
                set_private_project(env.records, 'alpha', False, expected_revision=row.revision)
            else:
                query.models.update('embedding', {'base_url': 'https://synthetic.invalid/v1',
                    'model': 'synthetic-remote', 'api_key': 'test-synthetic-vector',
                    'allow_remote': True, 'enabled': True, 'expected_revision': 0})
        return result
    # 只改变编码边界的时序，权限、配置、缓存和内核仍为真实实现。
    monkeypatch.setattr(local_vectors.SentenceEncoder, 'encode', encode)
    scores, _ = private_scores(env, query, identity=identity)
    assert scores == {} and len(calls[background_calls:]) == 1 and calls[background_calls][1] == 'query'
    receipts = wire_receipts(env.records)
    assert len(receipts) == 2 and sorted(row['status'] for row in receipts) == ['failed_transport', 'succeeded']
    assert all(row['attempt_number'] == 1 for row in receipts)
    assert all(row['provider_id'] == 'local' for row in receipts)
    keys = [row.payload['identity']['key'] for row in env.records.list('v2_memory_turn_keys')
            if row.payload['identity']['key'].get('namespace') == 'contextual-chunks-v1']
    assert len(keys) == 1
    key = keys[0]
    assert key['configuration_revision'] == 0
    assert key['mode_revision'] == (1 if change == 'configuration' else 0)
    assert len(requests(env.records)) == 2 and all(row['privacy']['mode'] == 'local_only'
                                                for row in requests(env.records))
    assert query._v1_network_calls == []


def test_interrupted_index_preserves_first_batch_and_resumes_only_missing_cache(env, local_query, monkeypatch):
    query, calls = local_query
    add_document(env, summary='合成摘要', body='合成事实。' * 3000)
    set_private_project(env.records, 'alpha', True, expected_revision=0)
    collections = ('documents', 'workspace_items', 'recognitions', 'recognition_experiences')
    facts = {name: env.records.list(name) for name in collections}
    original = local_vectors.SentenceEncoder.encode
    document_batches = 0
    first_run_revisions = []
    def interrupted(encoder, texts, input_type, *, policy):
        nonlocal document_batches
        if input_type == 'document':
            if not first_run_revisions:
                first_run_revisions.append(env.records.read('v2_embedding_index', 'default').revision)
            document_batches += 1
            if document_batches == 2:
                raise local_vectors.LocalVectorError('synthetic_encoder_interruption')
        return original(encoder, texts, input_type, policy=policy)
    monkeypatch.setattr(local_vectors.SentenceEncoder, 'encode', interrupted)
    with pytest.raises(local_vectors.LocalVectorError, match='synthetic_encoder_interruption'):
        EmbeddingIndex(query).run()
    failed = env.records.read('v2_embedding_index', 'default').payload
    assert failed['reason_code'] == 'embedding_index_interrupted'
    assert failed['progress']['done'] == 8 and failed['progress']['total'] > 8
    path = env.records.database_path.parent / 'recognition-vectors.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        first_batch = connection.execute('SELECT * FROM recognition_embedding_cache ORDER BY recognition_id').fetchall()
    assert len(first_batch) == 8
    receipts = wire_receipts(env.records)
    assert len(receipts) == 2 and sorted(row['status'] for row in receipts) == ['failed_transport', 'succeeded']
    assert all(row['provider_id'] == 'local' and row['attempt_number'] == 1 for row in receipts)
    store = MemoryTurn.store_for(env.records)
    batches = {}
    old_keys = env.records.list('v2_memory_turn_keys')
    old_requests = requests(env.records)
    for row in old_keys:
        key = row.payload['identity']['key']
        assert key['namespace'] == 'local-index-v1'
        assert key['index_run_revision'] == first_run_revisions[0]
        terminal = [store.get(event['data']['receipt_ref']) for event in store.events_after(row.object_id)
                    if event['type'] == 'model.attempt.terminal']
        assert len(terminal) == 1
        status = terminal[0]['status']
        assert status not in batches
        batches[status] = {(entry['project'], entry['id'], entry['revision'], key['model_key'])
                           for entry in key['entries']}
    first_keys = {(row['project_id'], row['recognition_id'], row['revision'], row['model_id'])
                  for row in first_batch}
    assert batches['succeeded'] == first_keys
    assert batches['failed_transport'] and batches['failed_transport'].isdisjoint(first_keys)
    directory = local_vectors.model_directory(env.root / 'data' / 'models')
    worker = local_vectors.worker_for(directory, policy=vector_policy())
    worker.close()
    assert worker.thread is not None and not worker.thread.is_alive()
    resumed_revisions = []
    def resumed(encoder, texts, input_type, *, policy):
        if input_type == 'document' and not resumed_revisions:
            resumed_revisions.append(env.records.read('v2_embedding_index', 'default').revision)
        return original(encoder, texts, input_type, policy=policy)
    monkeypatch.setattr(local_vectors.SentenceEncoder, 'encode', resumed)
    previous_calls = len(calls)
    restarted = EmbeddingIndex(query)
    missing = failed['progress']['total'] - 8
    assert restarted.run() == missing
    assert resumed_revisions[0] > first_run_revisions[0]
    all_keys = env.records.list('v2_memory_turn_keys')
    assert all(row in all_keys for row in old_keys)
    assert all(row in requests(env.records) for row in old_requests)
    all_receipts = wire_receipts(env.records)
    assert all(row in all_receipts for row in receipts)
    assert all(row['provider_id'] == 'local' and row['attempt_number'] == 1 for row in all_receipts)
    old_ids = {row.object_id for row in old_keys}
    new_keys = [row for row in all_keys if row.object_id not in old_ids]
    assert new_keys and all(row.payload['identity']['key']['namespace'] == 'local-index-v1'
        and row.payload['identity']['key']['index_run_revision'] == resumed_revisions[0] for row in new_keys)
    new_receipts = [store.get(event['data']['receipt_ref']) for row in new_keys
        for event in store.events_after(row.object_id) if event['type'] == 'model.attempt.terminal']
    assert len(new_receipts) == len(new_keys) and all(row['status'] == 'succeeded' for row in new_receipts)
    assert sum(len(texts) for texts, kind in calls[previous_calls:] if kind == 'document') == missing
    ready = env.records.read('v2_embedding_index', 'default').payload
    assert ready['progress'] == {'done': failed['progress']['total'], 'total': failed['progress']['total']}
    assert ready['reason_code'] is None
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        completed = connection.execute('SELECT * FROM recognition_embedding_cache ORDER BY recognition_id').fetchall()
    assert len(completed) == failed['progress']['total'] and all(row in completed for row in first_batch)
    completed_calls = len(calls)
    assert restarted.run() == 0 and len(calls) == completed_calls
    assert wire_receipts(env.records) == all_receipts and env.records.list('v2_memory_turn_keys') == all_keys
    assert {name: env.records.list(name) for name in collections} == facts
    assert query._v1_network_calls == []


def test_background_index_reentry_keeps_one_run_identity(env, local_query, monkeypatch):
    query, calls = local_query
    add_document(env, summary='合成摘要', body='合成事实。' * 180)
    index = EmbeddingIndex(query)
    entered, release = Event(), Event()
    context = copy_context()
    original = local_vectors.SentenceEncoder.encode
    def blocked(encoder, texts, input_type, *, policy):
        entered.set()
        def contender():
            try:
                progress = env.records.read('v2_embedding_index', 'default')
                keys = env.records.list('v2_memory_turn_keys')
                assert index.run() == 0
                assert env.records.read('v2_embedding_index', 'default') == progress
                assert env.records.list('v2_memory_turn_keys') == keys
            finally:
                release.set()
        # 10 秒只约束编码已进入后的真实竞争调用，不给首次内核准备新增上限。
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(context.run, contender)
            try:
                assert release.wait(10)
                future.result(timeout=10)
            finally:
                release.set()
        return original(encoder, texts, input_type, policy=policy)
    monkeypatch.setattr(local_vectors.SentenceEncoder, 'encode', blocked)
    assert index.run() > 0 and entered.is_set()
    receipts = wire_receipts(env.records)
    assert receipts and all(row['status'] == 'succeeded' and row['provider_id'] == 'local'
                           and row['attempt_number'] == 1 for row in receipts)
    assert calls and all(kind == 'document' for _, kind in calls) and query._v1_network_calls == []


@pytest.mark.parametrize('change', ['mode', 'source'])
def test_background_index_rejects_changed_authority_before_cache_write(env, local_query, monkeypatch, change):
    query, calls = local_query
    _, item_id = add_document(env, summary='合成摘要', body='合成事实。' * 180)
    original = local_vectors.SentenceEncoder.encode
    def changed(encoder, texts, input_type, *, policy):
        result = original(encoder, texts, input_type, policy=policy)
        if change == 'mode':
            EmbeddingSettings(env.records, query.models._local_models_root).update_mode(
                mode='remote', expected_revision=0)
        else:
            with env.records.begin() as tx:
                row = tx.read('workspace_items', item_id)
                tx.put('workspace_items', item_id, {**row.payload, 'source_text': '更新后的合成原文'},
                       expected_revision=row.revision)
                tx.commit()
        return result
    monkeypatch.setattr(local_vectors.SentenceEncoder, 'encode', changed)
    expected = RecognitionRetrievalError if change == 'mode' else RecognitionConflict
    with pytest.raises(expected) as rejected:
        EmbeddingIndex(query).run()
    if change == 'mode':
        assert str(rejected.value) == 'configured_model_changed_before_request'
    progress = env.records.read('v2_embedding_index', 'default').payload
    assert progress['progress']['done'] == 0 and progress['reason_code'] == 'embedding_index_interrupted'
    with sqlite3.connect(env.records.database_path.parent / 'recognition-vectors.sqlite3') as connection:
        assert connection.execute('SELECT COUNT(*) FROM recognition_embedding_cache').fetchone()[0] == 0
    receipts = wire_receipts(env.records)
    assert len(receipts) == 1 and receipts[0]['status'] == 'failed_transport'
    assert receipts[0]['provider_id'] == 'local' and receipts[0]['attempt_number'] == 1
    assert len(calls) == 1 and calls[0][1] == 'document' and query._v1_network_calls == []


@pytest.mark.parametrize('unavailable', ['dependencies', 'weights', 'encoder_unavailable'])
def test_workspace_query_keeps_real_keyword_evidence_when_vectors_unavailable(env, local_query, monkeypatch, unavailable):
    query, calls = local_query
    identity, _ = add_document(env, summary='合成概览',
        body='orchidmarker backup connector is blue.', original='orchidmarker backup connector is blue.')
    background_calls = []
    background_receipts = []
    background_requests = []
    failed_encodes = []
    assert env.records.list('v2_embedding_index') == ()
    if unavailable == 'dependencies':
        monkeypatch.setattr('backend.memory_app.v2.embedding_settings.dependencies_available', lambda: False)
    elif unavailable == 'weights':
        directory = local_vectors.model_directory(env.root / 'data' / 'models')
        # 只移除本fixture自建的marker，不碰权威权重。
        (directory / 'model.safetensors').unlink()
    else:
        assert EmbeddingIndex(query).run() > 0
        background_calls = list(calls)
        background_receipts = wire_receipts(env.records)
        background_requests = requests(env.records)
        assert background_calls and all(kind == 'document' for _, kind in background_calls)
        assert background_receipts and all(row['status'] == 'succeeded'
            and row['provider_id'] == 'local' and row['attempt_number'] == 1 for row in background_receipts)
        def unavailable_encoder(encoder, texts, input_type, *, policy):
            failed_encodes.append((tuple(texts), input_type))
            raise local_vectors.LocalVectorError('synthetic_encoder_unavailable')
        monkeypatch.setattr(local_vectors.SentenceEncoder, 'encode', unavailable_encoder)
    plan = query.prepare_ask('alpha', 'orchidmarker backup connector')
    matches = [entry for entry in plan['chosen'] if entry['entry']['id'] == identity and entry['layer'] == 'L1']
    assert len(matches) == 1 and 'orchidmarker backup connector is blue.' in matches[0]['excerpt']
    markdown = env.documents.markdown(identity)
    assert all(window.text == markdown[window.start:window.end] for window in matches[0]['windows'])
    assert calls == background_calls and query._v1_network_calls == []
    receipts = wire_receipts(env.records)
    if unavailable != 'encoder_unavailable':
        assert calls == [] and query.models.public()['embedding']['configured'] is False and receipts == []
    else:
        assert failed_encodes and all(texts == ('orchidmarker backup connector',) and kind == 'query'
                                     for texts, kind in failed_encodes)
        assert all(row in receipts for row in background_receipts)
        assert all(row in requests(env.records) for row in background_requests)
        foreground = [row for row in env.records.list('v2_memory_turn_keys')
            if row.payload['identity']['key'].get('namespace') == 'contextual-chunks-v1']
        assert foreground and all(row.payload['identity']['key']['embedding'] == ['orchidmarker backup connector']
                                  for row in foreground)
        store = MemoryTurn.store_for(env.records)
        failed_receipts = [store.get(event['data']['receipt_ref']) for row in foreground
            for event in store.events_after(row.object_id) if event['type'] == 'model.attempt.terminal']
        assert failed_receipts and all(row['status'] == 'failed_transport' and row['provider_id'] == 'local'
                                      and row['attempt_number'] == 1 for row in failed_receipts)
        assert len(receipts) == len(background_receipts) + len(failed_receipts)




def test_local_foreground_keeps_cold_entries_lexical_until_real_background_index(env, local_query):
    query, calls = local_query
    identity, _ = add_document(env, summary='合成概览',
        body='orchidmarker backup connector is blue.', original='orchidmarker backup connector is blue.')
    collections = ('documents', 'workspace_items', 'recognitions', 'recognition_experiences')
    facts = {name: env.records.list(name) for name in collections}
    assert env.records.list('v2_embedding_index') == ()
    plan = query.prepare_ask('alpha', 'orchidmarker backup connector')
    chosen = [entry for entry in plan['chosen'] if entry['entry']['id'] == identity and entry['layer'] == 'L1']
    assert len(chosen) == 1 and 'orchidmarker backup connector is blue.' in chosen[0]['excerpt']
    markdown = env.documents.markdown(identity)
    assert all(window.text == markdown[window.start:window.end] for window in chosen[0]['windows'])
    assert calls == [] and wire_receipts(env.records) == []
    set_private_project(env.records, 'alpha', True, expected_revision=0)
    computed = EmbeddingIndex(query).run()
    assert computed > 0 and all(kind == 'document' for _, kind in calls)
    progress = env.records.read('v2_embedding_index', 'default').payload
    assert progress['progress'] == {'done': computed, 'total': computed}
    before_query = len(calls)
    scores, chunks = private_scores(env, query, identity=identity)
    assert scores == {index: pytest.approx(1.0) for index in range(len(chunks))}
    assert [kind for _, kind in calls[before_query:]] == ['query']
    receipts = wire_receipts(env.records)
    assert len(receipts) == 2 and all(row['status'] == 'succeeded' and row['provider_id'] == 'local'
        and row['attempt_number'] == 1 for row in receipts)
    assert all(attempt_cost(env.records, receipt) == {'currency': 'CNY', 'amount': '0'} for receipt in receipts)
    assert all(request['privacy']['mode'] == 'local_only' for request in requests(env.records))
    assert {name: env.records.list(name) for name in collections} == facts
    assert query._v1_network_calls == []
