"""原向量冻结与交接事务的可选输入校验，不替换领域 owner。"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from backend.memory_app.retrieval_models import ConfiguredTransport
from backend.memory_app.v2 import contextual_chunk_vectors as vectors_module
from backend.memory_app.v2.external_context import ARCHIVE, DELIVERIES, ExternalContextError
from backend.shared.secret_detection import contains_secret
from tests.memory_app.v2.test_contextual_chunk_vectors import prepared, score, vectors
from tests.memory_app.v2.test_external_context import DAY, TURN, request, settings, setup
from tests.memory_app.v2.test_workbench_ask import env as env, add_document


def reject_secrets(value, credential=''):
    if isinstance(value, str):
        if contains_secret(value) or (credential and credential in value):
            raise ValueError('synthetic_input_denied')
    elif isinstance(value, dict):
        for key, item in value.items():
            reject_secrets(key, credential)
            reject_secrets(item, credential)
    elif isinstance(value, (tuple, list)):
        for item in value:
            reject_secrets(item, credential)


@pytest.mark.parametrize('where', ['projected', 'question', 'wire'])
def test_vector_validation_precedes_real_memory_key_and_dispatch(env, vectors, where):
    credential = 'synthetic-' + 'opaque-vector-input'
    body = '原始正文。' * 100 + (credential if where == 'projected' else '')
    document, _ = add_document(env, body=body)
    data = prepared(env, document)
    original_markdown = env.documents.markdown(document)
    before = env.records.list('v2_memory_turn_keys')
    cache = env.records.database_path.parent / 'recognition-vectors.sqlite3'
    cache_existed = cache.exists()
    observations = []

    def validate(value):
        observations.append(type(value).__name__)
        reject_secrets(value, credential)
        if where == 'wire' and isinstance(value, list) and value and isinstance(value[0], str):
            raise ValueError('synthetic_wire_denied')

    with vectors_module.embedding_input_validation(validate):
        assert score(data, question=credential if where == 'question' else '原始正文是什么？') == {}
    assert observations and vectors == []
    assert env.records.list('v2_memory_turn_keys') == before
    if where != 'wire':
        assert cache.exists() == cache_existed
    elif cache.exists():
        # 缓存读取可建空schema；真实wire输入拒绝后仍不得保存向量内容。
        import sqlite3
        with sqlite3.connect(cache) as connection:
            assert connection.execute('SELECT COUNT(*) FROM recognition_embedding_cache').fetchone()[0] == 0
    assert env.documents.markdown(document) == original_markdown


def test_cache_write_revalidates_projected_input_after_real_wire(env, monkeypatch):
    document, _ = add_document(env, body='安全正文。' * 100)
    data = prepared(env, document)
    sent, cache_checks = [], []

    def wire(transport, *, endpoint, payload):
        transport._check_current()
        sent.append(payload['input'])
        return {'data': [{'index': i, 'embedding': [1.0, 0.0]}
                         for i, _ in enumerate(payload['input'])], 'usage': {'prompt_tokens': 5}}

    def validate(value):
        if isinstance(value, list) and value and isinstance(value[0], dict):
            cache_checks.append(len(sent))
            if sent:
                raise ValueError('synthetic_cache_denied')

    monkeypatch.setattr(ConfiguredTransport, 'post_json', wire)
    with vectors_module.embedding_input_validation(validate):
        assert score(data) == {}
    assert len(sent) == 1 and cache_checks[0] == 0 and 1 in cache_checks
    import sqlite3
    with sqlite3.connect(data[0].records.database_path.parent / 'recognition-vectors.sqlite3') as connection:
        assert connection.execute('SELECT COUNT(*) FROM recognition_embedding_cache').fetchone()[0] == 0


def test_nested_input_scope_restores_outer_guard_and_resets_after_exception(env, vectors):
    document, _ = add_document(env, body='安全正文。' * 100)
    data = prepared(env, document)

    def deny(value):
        raise ValueError('synthetic_scope_denied')

    with pytest.raises(RuntimeError, match='scope_complete'):
        with vectors_module.embedding_input_validation(deny):
            assert score(data) == {}
            with vectors_module.embedding_input_validation(lambda value: None):
                assert score(data)
            assert score(data) == {}
            raise RuntimeError('scope_complete')
    assert score(data) and len(vectors) == 2


def test_concurrent_input_scopes_do_not_share_denial(env, vectors):
    document, _ = add_document(env, body='安全正文。' * 100)
    data = prepared(env, document)
    barrier = Barrier(2)

    def run(deny):
        def validate(value):
            if deny:
                raise ValueError('synthetic_scope_denied')
        with vectors_module.embedding_input_validation(validate):
            barrier.wait(timeout=10)
            return score(data)

    with ThreadPoolExecutor(max_workers=2) as pool:
        rejected, allowed = pool.submit(run, True), pool.submit(run, False)
        assert rejected.result(timeout=20) == {}
        assert allowed.result(timeout=20)
    assert len(vectors) == 1


@pytest.mark.parametrize('field', ['opaque_excerpt', 'shape_excerpt', 'title'])
def test_archive_validation_rejects_inside_original_transaction(env, field):
    credential = 'synthetic-' + 'opaque-archive-input'
    text = credential if field == 'opaque_excerpt' else ('secret =\n"' + 'Z' * 24 + '"')
    original = env.domains.items.create('alpha', 'text',
        text if field == 'title' else '合成标题', '安全正文' if field == 'title' else text)
    selection = {'type': 'original_item', 'id': original['id'], 'project_id': 'alpha',
                 'revision': 1, 'layer': 'L0', 'windows': []}
    api, _, _ = setup(env)
    settings(env, allow_remote=True)
    seen = []

    def validate(payload):
        seen.append(set(payload))
        assert env.records.read('v2_external_agent_bindings', TURN) is None
        reject_secrets(payload, credential)

    with pytest.raises(ExternalContextError, match='^external_context_unavailable$'):
        api.prepare(TURN, request(), [selection], session_id='session-external',
            operation_id='op-external', idempotency_key=TURN, created_at=DAY.isoformat(),
            validate_archive=validate)
    assert seen == [{'binding', 'selections', 'handoff'}]
    assert env.records.list('v2_external_agent_bindings') == ()
    assert env.records.list('v2_external_agent_reservations') == ()
    assert env.records.list(DELIVERIES) == ()
    assert api.turns.get_request(TURN) is None
    assert api.turns.get_immutable_payload(TURN, ARCHIVE) is None
    assert env.records.read('workspace_items', original['id']).payload['source_text'] == ('安全正文' if field == 'title' else text)
    assert env.model.calls == 0


def test_archive_validator_gets_detached_real_fields_before_commit(env):
    original = env.domains.items.create('alpha', 'text', '合成标题', '真实安全原文')
    selection = {'type': 'original_item', 'id': original['id'], 'project_id': 'alpha',
                 'revision': 1, 'layer': 'L0', 'windows': []}
    api, runtime, runner = setup(env)
    settings(env, allow_remote=True)
    seen = []

    def validate(payload):
        seen.append(payload['handoff']['entries'][0]['excerpt'])
        assert payload['binding']['material_refs'][0]['id'] == original['id']
        assert payload['binding']['source_snapshots'][0]['nodes']
        assert env.records.read('v2_external_agent_bindings', TURN) is None
        payload['binding']['request_json'] = 'mutated'
        payload['handoff']['entries'][0]['excerpt'] = 'mutated'

    frozen = api.prepare(TURN, request(), [selection], session_id='session-external',
        operation_id='op-external', idempotency_key=TURN, created_at=DAY.isoformat(), validate_archive=validate)
    archive = api.turns.get_immutable_payload(TURN, ARCHIVE)[1]
    assert seen == ['真实安全原文']
    assert archive['handoff']['entries'][0]['excerpt'] == '真实安全原文'
    assert archive['binding']['request_json'] != 'mutated'
    assert api.turns.get_request(TURN) == frozen
    assert api.execute(TURN, runtime=runtime, runner=runner)['entries'][0]['excerpt'] == '真实安全原文'


def test_none_archive_validator_keeps_original_frozen_request_bytes(env):
    from copy import deepcopy
    import json
    original = env.domains.items.create('alpha', 'text', '合成标题', '真实安全原文')
    selection = {'type': 'original_item', 'id': original['id'], 'project_id': 'alpha',
                 'revision': 1, 'layer': 'L0', 'windows': []}
    api, _, _ = setup(env)
    settings(env, allow_remote=True)
    common = dict(session_id='session-external', operation_id='op-external', created_at=DAY.isoformat())
    first = api.prepare(TURN, request(), [selection], idempotency_key=TURN, **common)
    other = 'turn-' + 'b' * 32
    second = api.prepare(other, request(), [selection], idempotency_key=other, validate_archive=None, **common)
    second = deepcopy(second)
    second['turn_id'], second['idempotency_key'] = first['turn_id'], first['idempotency_key']
    assert json.dumps(first, sort_keys=True, ensure_ascii=False) == json.dumps(second, sort_keys=True, ensure_ascii=False)
