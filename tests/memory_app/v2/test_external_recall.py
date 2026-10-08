"""External collection uses real domain owners and never creates embedding Turns."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

import pytest

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.external_agent_settings import external_agent_settings, replace_external_agent_settings
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.v2.policies import override
from backend.recognition import RecognitionError, WorkScope
from backend.security.secrets import InMemorySecretStore
from tests.memory_app.v2.test_workbench_ask import env as env, add_document
from tests.memory_app.v2.kernel_receipts import requests, wire_receipts


@pytest.fixture
def embedding_channel():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, payload))
            body = json.dumps({'data': [{'index': index, 'embedding': [1.0, 0.0]}
                for index, _ in enumerate(payload['input'])], 'usage': {'prompt_tokens': 5}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def external_settings(env, **changes):
    current = external_agent_settings(env.records)
    replace_external_agent_settings(env.records,
        {key: value for key, value in current.items() if key != 'revision'} | changes,
        expected_revision=current['revision'])


def models(env, endpoint):
    configuration = ModelConfiguration(env.records, env.root, InMemorySecretStore())
    configuration.update('generation', {'expected_revision': 0, 'base_url': 'https://example.invalid/v1',
        'model': 'synthetic', 'api_key': 'synthetic', 'allow_remote': False})
    configuration.update('embedding', {'expected_revision': 0, 'base_url': endpoint,
        'model': 'synthetic-vector', 'api_key': 'synthetic', 'allow_remote': True, 'enabled': True})
    env.domains.query.models = configuration
    return configuration


def method(env):
    scope = WorkScope('local-user', 'alpha')
    experience = env.service.stage_experience(scope=scope, content='真实合成方法出处')
    candidate = env.service.propose(scope=scope, content='先问近期爱好和实际预算',
        conditions=['挑礼物时'], source_experience_ids=[experience])
    return env.service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer='local-user')


def test_external_lexical_and_methods_ignore_generation_without_new_models(env, embedding_channel):
    endpoint, calls = embedding_channel
    doc, _ = add_document(env, summary='礼物摘要', body='礼物原文证据。' * 120)
    one = method(env)
    configuration = models(env, endpoint)
    external_settings(env, allow_remote=True)
    before_requests, before_receipts = deepcopy(requests(env.records)), deepcopy(wire_receipts(env.records))
    before_config = configuration.public()
    with override(retrieve='@2'):
        result = env.domains.query.collect_candidates('alpha', '礼物原文证据',
            situation='挑礼物', external_client='codex')
    assert any(row['entry']['id'] == doc and row['layer'] == 'L1' for row in result['candidates'])
    assert {row['id'] for row in result['method_candidates']} == {one.id}
    assert calls == []
    assert requests(env.records) == before_requests
    assert wire_receipts(env.records) == before_receipts
    assert configuration.public() == before_config
    # The same configured provider remains live on the unchanged default path.
    ordinary = env.domains.query.collect_candidates('alpha', '礼物原文证据')
    assert any(row['entry']['id'] == doc for row in ordinary['candidates'])
    assert calls
    assert len(requests(env.records)) > len(before_requests)
    assert len(wire_receipts(env.records)) > len(before_receipts)
    assert configuration.public() == before_config


@pytest.mark.parametrize('blocked', ['default', 'disabled_client', 'unknown_client', 'private_project'])
def test_external_closed_scope_rejects_before_material_or_embedding(env, embedding_channel, blocked):
    endpoint, calls = embedding_channel
    add_document(env)
    models(env, endpoint)
    client = 'codex'
    if blocked != 'default':
        external_settings(env, allow_remote=True)
    if blocked == 'disabled_client':
        external_settings(env, clients={'claude': True, 'codex': False})
    if blocked == 'unknown_client':
        client = 'other'
    if blocked == 'private_project':
        set_private_project(env.records, 'alpha', True, 0)
    original_requests = deepcopy(requests(env.records))
    with pytest.raises(RecognitionError, match='external_agent_remote_blocked'):
        env.domains.query.collect_candidates('alpha', 'alpha', external_client=client)
    assert calls == []
    assert requests(env.records) == original_requests


def test_external_preserves_private_original_source_closure(env, embedding_channel):
    endpoint, calls = embedding_channel
    doc, item = add_document(env, body='完整私密原文 alpha')
    models(env, endpoint)
    external_settings(env, allow_remote=True)
    authority = SourceEgressService(env.records)
    authority.set_policy(WorkScope('local-user', 'alpha'), 'original_item', item,
        env.records.read('workspace_items', item).revision, 0, [])
    before = deepcopy(requests(env.records))
    result = env.domains.query.collect_candidates('alpha', 'alpha', external_client='codex')
    assert not any(row['entry']['id'] in {doc, item} for row in result['candidates'])
    assert result['excluded_sources']
    assert calls == []
    assert requests(env.records) == before


def test_external_repeated_collection_is_deterministic_and_preserves_default_methods(env, embedding_channel):
    endpoint, calls = embedding_channel
    one = method(env)
    models(env, endpoint)
    external_settings(env, allow_remote=True)
    query = env.domains.query
    first = query.collect_candidates('alpha', '挑礼物', external_client='claude')
    second = query.collect_candidates('alpha', '挑礼物', external_client='claude')
    assert first == second
    assert {row['id'] for row in first['method_candidates']} == {one.id}
    assert query.collect_candidates('alpha', '挑礼物')['method_candidates'] == []
    assert calls == []
