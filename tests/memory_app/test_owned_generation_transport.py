"""Actual configured gateways, original SDK conversion and local HTTP attempts."""
import json
from pathlib import Path

import httpx
import pytest

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.policies import get
from backend.security.secrets import InMemorySecretStore
from core.ai_kernel import SQLiteAITurnStore, SynchronousAIRuntime, ScopedCapabilityRegistry
from core.storage_provider import SQLiteStructuredRecordStore
from tests.backend.unit.llm.test_owned_model_transport import OwnedProvider
from tests.backend.unit.llm.test_model_transport_watchdog import Answer


@pytest.mark.parametrize('endpoint,model', [
    ('https://api.deepseek.com', 'deepseek-flash'),
    ('https://synthetic-proxy.invalid/v1', 'writer'),
])
def test_actual_configured_endpoint_uses_original_sdk_and_closed_owned_attempts(tmp_path, monkeypatch, endpoint, model):
    import litellm
    provider = OwnedProvider('healthy')
    clients, requests, outputs, checks = [], [], [], []
    injected = [False]
    store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')

    class RoutedTransport(httpx.BaseTransport):
        # This is the external fake provider boundary; SDK/body/auth stay real.
        def __init__(self):
            self.inner = httpx.HTTPTransport()

        def handle_request(self, request):
            if injected[0] and requests:
                assert all(client.is_closed for client in clients[:-1])
                assert len([event for event in runtime.events_after('turn-configured-owned')
                    if event['type'] == 'model.attempt.terminal']) == 1
            body = json.loads(request.content)
            requests.append((str(request.url), body))
            request.url = httpx.URL(provider.base + '/chat/completions')
            return self.inner.handle_request(request)

        def close(self):
            self.inner.close()

    def factory():
        client = httpx.Client(transport=RoutedTransport())
        clients.append(client)
        return client

    shared = httpx.Client(transport=RoutedTransport())
    original_hooks = {kind: list(values) for kind, values in shared.event_hooks.items()}
    monkeypatch.setattr(litellm, 'client_session', shared)
    models = ModelConfiguration(SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3'),
        tmp_path, InMemorySecretStore(), model_http_client_factory=factory)
    models.update('generation', {'base_url': endpoint, 'model': model,
        'api_key': 'synthetic-private-test', 'allow_remote': True, 'expected_revision': 0})
    before = models.public(), models.snapshot('generation')

    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control):
            cfg = models.public()['generation']
            public = {key: cfg[key] for key in ('purpose', 'provider', 'base_url', 'model',
                'allow_remote', 'revision', 'configured', 'has_api_key')}
            ref = store.get_or_create_immutable_payload(request['turn_id'], 'memory-model-route-v1', public)
            route = {'payload_ref': ref, 'revision': 'a' * 64,
                'prompt_cache_scope_identity': 'b' * 64, 'configuration': public,
                'execution_location': 'remote'}

            def validate():
                execution_control.checkpoint()
                checks.append(len(requests))

            result = models.complete_governed([{'role': 'developer', 'content': 'synthetic'}],
                routing_snapshot=route, execution_control=execution_control,
                metadata_sink=execution_control, wire_attempt_sink=execution_control,
                response_model=Answer, validate_current=validate,
                **({'retry_policy': get('retry', version='@1')} if injected[0] else {}))
            outputs.append(result)
            return {'type': 'complete', 'summary': result[0].answer}

    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store)
    request = json.loads((Path(__file__).resolve().parents[2] /
        'core-contracts/ai/fixtures/turn-request/valid-project-answer.json').read_text(encoding='utf-8'))
    request['turn_id'] = 'turn-configured-owned'
    try:
        baseline = runtime.submit_turn({**request, 'turn_id': 'turn-configured-original',
            'operation_id': 'op-configured-original', 'idempotency_key': 'configured-original'})
        assert baseline.status == 'completed' and clients == [] and len(requests) == 1
        assert shared.is_closed is False and shared.event_hooks == original_hooks
        original_body = requests[0][1]
        provider.close()
        provider = OwnedProvider('error')
        injected[0] = True
        requests.clear()
        outputs.clear()
        checks.clear()
        receipt = runtime.submit_turn(request)
        terminals = [store.get(event['data']['receipt_ref']) for event in runtime.events_after(receipt.turn_id)
            if event['type'] == 'model.attempt.terminal']
        assert receipt.status == 'completed' and outputs[0][0].answer == 'done'
        assert len(provider.calls) == len(requests) == len(clients) == len(terminals) == 2
        assert [row['status'] for row in terminals] == ['failed_transport', 'succeeded']
        assert len({row['attempt_id'] for row in terminals}) == 2
        assert all(client.is_closed for client in clients)
        assert all(url == endpoint.rstrip('/') + '/chat/completions' if endpoint.endswith('/v1')
            else url == endpoint + '/v1/chat/completions' for url, _ in requests)
        assert original_body['model'] == model
        assert all(body == original_body for _, body in requests)
        assert shared.is_closed is False and shared.event_hooks == original_hooks
        print('OWNED_CONFIGURED_PROTOCOL', {'model': model,
            'roles': [message['role'] for message in original_body['messages']],
            'body_equal_to_original_none': True, 'original_turn': baseline.turn_id,
            'retry_turn': receipt.turn_id, 'owned_clients_closed': len(clients)})
        assert 0 in checks and 1 in checks and 2 in checks
        assert (models.public(), models.snapshot('generation')) == before
        assert terminals[1]['usage'] == {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}
    finally:
        for client in clients:
            client.close()
        shared.close()
        provider.close()
