"""Memory generation reuses its real Turn, model attempt and source owners."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, StrictStr

from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from backend.memory_app.kernel.aux_routing import CHOICE_KIND
from backend.memory_app.turn_routing import _revision
from backend.memory_app.v2.memory_turn import MemoryTurn
from backend.memory_app.v2.provider_store_settings import ProviderStoreSettings
from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from backend.security.secrets import InMemorySecretStore
from backend.shared.llm.model_capabilities import ModelCapabilities
from backend.shared.llm.openai_responses import ResponsesCompletion
from core.storage_provider import SQLiteStructuredRecordStore
from tests.backend.unit.llm.test_provider_store_capability import api_mode
from tests.memory_app.test_fast_generation import choose
from tests.memory_app.v2.test_main_provider_background import ProductProvider, store_facts
from tests.memory_app.v2.test_provider_store_bindings import BINDINGS, toggle


class MemoryOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    value: StrictStr


class MemoryProvider(ProductProvider):
    @staticmethod
    def output(messages):
        assert messages == [{'role': 'user', 'content': 'Synthetic memory'}]
        return {'value': 'Synthetic result'}, 'memory'


@pytest.fixture
def env(tmp_path):
    import litellm  # Load installed gateway metadata outside the original runtime budget.
    provider, clients, responses = MemoryProvider(), [], []
    provider.fail_close = False

    class OwnedClient(httpx.Client):
        def close(self):
            super().close()
            if provider.fail_close:
                raise ConnectionError('synthetic memory owner close failure')

    def factory():
        client = OwnedClient(trust_env=False, follow_redirects=False)
        client.event_hooks['response'].append(responses.append)
        clients.append(client)
        return client

    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    native = ResponsesCompletion(api_base=provider.base,
        capabilities=ModelCapabilities(background_resume=True))
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=native,
        model_http_client_factory=factory)
    models.update('generation', {'base_url': provider.base, 'model': 'main', 'api_key': 'synthetic-only',
        'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    api_mode(models)
    service = RecognitionService(records)
    source = service.stage_experience(scope=WorkScope('local-user', 'alpha'), content='Synthetic memory source')
    material = {'type': 'experience', 'id': source, 'project_id': 'alpha', 'revision': 1}
    try:
        yield SimpleNamespace(records=records, models=models, provider=provider, service=service,
            settings=ProviderStoreSettings(records, models), clients=clients, responses=responses,
            material=material)
    finally:
        provider.close()


def memory(env, key='memory-background'):
    return MemoryTurn(env.records, env.models, kind='memory.overview', project='alpha', key=key,
        materials=[env.material], validate=lambda: None)


def generate(turn):
    return turn.generate([{'role': 'user', 'content': 'Synthetic memory'}],
        response_model=MemoryOutput, max_tokens=700)


@pytest.mark.parametrize('fast', [None, 'quick'])
@pytest.mark.parametrize('phase', ['before_output', 'body'])
def test_memory_uses_one_create_cursor_get_and_one_original_logical_receipt(env, fast, phase):
    if fast:
        choose(env.models, fast)
    toggle(env)
    env.provider.phase = phase
    turn = memory(env)
    original = deepcopy(turn.request)
    output, metadata = generate(turn)
    assert output == MemoryOutput(value='Synthetic result')
    assert metadata['model'] == (fast or 'main')
    posts = [call for call in env.provider.calls if call['method'] == 'POST']
    assert len(posts) == 1 and posts[0]['body']['model'] == (fast or 'main')
    assert posts[0]['body'].get('store') is True and posts[0]['body'].get('background') is True
    assert posts[0]['body']['max_output_tokens'] == 700
    gets = [call for call in env.provider.calls if call['method'] == 'GET']
    assert len(gets) == 1 and gets[0]['after'] == (0 if phase == 'before_output' else 1)
    dispatches, terminals, checkpoints, effects = store_facts(turn.store, turn.turn_id)
    assert len(dispatches) == len(terminals) == 1 and dispatches[0]['model_id'] == (fast or 'main')
    assert terminals[0]['status'] == 'succeeded'
    assert terminals[0]['usage'] == {'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7}
    assert effects[dispatches[0]['attempt_id']] == 'SETTLED_OK'
    sequences = [0, 1, 2] if phase == 'before_output' else [0, 1, 2, 3]
    assert [row['cursor'] for row in checkpoints] == [
        {'response_id': gets[0]['identity'], 'sequence_number': sequence} for sequence in sequences]
    assert all(row['run_lease']['owner_id'].startswith('memory-') for row in checkpoints)
    events = tuple(turn.store.events_after(turn.turn_id))
    receipts = [turn.store.get(event['data']['receipt_ref']) for event in events
        if event['type'] == 'model.completed' and event['data'].get('receipt_ref')]
    assert len(receipts) == 1 and receipts[0]['model_call_purpose'] == 'aux'
    assert receipts[0]['model_id'] == (fast or 'main') and receipts[0]['usage'] == metadata['usage']
    assert len([event for event in events if event['type'] == 'model.routed']) == 1
    assert events[-1]['type'] == 'turn.completed' and turn.store.get_run_lease(turn.turn_id) is None
    saved = turn.store.get_immutable_payload(turn.turn_id, 'memory-model-route-v1')
    assert saved[1] == {'provider': 'openai', 'model': fast or 'main', 'base_url': env.provider.base,
        'revision': 1, 'allow_remote': True}
    routed = [event for event in events if event['type'] == 'model.routed']
    assert routed[0]['data']['payload_ref'] == saved[0]
    completed = next(event for event in events if event['type'] == 'model.completed')
    cache_receipts = [turn.store.get(ref) for ref in completed['data']['evidence_refs']
        if turn.store.get(ref).get('receipt_id', '').startswith('prompt-cache-receipt-')]
    assert len(cache_receipts) == 1
    assert cache_receipts[0]['routing_snapshot_revision'] == _revision(saved[1])
    assert cache_receipts[0]['prompt_cache_scope_identity'] == _revision({'turn_id': turn.turn_id, 'route': saved[1]})
    assert env.records.read(BINDINGS, turn.turn_id).payload['auxiliary']['model'] == fast
    assert turn.store.get_immutable_payload(turn.turn_id, CHOICE_KIND)[1] == env.records.read(BINDINGS, turn.turn_id).payload['auxiliary']
    assert turn.request == original and turn.store.get_request(turn.turn_id) == original
    prices = env.records.list('v2_model_wire_prices')
    assert len(prices) == 1 and prices[0].payload['attempt_id'] == dispatches[0]['attempt_id']
    assert generate(turn) == (output, metadata) and len(env.provider.calls) == 2
    assert env.clients and all(client.is_closed for client in env.clients)
    assert env.responses and all(response.is_closed for response in env.responses)


@pytest.mark.parametrize('historical', [False, True])
def test_memory_off_or_missing_history_never_adopts_a_later_background_choice(env, historical):
    turn = memory(env, 'historical')
    row = env.records.read(BINDINGS, turn.turn_id)
    assert row.payload['enabled'] is False
    if historical:
        with env.records.begin() as tx:
            tx.delete(BINDINGS, turn.turn_id, expected_revision=row.revision)
            tx.commit()
    toggle(env)
    result = generate(turn)
    assert result[0] == MemoryOutput(value='Synthetic result')
    assert len(env.provider.calls) == 1 and env.provider.calls[0]['method'] == 'POST'
    assert 'store' not in env.provider.calls[0]['body'] and 'background' not in env.provider.calls[0]['body']
    assert env.records.read(BINDINGS, turn.turn_id) == (None if historical else row)
    assert generate(turn) == result and len(env.provider.calls) == 1
    dispatches, terminals, checkpoints, effects = store_facts(turn.store, turn.turn_id)
    assert len(dispatches) == len(terminals) == 1 and checkpoints == []
    assert terminals[0]['status'] == 'succeeded' and effects[dispatches[0]['attempt_id']] == 'SETTLED_OK'
    assert turn.store.get_run_lease(turn.turn_id) is None
    assert env.clients and all(client.is_closed for client in env.clients)


def test_memory_requires_actual_typed_background_capability(env):
    toggle(env)
    env.models._completion_fn = ResponsesCompletion(api_base=env.provider.base)
    turn = memory(env, 'undeclared')
    assert env.records.read(BINDINGS, turn.turn_id).payload['enabled'] is False
    assert generate(turn)[0] == MemoryOutput(value='Synthetic result')
    assert len(env.provider.calls) == 1 and env.provider.calls[0]['method'] == 'POST'
    assert 'store' not in env.provider.calls[0]['body'] and 'background' not in env.provider.calls[0]['body']
    dispatches, terminals, checkpoints, effects = store_facts(turn.store, turn.turn_id)
    assert len(dispatches) == len(terminals) == 1 and checkpoints == []
    assert effects[dispatches[0]['attempt_id']] == 'SETTLED_OK'
    assert turn.store.get_run_lease(turn.turn_id) is None


def test_memory_custom_invoke_retains_its_original_protocol_even_when_choice_is_on(env):
    toggle(env)
    turn, observed = memory(env, 'custom'), []

    def invoke(control, current):
        observed.append(control)
        return env.models.complete_structured([{'role': 'user', 'content': 'Synthetic memory'}],
            response_model=MemoryOutput, max_tokens=700, validate_current=current, wire_attempt_sink=control)

    output, metadata = turn.generate([], response_model=MemoryOutput, max_tokens=700, invoke=invoke)
    assert output == MemoryOutput(value='Synthetic result') and metadata['model'] == 'main'
    assert len(observed) == 1 and env.records.read(BINDINGS, turn.turn_id).payload['enabled'] is True
    assert len(env.provider.calls) == 1 and env.provider.calls[0]['method'] == 'POST'
    assert 'store' not in env.provider.calls[0]['body'] and 'background' not in env.provider.calls[0]['body']
    dispatches, terminals, checkpoints, effects = store_facts(turn.store, turn.turn_id)
    assert len(dispatches) == len(terminals) == 1 and checkpoints == []
    assert terminals[0]['status'] == 'succeeded' and effects[dispatches[0]['attempt_id']] == 'SETTLED_OK'
    assert turn.store.get_run_lease(turn.turn_id) is None
    assert len([event for event in turn.store.events_after(turn.turn_id) if event['type'] == 'model.routed']) == 1


@pytest.mark.parametrize('revocation', ['fast', 'store', 'generation', 'project', 'source'])
def test_memory_each_cursor_get_rechecks_actual_frozen_source_and_configuration(env, revocation):
    from backend.memory_app.source_egress import SourceEgressService
    from backend.memory_app.v2.privacy import set_private_project
    toggle(env)
    choose(env.models, 'quick')
    turn = memory(env, 'revoke-' + revocation)
    assert turn.request['privacy']['material_refs'] == [env.material]
    assert turn.request['input']['refs'] == [{
        'kind': 'atom', 'object_id': env.material['id'],
        'uri': 'crp://default/recognition_experiences/' + env.material['id']}]
    snapshots = turn.request['privacy']['source_snapshots']
    assert len(snapshots) == 1 and snapshots[0]['scope'] == {'user_id': 'local-user', 'project_id': 'alpha'}
    assert any(node['type'] == 'experience' and node['id'] == env.material['id']
        and node['source_revision'] == 1 for node in snapshots[0]['nodes'])

    def revoke():
        deadline = monotonic() + 2
        while monotonic() < deadline and not store_facts(turn.store, turn.turn_id)[2]:
            sleep(.01)
        assert store_facts(turn.store, turn.turn_id)[2]
        if revocation == 'fast':
            choose(env.models, 'other', 1)
        elif revocation == 'store':
            toggle(env, False)
        elif revocation == 'generation':
            env.models.update('generation', {'base_url': 'https://synthetic.invalid/v1',
                'model': 'other', 'expected_revision': 1})
        elif revocation == 'project':
            set_private_project(env.records, 'alpha', True, 0)
        else:
            SourceEgressService(env.records).set_policy(WorkScope('local-user', 'alpha'),
                'experience', env.material['id'], 1, 0, [])

    env.provider.after_emitted = revoke
    with pytest.raises((ModelConfigurationError, RecognitionConflict)):
        generate(turn)
    assert env.provider.callback_errors == []
    assert len(env.provider.calls) == 1 and env.provider.calls[0]['method'] == 'POST'
    assert env.provider.calls[0]['body']['store'] is True and env.provider.calls[0]['body']['background'] is True
    dispatches, terminals, checkpoints, effects = store_facts(turn.store, turn.turn_id)
    assert len(dispatches) == len(terminals) == 1 and checkpoints
    assert terminals[0]['status'] == 'failed_transport' and terminals[0]['usage'] is None
    assert terminals[0]['usage_status'] == 'unavailable' and effects[dispatches[0]['attempt_id']] == 'UNKNOWN'
    assert turn.store.get_immutable_payload(turn.turn_id, 'memory-generation-output-v1') is None
    assert turn.store.get_run_lease(turn.turn_id).status == 'active'
    failed = next(event for event in turn.store.events_after(turn.turn_id) if event['type'] == 'model.failed')
    receipt = turn.store.get(failed['data']['receipt_ref'])
    assert receipt['status'] == 'failed' and receipt['usage'] is None
    assert receipt['usage_status'] == 'not_recorded' and receipt['model_call_purpose'] == 'aux'
    assert tuple(turn.store.events_after(turn.turn_id))[-1]['type'] == 'turn.failed'
    assert env.clients and all(client.is_closed for client in env.clients)
    assert env.responses and all(response.is_closed for response in env.responses)


@pytest.mark.parametrize('failure', ['observer', 'close'])
def test_memory_observer_or_owned_cleanup_failure_never_recreates_provider_request(env, failure):
    toggle(env)
    turn = memory(env, 'fault-' + failure)
    if failure == 'observer':
        with sqlite3.connect(turn.store._path) as connection:
            connection.execute("CREATE TRIGGER synthetic_memory_observer BEFORE INSERT ON ai_turn_immutable_payloads "
                "WHEN NEW.kind LIKE 'model-provider-checkpoint-%' BEGIN SELECT RAISE(ABORT,'synthetic memory'); END")
    else:
        env.provider.fail_close = True
    with pytest.raises(ModelConfigurationError):
        generate(turn)
    assert len(env.provider.calls) == 1 and env.provider.calls[0]['method'] == 'POST'
    assert env.provider.calls[0]['body']['background'] is True
    dispatches, terminals, checkpoints, effects = store_facts(turn.store, turn.turn_id)
    assert len(dispatches) == len(terminals) == 1
    assert terminals[0]['status'] == 'failed_transport' and terminals[0]['usage'] is None
    assert effects[dispatches[0]['attempt_id']] == 'UNKNOWN'
    if failure == 'observer':
        assert checkpoints == []
    assert turn.store.get_immutable_payload(turn.turn_id, 'memory-generation-output-v1') is None
    assert turn.store.get_run_lease(turn.turn_id).status == 'active'
    assert tuple(turn.store.events_after(turn.turn_id))[-1]['type'] == 'turn.failed'
    assert env.clients and all(client.is_closed for client in env.clients)
    assert env.responses and all(response.is_closed for response in env.responses)


def test_memory_two_instances_never_take_over_a_busy_real_owner(env):
    toggle(env)
    turn = memory(env, 'concurrent')
    entered, release = Event(), Event()

    def hold():
        deadline = monotonic() + 2
        while monotonic() < deadline and not store_facts(turn.store, turn.turn_id)[2]:
            sleep(.01)
        assert store_facts(turn.store, turn.turn_id)[2]
        entered.set()
        assert release.wait(4)

    env.provider.after_emitted = hold
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(generate, turn)
        assert entered.wait(4)
        try:
            lease = turn.store.get_run_lease(turn.turn_id)
            assert lease.status == 'active' and lease.token.owner_id.startswith('memory-')
            second = memory(env, 'concurrent')
            assert second.turn_id == turn.turn_id
            with pytest.raises(RecognitionConflict, match='memory_turn_result_unavailable'):
                generate(second)
            assert turn.store.get_run_lease(turn.turn_id) == lease
            assert len(env.provider.calls) == 1 and env.provider.calls[0]['method'] == 'POST'
        finally:
            release.set()
        output, _ = future.result(timeout=5)
    assert env.provider.callback_errors == [] and output == MemoryOutput(value='Synthetic result')
    assert len(env.provider.calls) == 2 and env.provider.calls[1]['method'] == 'GET'
    dispatches, terminals, _checkpoints, effects = store_facts(turn.store, turn.turn_id)
    assert len(dispatches) == len(terminals) == 1 and effects[dispatches[0]['attempt_id']] == 'SETTLED_OK'
    assert turn.store.get_run_lease(turn.turn_id) is None


@pytest.mark.parametrize('mismatch', ['kind', 'ref', 'route', 'boolean_revision', 'selected'])
def test_memory_governed_projection_requires_the_original_exact_route_and_actual_selected_owner(env, mismatch):
    from backend.memory_app.kernel.aux_routing import auxiliary_models
    from backend.memory_app.kernel.provider_store_binding import memory_provider_store_call
    toggle(env)
    choose(env.models, 'quick')
    turn = memory(env, 'canonical')
    assert turn.store.claim_turn(turn.request) == (turn.turn_id, True)
    selected = auxiliary_models(env.models, turn.store, turn.turn_id, records=env.records)
    configuration = selected.public()['generation']
    route = {field: configuration[field] for field in ('provider', 'model', 'base_url', 'revision', 'allow_remote')}
    ref = turn.store.get_or_create_immutable_payload(turn.turn_id, 'memory-model-route-v1', route)
    row, request = env.records.read(BINDINGS, turn.turn_id), deepcopy(turn.request)
    governed, activation = memory_provider_store_call(env.models, selected, env.records, turn.store, request, ref, route)
    assert governed == {'payload_ref': ref, 'revision': _revision(route),
        'prompt_cache_scope_identity': _revision({'turn_id': turn.turn_id, 'route': route}),
        'configuration': row.payload['configuration'], 'execution_location': 'local_loopback'}
    assert activation.expected_purpose == 'aux'
    changed = deepcopy(route)
    if mismatch == 'kind':
        request['desired_outcome'] = 'project.answer'
    elif mismatch == 'ref':
        ref = turn.store.get_immutable_payload(turn.turn_id, CHOICE_KIND)[0]
    elif mismatch == 'route':
        changed['model'] = 'foreign'
    elif mismatch == 'boolean_revision':
        changed['revision'] = True
    else:
        selected = env.models
    with pytest.raises(ModelConfigurationError, match='provider_store_binding_invalid'):
        memory_provider_store_call(env.models, selected, env.records, turn.store, request, ref, changed)
    assert env.provider.calls == [] and env.records.read(BINDINGS, turn.turn_id) == row
    assert turn.store.get_immutable_payload(turn.turn_id, 'memory-model-route-v1')[1] == route
    assert store_facts(turn.store, turn.turn_id)[:3] == ([], [], [])
    assert turn.store.get_run_lease(turn.turn_id) is None


def test_memory_vision_custom_invoke_keeps_its_original_independent_configuration(env):
    toggle(env)
    choose(env.models, 'quick')
    env.models.update('vision', {'base_url': env.provider.base, 'model': 'vision', 'api_key': 'synthetic-only',
        'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    env.models.update_vision_mode(mode='remote', expected_revision=0)
    turn = MemoryTurn(env.records, env.models, kind='memory.overview', project='alpha', key='vision',
        purpose='vision', materials=[env.material], validate=lambda: None)

    def invoke(control, current):
        return env.models.complete_vision([{'role': 'user', 'content': 'Synthetic memory'}],
            response_model=MemoryOutput, max_tokens=700, validate_current=current, wire_attempt_sink=control)

    with httpx.Client(trust_env=False, follow_redirects=False) as borrowed:
        borrowed.event_hooks['response'].append(env.responses.append)
        env.models._completion_fn = ResponsesCompletion(client=borrowed, api_base=env.provider.base,
            capabilities=ModelCapabilities(background_resume=True))
        output, metadata = turn.generate([], response_model=MemoryOutput, max_tokens=700, invoke=invoke)
        assert output == MemoryOutput(value='Synthetic result') and metadata['model'] == 'vision'
        assert env.records.read(BINDINGS, turn.turn_id) is None
        assert len(env.provider.calls) == 1 and env.provider.calls[0]['body']['model'] == 'vision'
        assert 'store' not in env.provider.calls[0]['body'] and 'background' not in env.provider.calls[0]['body']
        dispatches, terminals, checkpoints, effects = store_facts(turn.store, turn.turn_id)
        assert len(dispatches) == len(terminals) == 1 and dispatches[0]['model_id'] == 'vision'
        assert terminals[0]['status'] == 'succeeded' and effects[dispatches[0]['attempt_id']] == 'SETTLED_OK'
        assert checkpoints == [] and turn.store.get_run_lease(turn.turn_id) is None
        assert env.clients == [] and borrowed.is_closed is False
        assert env.responses and all(response.is_closed for response in env.responses)
    assert borrowed.is_closed is True


def test_original_embedding_request_remains_one_plain_wire_without_background_binding(env):
    from backend.memory_app.retrieval_models import ConfiguredTransport
    from backend.memory_app.v2.memory_turn import embedding_request
    toggle(env)
    choose(env.models, 'quick')
    calls = []
    response = {'data': [{'index': 0, 'embedding': [1.0, 0.0]}],
        'usage': {'input_tokens': 4, 'total_tokens': 4}}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append({'path': self.path, 'body': json.loads(self.rfile.read(int(self.headers['Content-Length'])))})
            raw = json.dumps(response).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}/v1'
    try:
        env.models.update('embedding', {'base_url': base, 'model': 'embedding', 'api_key': 'synthetic-only',
            'allow_remote': True, 'enabled': True, 'expected_revision': 0})
        request = {'endpoint': base + '/embeddings', 'payload': {'model': 'embedding', 'input': ['Synthetic memory']}}
        result = embedding_request(env.records, env.models, 'alpha', [env.material], 'embedding', lambda: None,
            ConfiguredTransport('synthetic-only'), request)
        assert result == response and calls == [{'path': '/v1/embeddings', 'body': request['payload']}]
        assert env.provider.calls == []
        row = next(row for row in env.records.list('v2_memory_turn_keys') if row.payload['identity'].get('purpose') == 'embedding')
        turn_id = row.object_id
        store = MemoryTurn.store_for(env.records)
        assert env.records.read(BINDINGS, turn_id) is None
        assert 'store' not in calls[0]['body'] and 'background' not in calls[0]['body']
        dispatches, terminals, checkpoints, effects = store_facts(store, turn_id)
        assert len(dispatches) == len(terminals) == 1 and dispatches[0]['model_id'] == 'embedding'
        assert terminals[0]['usage'] == {'input_tokens': 4, 'output_tokens': 0, 'total_tokens': 4}
        assert terminals[0]['status'] == 'succeeded' and effects[dispatches[0]['attempt_id']] == 'SETTLED_OK'
        assert checkpoints == [] and store.get_run_lease(turn_id) is None
        assert embedding_request(env.records, env.models, 'alpha', [env.material], 'embedding', lambda: None,
            ConfiguredTransport('synthetic-only'), request) == result and len(calls) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
