"""First auxiliary selection is staged by the real product transaction owners."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
from threading import Barrier, Thread
from types import SimpleNamespace

import pytest

from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from backend.memory_app.kernel.aux_routing import CHOICES
from backend.memory_app.v2.memory_turn import MemoryTurn
from backend.memory_app.v2.provider_store_settings import ProviderStoreSettings
from backend.memory_app.v2.route import RouteService
from backend.recognition import RecognitionService, WorkScope
from backend.security.secrets import InMemorySecretStore
from backend.shared.llm.model_capabilities import ModelCapabilities
from backend.shared.llm.openai_responses import ResponsesCompletion
from core.storage_provider import SQLiteStructuredRecordStore
from tests.backend.unit.llm.test_provider_store_capability import api_mode
from tests.memory_app.test_fast_generation import choose
from tests.memory_app.v2.test_route import TEXT, good_output


BINDINGS = 'v2_provider_store_bindings'
FIELDS = {'schema_version', 'turn_id', 'project_id', 'kind', 'enabled',
    'selection_revision', 'parent', 'auxiliary', 'configuration', 'adapter'}


class RouteProvider:
    """Only the external HTTP provider is synthetic; all product owners are real."""
    def __init__(self):
        self.bodies = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.bodies.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                text = json.dumps(good_output(), ensure_ascii=False)
                if owner.bodies[-1].get('background') is True:
                    events = [{'type': 'response.created', 'sequence_number': 0,
                        'response': {'id': 'resp_binding', 'status': 'in_progress'}},
                        {'type': 'response.output_text.delta', 'sequence_number': 1, 'delta': text},
                        {'type': 'response.completed', 'sequence_number': 2,
                            'response': {'id': 'resp_binding', 'status': 'completed',
                                'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}],
                                'usage': {'input_tokens': 2, 'output_tokens': 3, 'total_tokens': 5}}}]
                else:
                    events = [{'type': 'response.output_text.delta', 'delta': text},
                    {'type': 'response.completed', 'response': {'id': 'resp_binding',
                        'status': 'completed', 'output': [{'type': 'message', 'content': [
                            {'type': 'output_text', 'text': text}]}],
                        'usage': {'input_tokens': 2, 'output_tokens': 3, 'total_tokens': 5}}}]
                raw = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}/v1'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@pytest.fixture
def env(tmp_path):
    import litellm  # The existing route deadline includes execution, not dependency installation.
    provider = RouteProvider()
    native = ResponsesCompletion(api_base=provider.base,
        capabilities=ModelCapabilities(background_resume=True))
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=native)
    models.update('generation', {'base_url': provider.base, 'model': 'main',
        'api_key': 'synthetic-only', 'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    api_mode(models)
    service = RecognitionService(records)
    source = service.stage_experience(scope=WorkScope('local-user', 'alpha'), content='Synthetic binding source')
    material = {'type': 'experience', 'id': source, 'project_id': 'alpha',
        'revision': records.read('recognition_experiences', source).revision}
    try:
        yield SimpleNamespace(records=records, models=models, provider=provider, native=native,
            settings=ProviderStoreSettings(records, models), material=material,
            route=RouteService(records, models))
    finally:
        provider.close()


def toggle(env, enabled=True):
    view = env.settings.get()
    return env.settings.update(enabled=enabled, expected_revision=view['revision'],
        expected_generation_revision=view['generation_revision'], expected_mode_revision=view['mode_revision'])


def memory(env, key='memory'):
    return MemoryTurn(env.records, env.models, kind='memory.overview', project='alpha', key=key,
        materials=[env.material], validate=lambda: None)


def validate(env, request):
    from backend.memory_app.kernel.provider_store_binding import validate_provider_store_binding
    with env.records.begin() as tx:
        return validate_provider_store_binding(env.models, tx, request)


def assert_binding(env, request, *, enabled, model=None):
    row = env.records.read(BINDINGS, request['turn_id'])
    assert row is not None and row.revision == 1
    value = row.payload
    assert set(value) == FIELDS
    assert {key: value[key] for key in ('schema_version', 'turn_id', 'project_id', 'kind', 'enabled')} == {
        'schema_version': '1.0.0', 'turn_id': request['turn_id'], 'project_id': 'alpha',
        'kind': request['desired_outcome'], 'enabled': enabled}
    assert value['selection_revision'] == env.settings.get()['revision']
    if enabled:
        choice = env.records.read(CHOICES, request['turn_id'])
        assert choice.revision == 1 and value['auxiliary'] == choice.payload
        assert value['parent'] == env.models.provider_store_capability()['binding']
        assert value['configuration'] == {**value['parent']['configuration'], 'model': model}
        assert value['adapter'] == {'kind': 'openai-responses', 'api_base': env.provider.base,
            'background_resume': True}
    else:
        assert all(value[key] is None for key in ('parent', 'auxiliary', 'configuration', 'adapter'))
    assert validate(env, request) is enabled
    assert 'synthetic-only' not in json.dumps(value) and 'secret_ref' not in json.dumps(value)
    assert not any(key.startswith('provider_') for key in request)
    return row


@pytest.mark.parametrize('fast', [None, 'quick'])
def test_real_memory_first_tx_freezes_actual_aux_configuration_without_dispatch(env, fast):
    if fast:
        choose(env.models, fast)
    toggle(env)
    original = env.models.public(), env.models.snapshot('generation')
    turn = memory(env)
    assert_binding(env, turn.request, enabled=True, model=fast or 'main')
    assert env.provider.bodies == [] and tuple(turn.store.events_after(turn.turn_id)) == ()
    assert (env.models.public(), env.models.snapshot('generation')) == original
    assert set(env.records.read('v2_memory_turn_keys', turn.turn_id).payload) == {'identity', 'request'}


@pytest.mark.parametrize('fast', [None, 'quick'])
def test_real_route_first_tx_freezes_choice_and_enables_declared_background_wire(env, fast):
    if fast:
        choose(env.models, fast)
    toggle(env)
    result = env.route.route_model(TEXT, project_id='alpha', request_key='route')
    assert result.mode == 'model', {'reason': result.reason, 'provider_calls': len(env.provider.bodies),
        'request_fields': [sorted(body) for body in env.provider.bodies]}
    request = env.route.store.get_request(result.turn_id)
    assert_binding(env, request, enabled=True, model=fast or 'main')
    assert len(env.provider.bodies) == 1 and env.provider.bodies[0]['model'] == (fast or 'main')
    assert env.provider.bodies[0]['background'] is True and env.provider.bodies[0]['store'] is True
    indexed = env.records.list('v2_route_turn_keys')
    assert len(indexed) == 1 and set(indexed[0].payload) == {'identity', 'inputs', 'request', 'configuration'}
    assert env.route.route_model(TEXT, project_id='alpha', request_key='route') == result
    assert len(env.provider.bodies) == 1 and env.records.read(BINDINGS, result.turn_id).revision == 1


@pytest.mark.parametrize('historical', [False, True])
@pytest.mark.parametrize('owner', ['memory', 'route'])
def test_off_and_missing_history_never_adopt_later_selection(env, owner, historical):
    turn = memory(env) if owner == 'memory' else env.route.route_model(TEXT, project_id='alpha', request_key='off')
    request = turn.request if owner == 'memory' else env.route.store.get_request(turn.turn_id)
    row = assert_binding(env, request, enabled=False)
    if historical:
        with env.records.begin() as tx:
            tx.delete(BINDINGS, row.object_id, expected_revision=row.revision)
            tx.commit()
    toggle(env)
    choose(env.models, 'later')
    replay = memory(env) if owner == 'memory' else env.route.route_model(TEXT, project_id='alpha', request_key='off')
    assert replay.turn_id == turn.turn_id
    assert (replay.request if owner == 'memory' else env.route.store.get_request(replay.turn_id)) == request
    assert env.records.read(BINDINGS, turn.turn_id) == (None if historical else row)
    assert validate(env, request) is False
    assert len(env.provider.bodies) == (0 if owner == 'memory' else 1)


@pytest.mark.parametrize('owner', ['memory', 'route'])
def test_same_key_concurrent_first_tx_keeps_one_winner_binding(env, owner):
    toggle(env)
    barrier = Barrier(2)

    def create(_index):
        barrier.wait(timeout=5)
        return memory(env, 'winner') if owner == 'memory' else env.route.route_model(
            TEXT, project_id='alpha', request_key='winner')

    with ThreadPoolExecutor(max_workers=2) as pool:
        turns = list(pool.map(create, range(2)))
    assert len({turn.turn_id for turn in turns}) == 1
    identity = turns[0].turn_id
    rows = env.records.list('v2_memory_turn_keys' if owner == 'memory' else 'v2_route_turn_keys')
    assert len(rows) == len(env.records.list(CHOICES)) == len(env.records.list(BINDINGS)) == 1
    request = rows[0].payload['request']
    assert request['turn_id'] == identity
    assert_binding(env, request, enabled=True, model='main')
    assert len(env.provider.bodies) == (0 if owner == 'memory' else 1)


@pytest.mark.parametrize('drift', ['selection', 'generation', 'mode', 'fast', 'adapter'])
def test_enabled_binding_rejects_current_drift_without_dispatch_or_rewriting(env, drift):
    choose(env.models, 'quick')
    toggle(env)
    turn = memory(env)
    frozen = assert_binding(env, turn.request, enabled=True, model='quick')
    if drift == 'selection':
        toggle(env, False)
    elif drift == 'generation':
        env.models.update('generation', {'base_url': 'https://synthetic.invalid/v1', 'model': 'changed',
            'api_key': 'synthetic-only', 'allow_remote': True, 'enabled': True, 'expected_revision': 1})
    elif drift == 'mode':
        env.models.update_generation_mode(mode='api', local_enabled=False,
            local_base_url='http://127.0.0.1:8001/local-model/v1', expected_revision=1)
    elif drift == 'fast':
        choose(env.models, 'different', 1)
    else:
        env.native.capabilities = ModelCapabilities(background_resume=False)
    with pytest.raises(ModelConfigurationError, match='provider_store_binding_changed'):
        validate(env, turn.request)
    assert env.records.read(BINDINGS, turn.turn_id) == frozen and env.provider.bodies == []


@pytest.mark.parametrize('corrupt', ['extra', 'bool', 'configuration', 'auxiliary', 'adapter', 'scope'])
def test_damaged_binding_is_never_treated_as_missing_off_history(env, corrupt):
    toggle(env)
    turn = memory(env)
    value = deepcopy(assert_binding(env, turn.request, enabled=True, model='main').payload)
    if corrupt == 'extra': value['unexpected'] = 'unsafe'
    elif corrupt == 'bool': value['enabled'] = 1
    elif corrupt == 'configuration': value['configuration']['revision'] = True
    elif corrupt == 'auxiliary': value['auxiliary']['selection_revision'] = True
    elif corrupt == 'adapter': value['adapter']['api_base'] = 'https://foreign.invalid/v1'
    else: value['project_id'] = 'beta'
    with sqlite3.connect(env.records.database_path) as connection:
        connection.execute('UPDATE crp_structured_records SET payload_json=? WHERE collection=? AND object_id=?',
            (json.dumps(value), BINDINGS, turn.turn_id))
    with pytest.raises(ModelConfigurationError, match='provider_store_binding_(invalid|changed)'):
        validate(env, turn.request)
    assert env.provider.bodies == []


@pytest.mark.parametrize('owner', ['memory', 'route'])
def test_actual_sql_abort_rolls_back_index_choice_and_binding_in_first_tx(env, owner):
    toggle(env)
    with sqlite3.connect(env.records.database_path) as connection:
        connection.execute("CREATE TRIGGER reject_binding BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection='v2_provider_store_bindings' BEGIN SELECT RAISE(ABORT, 'binding_abort'); END")
    if owner == 'memory':
        with pytest.raises(sqlite3.IntegrityError, match='binding_abort'):
            memory(env)
    else:
        result = env.route.route_model(TEXT, project_id='alpha', request_key='abort')
        assert result.mode == 'rules' and result.reason == 'freeze_failed'
    assert env.records.list('v2_memory_turn_keys') == env.records.list('v2_route_turn_keys') == ()
    assert env.records.list(CHOICES) == env.records.list(BINDINGS) == ()
    assert env.provider.bodies == [] and env.settings.get()['enabled'] is True


def test_old_synthetic_model_without_getters_freezes_only_explicit_off(env):
    from tests.memory_app.v2.test_insight_generation import Model
    models = Model()
    turn = MemoryTurn(env.records, models, kind='memory.overview', project='alpha', key='legacy-model',
        materials=[env.material], validate=lambda: None)
    row = env.records.read(BINDINGS, turn.turn_id)
    assert row is not None and set(row.payload) == FIELDS
    assert row.payload['enabled'] is False and row.payload['selection_revision'] == 0
    assert all(row.payload[key] is None for key in ('parent', 'auxiliary', 'configuration', 'adapter'))
    assert env.records.read(CHOICES, turn.turn_id) is None and models.calls == 0


def test_settings_capture_uses_passed_reader_and_does_not_begin_a_second_tx(env):
    toggle(env)
    with env.records.begin() as tx:
        capture = env.settings.capture(tx)
        assert capture['enabled'] is True and capture['selection_revision'] == 1
        assert capture['parent'] == env.models.provider_store_capability(reader=tx)['binding']
        tx.rollback()
    assert env.provider.bodies == []
