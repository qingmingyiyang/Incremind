"""Routing background reads keep the real four-second auxiliary Turn owner."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace

import httpx
import pytest

from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from backend.memory_app.kernel.aux_routing import CHOICE_KIND, auxiliary_models
from backend.memory_app.v2.provider_store_settings import ProviderStoreSettings
from backend.memory_app.v2.route import RouteService
from backend.memory_app.turn_routing import _revision
from backend.security.secrets import InMemorySecretStore
from backend.shared.llm.model_capabilities import ModelCapabilities
from backend.shared.llm.openai_responses import ResponsesCompletion
from core.storage_provider import SQLiteStructuredRecordStore
from tests.backend.unit.llm.test_provider_store_capability import api_mode
from tests.memory_app.test_fast_generation import choose
from tests.memory_app.v2.test_main_provider_background import ProductProvider, store_facts
from tests.memory_app.v2.test_provider_store_bindings import BINDINGS, toggle
from tests.memory_app.v2.test_route import TEXT, good_output


class RouteProvider(ProductProvider):
    @staticmethod
    def output(messages):
        value = json.loads(messages[-1]['content'])
        assert value['original_text'] == TEXT and value['project_id'] == 'alpha'
        return good_output(), 'route'


@pytest.fixture
def env(tmp_path):
    import litellm  # Preserve the original Route fixture's dependency preload.
    provider, clients, responses = RouteProvider(), [], []
    provider.fail_close = False

    class OwnedClient(httpx.Client):
        def close(self):
            super().close()
            if provider.fail_close:
                raise ConnectionError('synthetic route owner close failure')

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
    route = RouteService(records, models)
    try:
        yield SimpleNamespace(root=tmp_path, records=records, models=models, provider=provider,
            route=route, store=route.store, settings=ProviderStoreSettings(records, models),
            clients=clients, responses=responses)
    finally:
        provider.close()


@pytest.mark.parametrize('fast', [None, 'quick'])
@pytest.mark.parametrize('phase', ['before_output', 'body'])
def test_route_uses_one_create_then_cursor_get_under_its_frozen_auxiliary_owner(env, fast, phase):
    if fast:
        choose(env.models, fast)
    toggle(env)
    env.provider.phase = phase
    result = env.route.route_model(TEXT, project_id='alpha', request_key='background')
    assert result.mode == 'model', result.reason
    assert [part.model_dump() for part in result.parts] == good_output()['parts']
    posts = [call for call in env.provider.calls if call['method'] == 'POST']
    assert len(posts) == 1 and posts[0]['body']['model'] == (fast or 'main')
    assert posts[0]['body'].get('background') is True and posts[0]['body'].get('store') is True
    assert posts[0]['body']['max_output_tokens'] == 400
    gets = [call for call in env.provider.calls if call['method'] == 'GET']
    assert len(gets) == 1 and gets[0]['after'] == (0 if phase == 'before_output' else 1)
    dispatches, terminals, checkpoints, effects = store_facts(env.store, result.turn_id)
    assert len(dispatches) == len(terminals) == 1
    assert dispatches[0]['model_id'] == (fast or 'main')
    assert terminals[0]['status'] == 'succeeded'
    assert terminals[0]['usage'] == {'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7}
    assert effects[dispatches[0]['attempt_id']] == 'SETTLED_OK'
    sequences = [0, 1, 2] if phase == 'before_output' else [0, 1, 2, 3]
    assert [row['cursor'] for row in checkpoints] == [
        {'response_id': gets[0]['identity'], 'sequence_number': sequence} for sequence in sequences]
    receipts = [env.store.get(event['data']['receipt_ref']) for event in env.store.events_after(result.turn_id)
        if event['type'] == 'model.completed']
    assert len(receipts) == 1 and receipts[0]['model_call_purpose'] == 'aux'
    assert receipts[0]['model_id'] == (fast or 'main')
    assert result.usage == receipts[0]['usage'] and result.egress_receipt_id == receipts[0]['receipt_id']
    request = env.store.get_request(result.turn_id)
    assert request['execution_policy']['budget']['planner_timeout_ms'] == 4000
    assert request['capability_policy']['allowed'] == []
    assert env.records.read(BINDINGS, result.turn_id).payload['auxiliary']['model'] == fast
    prices = [row.payload for row in env.records.list('v2_model_wire_prices')]
    assert len(prices) == 1 and prices[0]['attempt_id'] == dispatches[0]['attempt_id']
    assert env.route.route_model(TEXT, project_id='alpha', request_key='background') == result
    assert len(env.provider.calls) == 2
    assert all(client.is_closed for client in env.clients)
    assert all(response.is_closed for response in env.responses)


def frozen_activation(env, request, route):
    from backend.memory_app.kernel.provider_store_binding import route_provider_store_activation
    selected = auxiliary_models(env.models, env.store, request['turn_id'], records=env.records)
    return route_provider_store_activation(env.models, selected, env.records, env.store, request, route)


def completed_route(env):
    toggle(env)
    choose(env.models, 'quick')
    result = env.route.route_model(TEXT, project_id='alpha', request_key='identity')
    assert result.mode == 'model', result.reason
    request = env.store.get_request(result.turn_id)
    saved = env.store.get_immutable_payload(result.turn_id, 'workbench-route-model-v1')
    body = saved[1]
    route = {'payload_ref': saved[0], 'revision': _revision(body),
        'prompt_cache_scope_identity': _revision(body), 'configuration': body['configuration'],
        'execution_location': body['execution_location']}
    return request, route


@pytest.mark.parametrize('mismatch', ['kind', 'ref', 'revision', 'cache_identity', 'configuration', 'location'])
def test_route_activation_rejects_wrong_kind_or_canonical_route_identity_without_new_dispatch(env, mismatch):
    request, route = completed_route(env)
    previous = len(env.provider.calls), len(store_facts(env.store, request['turn_id'])[0])
    request, route = deepcopy(request), deepcopy(route)
    if mismatch == 'kind':
        request['desired_outcome'] = 'project.answer'
    elif mismatch == 'ref':
        route['payload_ref'] = env.store.get_immutable_payload(request['turn_id'], CHOICE_KIND)[0]
    elif mismatch == 'revision':
        route['revision'] = 'a' * 64
    elif mismatch == 'cache_identity':
        route['prompt_cache_scope_identity'] = 'b' * 64
    elif mismatch == 'configuration':
        route['configuration']['model'] = 'wrong'
    else:
        route['execution_location'] = 'remote'
    with pytest.raises(ModelConfigurationError, match='provider_store_binding_invalid'):
        frozen_activation(env, request, route)
    assert (len(env.provider.calls), len(store_facts(env.store, request['turn_id'])[0])) == previous


@pytest.mark.parametrize('drift', ['store', 'fast', 'generation', 'mode', 'adapter'])
def test_route_activation_rejects_current_selection_drift_without_new_dispatch(env, drift):
    request, route = completed_route(env)
    previous = len(env.provider.calls), len(store_facts(env.store, request['turn_id'])[0])
    if drift == 'store':
        toggle(env, False)
    elif drift == 'fast':
        choose(env.models, 'other', 1)
    elif drift == 'generation':
        env.models.update('generation', {'base_url': 'https://synthetic.invalid/v1',
            'model': 'other', 'expected_revision': 1})
    elif drift == 'mode':
        env.models.update_generation_mode(mode='api', local_enabled=False,
            local_base_url='http://127.0.0.1:8001/local-model/v1', expected_revision=1)
    else:
        env.models._completion_fn = ResponsesCompletion(api_base=env.provider.base)
    with pytest.raises(ModelConfigurationError):
        frozen_activation(env, request, route)
    assert (len(env.provider.calls), len(store_facts(env.store, request['turn_id'])[0])) == previous


@pytest.mark.parametrize('historical', [False, True])
def test_off_route_replay_never_adopts_a_later_background_choice(env, historical):
    result = env.route.route_model(TEXT, project_id='alpha', request_key='off')
    assert result.mode == 'model'
    row = env.records.read(BINDINGS, result.turn_id)
    assert row.payload['enabled'] is False
    if historical:
        with env.records.begin() as tx:
            tx.delete(BINDINGS, row.object_id, expected_revision=row.revision)
            tx.commit()
    toggle(env)
    assert env.route.route_model(TEXT, project_id='alpha', request_key='off') == result
    assert len(env.provider.calls) == 1
    assert env.provider.calls[0]['body'].get('background') is None
    assert env.provider.calls[0]['body'].get('store') is None
    assert env.records.read(BINDINGS, result.turn_id) == (None if historical else row)
    assert env.clients and all(client.is_closed for client in env.clients)


def test_route_does_not_infer_background_capability_from_model_or_saved_toggle(env):
    toggle(env)
    env.models._completion_fn = ResponsesCompletion(api_base=env.provider.base)
    result = env.route.route_model(TEXT, project_id='alpha', request_key='undeclared')
    assert result.mode == 'model' and result.usage == {'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7}
    row = env.records.read(BINDINGS, result.turn_id)
    assert row.payload['enabled'] is False
    assert all(row.payload[key] is None for key in ('parent', 'auxiliary', 'configuration', 'adapter'))
    assert len(env.provider.calls) == 1 and env.provider.calls[0]['method'] == 'POST'
    assert 'background' not in env.provider.calls[0]['body'] and 'store' not in env.provider.calls[0]['body']
    assert env.store.get_run_lease(result.turn_id) is None
    dispatches, terminals, checkpoints, effects = store_facts(env.store, result.turn_id)
    assert len(dispatches) == len(terminals) == 1 and checkpoints == []
    assert terminals[0]['status'] == 'succeeded' and effects[dispatches[0]['attempt_id']] == 'SETTLED_OK'
    assert env.clients and all(client.is_closed for client in env.clients)


@pytest.mark.parametrize('revocation', ['fast', 'store', 'generation', 'project'])
def test_route_cursor_get_rechecks_actual_frozen_selection_and_project_egress(env, revocation):
    from backend.memory_app.v2.privacy import set_private_project
    toggle(env)
    choose(env.models, 'quick')

    def revoke():
        identity = env.records.list(BINDINGS)[0].object_id
        deadline = monotonic() + 2
        while monotonic() < deadline and not store_facts(env.store, identity)[2]:
            sleep(.01)
        assert store_facts(env.store, identity)[2]
        request = env.store.get_request(identity)
        assert request['scope']['project_id'] == 'alpha' and request['privacy']['allow_remote'] is True
        assert json.loads(request['input']['text'])['original_text'] == TEXT
        if revocation == 'fast':
            choose(env.models, 'other', 1)
        elif revocation == 'store':
            toggle(env, False)
        elif revocation == 'generation':
            env.models.update('generation', {'base_url': 'https://synthetic.invalid/v1',
                'model': 'other', 'expected_revision': 1})
        else:
            set_private_project(env.records, 'alpha', True, 0)

    env.provider.after_emitted = revoke
    result = env.route.route_model(TEXT, project_id='alpha', request_key='revoke')
    assert env.provider.callback_errors == []
    assert result.mode == 'rules' and result.reason == 'model_route_failed'
    posts = [call for call in env.provider.calls if call['method'] == 'POST']
    assert len(posts) == 1 and posts[0]['body']['model'] == 'quick'
    assert posts[0]['body']['background'] is True and posts[0]['body']['store'] is True
    assert not any(call['method'] == 'GET' for call in env.provider.calls)
    dispatches, terminals, checkpoints, effects = store_facts(env.store, result.turn_id)
    assert len(dispatches) == len(terminals) == 1 and checkpoints
    assert terminals[0]['status'] != 'succeeded' and terminals[0]['usage'] is None
    assert effects[dispatches[0]['attempt_id']] == 'UNKNOWN'
    assert env.store.get_immutable_payload(result.turn_id, 'workbench-route-output-v1') is None
    assert env.clients and all(client.is_closed for client in env.clients)
    assert env.responses and all(response.is_closed for response in env.responses)


@pytest.mark.parametrize('failure', ['observer', 'close'])
def test_route_derived_checkpoint_or_owned_close_failure_never_repeats_create(env, failure):
    toggle(env)
    if failure == 'observer':
        with sqlite3.connect(env.store._path) as connection:
            connection.execute("CREATE TRIGGER synthetic_route_observer BEFORE INSERT ON ai_turn_immutable_payloads "
                "WHEN NEW.kind LIKE 'model-provider-checkpoint-%' BEGIN SELECT RAISE(ABORT,'synthetic route'); END")
    else:
        env.provider.fail_close = True
    result = env.route.route_model(TEXT, project_id='alpha', request_key='fault')
    assert result.mode == 'rules' and result.reason == 'model_route_failed'
    assert len(env.provider.calls) == 1 and env.provider.calls[0]['method'] == 'POST'
    assert env.provider.calls[0]['body']['background'] is True
    dispatches, terminals, checkpoints, effects = store_facts(env.store, result.turn_id)
    assert len(dispatches) == len(terminals) == 1
    assert terminals[0]['status'] != 'succeeded' and terminals[0]['usage'] is None
    assert effects[dispatches[0]['attempt_id']] == 'UNKNOWN'
    if failure == 'observer':
        assert checkpoints == []
    assert env.store.get_immutable_payload(result.turn_id, 'workbench-route-output-v1') is None
    assert env.clients and all(client.is_closed for client in env.clients)
    assert env.responses and all(response.is_closed for response in env.responses)


def test_two_route_instances_preserve_the_actual_busy_owner_and_dispatch_once(env):
    toggle(env)
    entered, release = Event(), Event()

    def hold():
        identity = env.records.list(BINDINGS)[0].object_id
        deadline = monotonic() + 2
        while monotonic() < deadline and not store_facts(env.store, identity)[2]:
            sleep(.01)
        assert store_facts(env.store, identity)[2]
        entered.set()
        assert release.wait(2)

    env.provider.after_emitted = hold
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(env.route.route_model, TEXT, project_id='alpha', request_key='concurrent')
        assert entered.wait(4)
        try:
            identity = env.records.list(BINDINGS)[0].object_id
            before = env.store.get_run_lease(identity)
            assert before.status == 'active' and before.token.owner_id.startswith('route-')
            now = datetime.now(timezone.utc)
            assert env.store.try_acquire_run_lease(identity, 'independent-contender', now=now,
                stale_after=now + timedelta(seconds=1)) is None
            second = RouteService(env.records, env.models).route_model(TEXT, project_id='alpha', request_key='concurrent')
            assert second.mode == 'rules' and second.reason == 'prior_result_unavailable'
            assert second.turn_id == identity and env.store.get_run_lease(identity) == before
            assert len(env.provider.calls) == 1
        finally:
            release.set()
        result = future.result(timeout=5)
    assert env.provider.callback_errors == [] and result.mode == 'model'
    assert len([call for call in env.provider.calls if call['method'] == 'POST']) == 1
    assert len([call for call in env.provider.calls if call['method'] == 'GET']) == 1
    assert env.store.get_run_lease(identity) is None
    dispatches, terminals, _checkpoints, effects = store_facts(env.store, identity)
    assert len(dispatches) == len(terminals) == 1 and terminals[0]['status'] == 'succeeded'
    assert effects[dispatches[0]['attempt_id']] == 'SETTLED_OK'


def test_background_route_never_upgrades_the_original_four_second_budget(env):
    toggle(env)
    env.provider.after_emitted = lambda: sleep(4.05)
    started = monotonic()
    result = env.route.route_model(TEXT, project_id='alpha', request_key='deadline')
    elapsed = monotonic() - started
    assert result.mode == 'rules' and result.reason == 'model_route_failed'
    assert env.store.get_immutable_payload(result.turn_id, 'workbench-route-output-v1') is None
    assert len(env.provider.calls) == 1 and env.provider.calls[0]['method'] == 'POST'
    assert env.provider.calls[0]['body']['background'] is True
    assert env.store.get_request(result.turn_id)['execution_policy']['budget']['planner_timeout_ms'] == 4000
    print('ROUTE_DEADLINE elapsed_seconds=', round(elapsed, 3))
    assert env.clients and all(client.is_closed for client in env.clients)
