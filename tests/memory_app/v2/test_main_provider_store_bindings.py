"""Main selection uses real product routing, SQLite and an external HTTP provider."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
from threading import Barrier, Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.app import create_app
from backend.memory_app.kernel.product_routing import ProductGenerationRouting
from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from backend.memory_app.storage_authority import resolve_recognition_document_store
from backend.memory_app.turn_routing import SNAPSHOT_KIND
from backend.memory_app.v2.provider_store_settings import ProviderStoreSettings
from backend.memory_app.v2.turn_requests import freeze_product_turn
from backend.recognition import RecognitionService, WorkScope
from backend.security.secrets import InMemorySecretStore
from backend.shared.llm.model_capabilities import ModelCapabilities
from backend.shared.llm.openai_responses import ResponsesCompletion
from core.ai_kernel import ScopedCapabilityRegistry, SynchronousAIRuntime, validate_turn_request
from core.ai_kernel.turn_kinds import freeze_turn_request
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.storage_provider import SQLiteStructuredRecordStore
from tests.backend.unit.llm.test_provider_store_capability import api_mode
from tests.memory_app.test_fast_generation import choose
from tests.memory_app.v2.test_provider_store_bindings import BINDINGS, FIELDS, toggle


class MainProvider:
    def __init__(self):
        self.bodies = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.bodies.append(body)
                messages = body['input']
                text = '\n'.join(message['content'] for message in messages)
                try:
                    context = json.loads(messages[-1]['content'])
                except (ValueError, TypeError):
                    context = {}
                if 'output' in context:
                    output = {'mode': 'cluster', 'assignments': [{'profile_id': 'subagent.worker',
                        'task': 'Synthetic child', 'goal': 'Synthetic goal', 'deliverable': '整理稿',
                        'capabilities': ['document.draft.propose'], 'depends_on': []}]}
                elif 'decision_contract' in context:
                    output = {'type': 'complete', 'summary': 'Synthetic completed work'}
                elif 'condensed_question' in text:
                    output = {'condensed_question': 'alpha beta gamma?'}
                elif '"queries"' in text:
                    output = {'queries': ['alpha beta gamma?']}
                else:
                    output = {'answer': 'Synthetic answer',
                        'citations': [int(number) for number in re.findall(r'^\[(\d+)\]', text, re.M)]}
                raw = json.dumps(output, ensure_ascii=False)
                response_id = ('resp_main_' + str(len(owner.bodies))) if body.get('background') is True else 'resp_main'
                response = {'id': response_id, 'status': 'completed', 'output': [{'type': 'message',
                    'content': [{'type': 'output_text', 'text': raw}]}],
                    'usage': {'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7}}
                if body.get('background') is True:
                    events = [{'type': 'response.created', 'sequence_number': 0,
                        'response': {'id': response_id, 'status': 'in_progress'}},
                        {'type': 'response.output_text.delta', 'sequence_number': 1, 'delta': raw},
                        {'type': 'response.completed', 'sequence_number': 2, 'response': response}]
                    payload = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
                elif body.get('stream'):
                    events = [{'type': 'response.output_text.delta', 'delta': raw},
                        {'type': 'response.completed', 'response': response}]
                    payload = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
                else:
                    payload = json.dumps(response).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

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
    import litellm
    provider = MainProvider()
    records, _namespace = resolve_recognition_document_store(tmp_path)
    native = ResponsesCompletion(api_base=provider.base, capabilities=ModelCapabilities(background_resume=True))
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=native)
    models.update('generation', {'base_url': provider.base, 'model': 'main', 'api_key': 'synthetic-only',
        'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    api_mode(models)
    store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    try:
        yield SimpleNamespace(root=tmp_path, records=records, models=models, provider=provider, native=native,
            settings=ProviderStoreSettings(records, models), store=store)
    finally:
        provider.close()


def request(env, identity='turn-main'):
    value = freeze_product_turn('project.answer', records=env.records, models=env.models,
        project_id='alpha', load_text=lambda _item: '', text='alpha beta gamma?',
        turn_id=identity, session_id='session-' + identity, operation_id='answer-' + identity,
        idempotency_key='answer-' + identity, created_at='2026-10-06T00:00:00Z',
        capabilities=['workbench.answer.execute'], template_version=2)
    value['context_policy']['include_memory'] = False
    value['capability_request'] = {'mode': 'execute_exact_v1',
        'capability_id': 'workbench.answer.execute', 'arguments': {'query': value['input']['text']}}
    value = validate_turn_request(value)
    env.store.claim_turn(value)
    return value


def routing(env, store=None):
    return ProductGenerationRouting(env.models, store or env.store, records=env.records, answer_owner=True)


def acquire(owner, value):
    return owner.acquire(request=value, project_id=value['scope']['project_id'],
        project_profile_id='project-profile', project_profile_revision=1,
        boundary_profile_id='boundary-profile', boundary_profile_revision=1,
        capability_ids=['workbench.answer.execute'])


def validate(env, value, route):
    from backend.memory_app.kernel.provider_store_binding import validate_main_provider_store_binding
    with env.records.begin() as tx:
        return validate_main_provider_store_binding(env.models, tx, value, route.payload)


def binding(env, value, route, enabled=True):
    row = env.records.read(BINDINGS, value['turn_id'])
    assert row is not None and row.revision == 1
    assert set(row.payload) == FIELDS and row.payload['auxiliary'] is None
    assert row.payload['enabled'] is enabled
    if enabled:
        assert row.payload['configuration'] == route.payload['configuration']
        assert row.payload['configuration']['model'] == 'main'
        assert row.payload['parent'] == env.models.provider_store_capability()['binding']
    else:
        assert all(row.payload[key] is None for key in ('parent', 'auxiliary', 'configuration', 'adapter'))
    assert validate(env, value, route) is enabled
    assert 'synthetic-only' not in json.dumps(row.payload) and 'secret_ref' not in json.dumps(row.payload)
    return row


@pytest.mark.parametrize('fast', [None, 'quick'])
def test_true_sqlite_creator_main_never_selects_auxiliary_model(env, fast):
    if fast:
        choose(env.models, fast)
    toggle(env)
    value = request(env)
    route = acquire(routing(env), value)
    assert env.store.get_immutable_payload(value['turn_id'], SNAPSHOT_KIND) == (route.payload_ref, route.payload)
    binding(env, value, route)
    aux = env.store.get_immutable_payload(value['turn_id'], 'product-aux-configuration-v1')[1]
    assert set(aux) == {'model', 'selection_revision', 'parent'} and aux['model'] == fast
    assert env.provider.bodies == []


def test_two_sqlite_instances_keep_one_created_choice_and_actual_ref(env):
    toggle(env)
    value = request(env)
    barrier = Barrier(2)
    def run(_index):
        owner = routing(env, SQLiteAITurnStore(env.root / 'turns.sqlite3'))
        barrier.wait()
        return acquire(owner, value)
    with ThreadPoolExecutor(max_workers=2) as pool:
        routes = tuple(pool.map(run, range(2)))
    assert routes[0] == routes[1]
    assert env.store.get_immutable_payload(value['turn_id'], SNAPSHOT_KIND) == (routes[0].payload_ref, routes[0].payload)
    assert len(env.records.list(BINDINGS)) == 1
    binding(env, value, routes[0])
    assert env.provider.bodies == []


@pytest.mark.parametrize('historical', [False, True])
def test_missing_and_first_off_history_never_adopt_current_toggle(env, historical):
    value = request(env)
    owner = ProductGenerationRouting(env.models, env.store) if historical else routing(env)
    first = acquire(owner, value)
    before = env.records.read(BINDINGS, value['turn_id'])
    if not historical:
        binding(env, value, first, False)
    toggle(env)
    replay = acquire(routing(env), value)
    assert replay == first and env.records.read(BINDINGS, value['turn_id']) == before
    assert validate(env, value, replay) is False and env.provider.bodies == []


@pytest.mark.parametrize('drift', ['selection', 'mode', 'configuration', 'adapter'])
def test_current_main_binding_drift_refuses_without_wire_or_rewrite(env, drift):
    toggle(env)
    value = request(env)
    route = acquire(routing(env), value)
    before = binding(env, value, route)
    if drift == 'selection':
        toggle(env, False)
    elif drift == 'mode':
        original = env.records.read('recognition_generation_mode', 'default')
        with env.records.begin() as tx:
            tx.put('recognition_generation_mode', 'default', original.payload,
                expected_revision=original.revision)
            tx.commit()
    elif drift == 'configuration':
        env.models.update('generation', {'model': 'changed', 'base_url': 'https://synthetic.invalid/v1',
            'expected_revision': 1})
    else:
        env.models._completion_fn = ResponsesCompletion(api_base=env.provider.base)
    with pytest.raises(ModelConfigurationError, match='provider_store_binding_changed'):
        validate(env, value, route)
    assert env.records.read(BINDINGS, value['turn_id']) == before and env.provider.bodies == []


@pytest.mark.parametrize('corrupt', ['revision', 'bool', 'auxiliary', 'model', 'adapter', 'scope'])
def test_corrupt_main_choice_is_not_treated_as_missing_off(env, corrupt):
    toggle(env)
    value = request(env)
    route = acquire(routing(env), value)
    row = binding(env, value, route)
    payload = deepcopy(row.payload)
    if corrupt == 'bool':
        payload['enabled'] = 1
    elif corrupt == 'auxiliary':
        payload['auxiliary'] = {'model': 'quick'}
    elif corrupt == 'model':
        payload['configuration']['model'] = 'quick'
    elif corrupt == 'adapter':
        payload['adapter']['background_resume'] = 1
    elif corrupt == 'scope':
        payload['project_id'] = 'other'
    with sqlite3.connect(env.records.database_path) as connection:
        connection.execute('UPDATE crp_structured_records SET revision=?,payload_json=? WHERE collection=? AND object_id=?',
            (2 if corrupt == 'revision' else 1, json.dumps(payload), BINDINGS, value['turn_id']))
    with pytest.raises(ModelConfigurationError, match='provider_store_binding_invalid'):
        validate(env, value, route)
    assert env.provider.bodies == []


def test_binding_sql_abort_retains_original_route_and_never_backfills(env):
    toggle(env)
    value = request(env)
    with sqlite3.connect(env.records.database_path) as connection:
        connection.execute("CREATE TRIGGER reject_main_binding BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection='v2_provider_store_bindings' BEGIN SELECT RAISE(ABORT,'main binding'); END")
    with pytest.raises(sqlite3.IntegrityError, match='main binding'):
        acquire(routing(env), value)
    saved = env.store.get_immutable_payload(value['turn_id'], SNAPSHOT_KIND)
    assert saved is not None and env.records.read(BINDINGS, value['turn_id']) is None
    with sqlite3.connect(env.records.database_path) as connection:
        connection.execute('DROP TRIGGER reject_main_binding')
    replay = acquire(routing(env), value)
    assert (replay.payload_ref, replay.payload) == saved
    assert env.records.read(BINDINGS, value['turn_id']) is None and validate(env, value, replay) is False
    assert env.provider.bodies == []


def test_different_payload_remains_original_immutable_conflict(env):
    value = request(env)
    first = acquire(routing(env), value)
    env.models.update('generation', {'model': 'changed', 'base_url': 'https://synthetic.invalid/v1', 'expected_revision': 1})
    with pytest.raises(ValueError, match='immutable payload identity conflict'):
        acquire(routing(env), value)
    assert env.store.get_immutable_payload(value['turn_id'], SNAPSHOT_KIND) == (first.payload_ref, first.payload)
    assert env.provider.bodies == []


def test_aux_freeze_releases_records_before_model_lock(env):
    toggle(env)
    value = request(env)
    captured, locked, written = Event(), Event(), Event()
    original = env.models.provider_store_adapter
    def observe(*, reader=None):
        adapter = original(reader=reader)
        captured.set()
        assert locked.wait(3)
        return adapter
    env.models.provider_store_adapter = observe
    def update():
        assert captured.wait(3)
        with env.models._lock:
            locked.set()
            with env.records.begin() as tx:
                tx.put('synthetic_lock_probe', 'probe', {'observed': True}, expected_revision=0)
                tx.commit()
        written.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        updater = pool.submit(update)
        result = pool.submit(acquire, routing(env), value)
        route = result.result(timeout=8)
        updater.result(timeout=8)
    assert written.is_set() and env.records.read('synthetic_lock_probe', 'probe') is not None
    env.models.provider_store_adapter = original
    binding(env, value, route)


@pytest.mark.parametrize('untrusted', ['answer-owner', 'task-without-binding', 'task-role-string'])
def test_direct_routing_without_real_main_owner_never_freezes_selection(env, untrusted):
    toggle(env)
    value = request(env)
    if untrusted != 'answer-owner':
        value = deepcopy(value)
        value['desired_outcome'] = 'project.task'
        if untrusted == 'task-role-string':
            value['agent_binding'] = {'role': 'main'}  # Untrusted input, no registered Run/verifier.
    route = acquire(ProductGenerationRouting(env.models, env.store, records=env.records), value)
    assert env.store.get_immutable_payload(value['turn_id'], SNAPSHOT_KIND) == (route.payload_ref, route.payload)
    assert env.records.read(BINDINGS, value['turn_id']) is None and env.provider.bodies == []


@pytest.mark.parametrize('unsupported', ['undeclared', 'local', 'subscription', 'proxy'])
def test_first_unsupported_adapter_remains_off_without_switching_transport(env, unsupported):
    toggle(env)
    if unsupported == 'undeclared':
        env.models._completion_fn = ResponsesCompletion(api_base=env.provider.base)
    elif unsupported == 'local':
        mode = env.records.read('recognition_generation_mode', 'default')
        with env.records.begin() as tx:
            tx.put('recognition_generation_mode', 'default', {**mode.payload, 'mode': 'local'},
                expected_revision=mode.revision)
            tx.commit()
    elif unsupported == 'subscription':
        mode = env.records.read('recognition_generation_mode', 'default')
        with env.records.begin() as tx:
            tx.put('v2_subscription_selection', 'default', {'model': 'synthetic-subscription',
                'mode_revision': mode.revision, 'account_revision': 0}, expected_revision=0)
            tx.commit()
    else:
        env.models.update('generation', {'base_url': 'https://synthetic-proxy.invalid/v1', 'expected_revision': 1})
    loader, subscription = env.models._completion_fn, env.models._responses
    value = request(env)
    route = acquire(routing(env), value)
    binding(env, value, route, False)
    assert env.models._completion_fn is loader and env.models._responses is subscription
    assert env.provider.bodies == []


def test_legacy_governed_fixture_consumes_retry_observer_with_real_sqlite_attempt(tmp_path):
    from backend.memory_app.structured_generation import AskOutput
    from tests.memory_app.v2.test_workbench_ask import Model
    models = Model()
    store = SQLiteAITurnStore(tmp_path / 'fixture-turns.sqlite3')
    outputs, notifications = [], []
    def observe(value):
        notifications.append(value)
        raise AssertionError('A successful synthetic wire has no retry notification')
    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control):
            route = ProductGenerationRouting(models, store).acquire(request=request, project_id='alpha',
                project_profile_id='fixture', project_profile_revision=1,
                boundary_profile_id='fixture', boundary_profile_revision=1, capability_ids=[])
            outputs.append(models.complete_governed([{'role': 'user', 'content': 'Synthetic fixture request'}],
                routing_snapshot=route.generation_binding(), execution_control=execution_control,
                metadata_sink=execution_control, wire_attempt_sink=execution_control,
                response_model=AskOutput, max_tokens=200, validate_current=lambda: None, on_retry=observe))
            return {'type': 'complete', 'summary': 'Fixture completed'}
    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store)
    value = freeze_turn_request('project.answer', turn_id='turn-fixture-observer', session_id='fixture-session',
        operation_id='fixture-operation', idempotency_key='fixture-observer', project_id='alpha',
        created_at='2026-10-06T00:00:00Z', text='Synthetic fixture request', capabilities=[],
        privacy={'mode': 'remote_allowed', 'allow_remote': True, 'pii': 'possible',
            'consent_refs': ['crp://default/model-settings/generation'], 'retention': 'session'})
    receipt = runtime.submit_turn(value)
    assert receipt.status == 'completed' and models.calls == 1 and notifications == []
    assert outputs[0][0].model_dump() == {'answer': 'Synthetic answer', 'citations': []}
    assert outputs[0][1]['usage'] == {'total_tokens': 7}
    events = runtime.events_after(value['turn_id'])
    assert sum(event['type'] == 'model.attempt.dispatched' for event in events) == 1
    terminals = [store.get(event['data']['receipt_ref']) for event in events
        if event['type'] == 'model.attempt.terminal']
    assert len(terminals) == 1 and terminals[0]['status'] == 'succeeded'


@pytest.mark.parametrize('intent', ['ask', 'do'])
def test_real_http_main_and_worker_identity_freeze_without_enabling_background(env, intent):
    (env.root / 'config').mkdir(exist_ok=True)
    (env.root / 'config' / 'settings.toml').write_bytes((Path(__file__).parents[3] / 'config/settings.toml.example').read_bytes())
    toggle(env)
    choose(env.models, 'quick')
    application = create_app(runtime_root=env.root, legacy_app=FastAPI(), model_configuration=env.models)
    with TestClient(application) as http:
        if intent == 'ask':
            service = application.state.recognition_service
            scope = WorkScope('local-user', 'project-a')
            experience = service.stage_experience(scope=scope, content='Synthetic evidence')
            candidate = service.propose(scope=scope, content='alpha beta gamma', source_experience_ids=[experience])
            service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer='local-user')
        response = http.post('/api/v2/workbench/turns', json={'project_id': 'project-a', 'intent': intent,
            'text': 'alpha beta gamma?' if intent == 'ask' else '写一段总结'})
        assert response.status_code == 200, response.text
        data = response.json()
        store = application.state.ai_turn_store
        identity = data['turn']['id'] if intent == 'ask' else data['turn']['receipt']['do']['kernel_turn_id']
        if intent == 'do':
            deadline = monotonic() + 25
            while monotonic() < deadline:
                receipt = http.get(f"/api/v2/workbench/threads/{data['thread_id']}?project_id=project-a").json()['turns'][0]['receipt']['do']
                if receipt['state'] in {'done', 'failed', 'partial'}:
                    break
                sleep(.05)
            assert receipt['state'] == 'done', receipt
        frozen = store.get_request(identity)
        saved = store.get_immutable_payload(identity, SNAPSHOT_KIND)
        row = env.records.read(BINDINGS, identity)
        assert row is not None and row.payload['enabled'] is True and row.payload['auxiliary'] is None
        assert row.payload['configuration'] == saved[1]['configuration'] and row.payload['configuration']['model'] == 'main'
        if intent == 'ask':
            assert 'agent_binding' not in frozen
        else:
            assert frozen['agent_binding']['role'] == 'main'
            runs = application.state.agent_runtime_composition.store.list_runs(project_id='project-a')
            children = [run for run in runs if run.role == 'subagent']
            assert children
            for run in children:
                child_binding = env.records.read(BINDINGS, run.turn_id)
                if run.profile_id == 'steward.scheduler':
                    assert child_binding.payload['enabled'] is True and child_binding.payload['auxiliary'] is None
                    assert child_binding.payload['configuration'] == saved[1]['configuration']
                    assert child_binding.payload['configuration']['model'] == 'main'
                else:
                    assert child_binding is None
        context_event = next(event for event in store.events_after(identity) if event['type'] == 'context.resolved')
        context = store.get(context_event['data']['payload_ref'])
        manifest = store.get(context['capability_manifest_ref'])
        assert manifest['model_routing_snapshot_ref'] == saved[0]
        entry = next(item for item in context['entries'] if item['kind'] == 'model_routing_snapshot')
        assert entry['payload_ref'] == saved[0]
        assert env.provider.bodies
        if intent == 'ask':
            assert len(env.provider.bodies) == 1
            assert env.provider.bodies[0]['model'] == 'main'
            assert env.provider.bodies[0]['store'] is True and env.provider.bodies[0]['background'] is True
        else:
            owners = []
            for body in env.provider.bodies:
                context = json.loads(body['input'][-1]['content'])
                name = ('steward' if 'output' in context else
                    'main' if context.get('role', {}).get('organization_role') == '主政协调' else 'worker')
                owners.append(name)
                if name in {'steward', 'main'}:
                    assert body['store'] is True and body['background'] is True
                else:
                    assert 'store' not in body and 'background' not in body
            assert owners == ['steward', 'worker', 'main']
