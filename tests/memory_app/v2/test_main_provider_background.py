"""Background selection crosses real product owners and physical model leases."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sqlite3
from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.app import create_app
from backend.memory_app.model_config import ModelConfiguration, ProviderStoreActivation
from backend.memory_app.storage_authority import resolve_recognition_document_store
from backend.memory_app.v2.provider_store_settings import ProviderStoreSettings
from backend.recognition import WorkScope
from backend.security.secrets import InMemorySecretStore
from backend.shared.llm.model_capabilities import ModelCapabilities
from backend.shared.llm.openai_responses import ResponsesCompletion
from tests.backend.unit.llm.test_provider_store_capability import api_mode
from tests.memory_app.test_fast_generation import choose
from tests.memory_app.v2.test_provider_store_bindings import BINDINGS, toggle


class ProductProvider:
    """An external local HTTP endpoint; all application owners remain real."""
    def __init__(self):
        self.calls, self.pending = [], {}
        self.phase = 'body'
        self.on_post = self.after_emitted = None
        self.callback_errors = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def emit(self, events, *, interrupted=False):
                raw = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(raw) + int(interrupted)))
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                output, kind = owner.output(body['input'])
                raw = json.dumps(output, ensure_ascii=False)
                identity = 'resp_product_' + str(len(owner.calls))
                owner.calls.append({'method': 'POST', 'path': self.path, 'kind': kind, 'body': body})
                if owner.on_post is not None:
                    owner.on_post(kind)
                if body.get('background') is True:
                    value = output.get('answer', output.get('summary', ''))
                    cut = raw.index(value) + max(1, len(value) // 2) if value else len(raw) // 2
                    owner.pending[identity] = (raw, cut)
                    events = [{'type': 'response.created', 'sequence_number': 0,
                        'response': {'id': identity, 'status': 'in_progress'}}]
                    if owner.phase == 'body':
                        events.append({'type': 'response.output_text.delta', 'sequence_number': 1, 'delta': raw[:cut]})
                    self.emit(events, interrupted=owner.after_emitted is not None)
                    if owner.after_emitted is not None:
                        try:
                            owner.after_emitted()
                        except Exception as error:
                            owner.callback_errors.append(error)
                elif body.get('stream'):
                    self.emit([{'type': 'response.output_text.delta', 'delta': raw}, owner.completed(identity, raw)])
                else:
                    payload = json.dumps(owner.completed(identity, raw)['response']).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            def do_GET(self):
                identity = urlsplit(self.path).path.rsplit('/', 1)[-1]
                after = int(parse_qs(urlsplit(self.path).query)['starting_after'][0])
                raw, cut = owner.pending[identity]
                owner.calls.append({'method': 'GET', 'path': self.path, 'after': after, 'identity': identity})
                tail = raw if after == 0 else raw[cut:]
                self.emit([{'type': 'response.output_text.delta', 'sequence_number': after + 1, 'delta': tail},
                    owner.completed(identity, raw, after + 2)])

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}/v1'

    @staticmethod
    def output(messages):
        text = '\n'.join(message['content'] for message in messages)
        try:
            context = json.loads(messages[-1]['content'])
        except (ValueError, TypeError):
            context = {}
        if 'output' in context:
            return {'mode': 'cluster', 'assignments': [{'profile_id': 'subagent.worker',
                'task': 'Synthetic child', 'goal': 'Synthetic goal', 'deliverable': '整理稿',
                'capabilities': ['document.draft.propose'], 'depends_on': []}]}, 'steward'
        if 'decision_contract' in context:
            role = context.get('role', {}).get('organization_role')
            if role == '主政协调':
                return {'type': 'complete', 'summary': 'Synthetic completed work'}, 'main'
            return {'type': 'tool', 'capability_id': 'document.draft.propose',
                'arguments': {'title': 'Synthetic draft', 'markdown': 'Synthetic child work', 'final_for': '整理稿'}}, 'worker'
        if 'condensed_question' in text:
            return {'condensed_question': 'alpha beta gamma?'}, 'aux'
        if '"queries"' in text:
            return {'queries': ['alpha beta gamma?']}, 'aux'
        return {'answer': 'Synthetic answer',
            'citations': [int(number) for number in re.findall(r'^\[(\d+)\]', text, re.M)]}, 'answer'

    @staticmethod
    def completed(identity, raw, sequence=None):
        event = {'type': 'response.completed', 'response': {'id': identity, 'status': 'completed',
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': raw}]}],
            'usage': {'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7}}}
        if sequence is not None:
            event['sequence_number'] = sequence
        return event

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@pytest.fixture
def env(tmp_path):
    import litellm
    provider, clients, responses = ProductProvider(), [], []
    class OwnedClient(httpx.Client):
        def close(self):
            super().close()
            if provider.fail_close and any(call['body'].get('background') for call in provider.calls if call['method'] == 'POST'):
                raise ConnectionError('synthetic owner close failure')
    provider.fail_close = False
    def factory():
        client = OwnedClient(trust_env=False, follow_redirects=False)
        client.event_hooks['response'].append(responses.append)
        clients.append(client)
        return client
    records, _namespace = resolve_recognition_document_store(tmp_path)
    native = ResponsesCompletion(api_base=provider.base, capabilities=ModelCapabilities(background_resume=True))
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=native,
        model_http_client_factory=factory)
    models.update('generation', {'base_url': provider.base, 'model': 'main', 'api_key': 'synthetic-only',
        'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    api_mode(models)
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/settings.toml').write_bytes((Path(__file__).parents[3] / 'config/settings.toml.example').read_bytes())
    try:
        yield SimpleNamespace(root=tmp_path, records=records, models=models, provider=provider,
            settings=ProviderStoreSettings(records, models), clients=clients, responses=responses)
    finally:
        provider.close()


def application(env):
    app = create_app(runtime_root=env.root, legacy_app=FastAPI(), model_configuration=env.models)
    service, scope = app.state.recognition_service, WorkScope('local-user', 'project-a')
    experience = service.stage_experience(scope=scope, content='Synthetic evidence')
    candidate = service.propose(scope=scope, content='alpha beta gamma', source_experience_ids=[experience])
    published = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer='local-user')
    env.application, env.source = app, published
    return app


def submit(env, http, intent, *, text=None):
    response = http.post('/api/v2/workbench/turns', json={'project_id': 'project-a', 'intent': intent,
        'text': text or ('alpha beta gamma?' if intent == 'ask' else '写一段总结')})
    assert response.status_code == 200, response.text
    data = response.json()
    identity = data['turn']['id'] if intent == 'ask' else data['turn']['receipt']['do']['kernel_turn_id']
    if intent == 'do':
        deadline = monotonic() + 25
        while monotonic() < deadline:
            receipt = http.get(f"/api/v2/workbench/threads/{data['thread_id']}?project_id=project-a").json()['turns'][0]['receipt']['do']
            if receipt['state'] in {'done', 'failed', 'partial'}:
                break
            sleep(.05)
        assert receipt['state'] == 'done', receipt
    return identity, data


def facts(env, identity):
    store = env.application.state.ai_turn_store
    return store_facts(store, identity)


def store_facts(store, identity):
    events = tuple(store.events_after(identity))
    dispatches = [store.get(event['data']['payload_ref']) for event in events if event['type'] == 'model.attempt.dispatched']
    terminals = [store.get(event['data']['receipt_ref']) for event in events if event['type'] == 'model.attempt.terminal']
    with sqlite3.connect(store._path) as connection:
        checkpoints = [json.loads(row[0]) for row in connection.execute(
            "SELECT payload_json FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind LIKE 'model-provider-checkpoint-%' ORDER BY kind", (identity,))]
        effects = dict(connection.execute('SELECT operation_id,state FROM effect'))
    return dispatches, terminals, checkpoints, effects


@pytest.mark.parametrize('intent', ['ask', 'do'])
@pytest.mark.parametrize('phase', ['before_output', 'body'])
def test_real_main_uses_one_background_create_and_owned_cursor_get(env, intent, phase):
    env.provider.phase = phase
    toggle(env)
    choose(env.models, 'quick')
    with TestClient(application(env)) as http:
        identity, data = submit(env, http, intent)
        posts = [call for call in env.provider.calls if call['method'] == 'POST']
        main = [call for call in posts if call['kind'] == ('answer' if intent == 'ask' else 'main')]
        assert len(main) == 1
        assert main[0]['body']['model'] == 'main'
        assert main[0]['body']['store'] is True and main[0]['body']['background'] is True
        assert main[0]['body']['stream'] is True
        assert all('background' not in call['body'] and 'store' not in call['body'] for call in posts if call not in main)
        gets = [call for call in env.provider.calls if call['method'] == 'GET']
        assert len(gets) == 1 and gets[0]['after'] == (0 if phase == 'before_output' else 1)
        dispatches, terminals, checkpoints, effects = facts(env, identity)
        assert len(dispatches) == len(terminals) == 1
        assert terminals[0]['status'] == 'succeeded'
        assert terminals[0]['usage'] == {'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7}
        assert effects[dispatches[0]['attempt_id']] == 'SETTLED_OK'
        sequences = [0, 1, 2] if phase == 'before_output' else [0, 1, 2, 3]
        assert [item['cursor'] for item in checkpoints] == [
            {'response_id': gets[0]['identity'], 'sequence_number': sequence} for sequence in sequences]
        assert all(item['dispatch'] == dispatches[0] for item in checkpoints)
        assert all(set(item) == {'schema_version', 'dispatch', 'dispatch_ref', 'cursor', 'previous_ref', 'run_lease', 'effect_lease'} for item in checkpoints)
        if intent == 'ask':
            assert data['turn']['receipt']['ask']['answer'] == 'Synthetic answer'
        else:
            composition = env.application.state.agent_runtime_composition
            workers = [run for run in composition.store.list_runs(project_id='project-a') if run.role == 'subagent' and run.profile_id == 'subagent.worker']
            assert len(workers) == 1 and env.records.read(BINDINGS, workers[0].turn_id) is None
            child_events = env.application.state.ai_turn_store.events_after(workers[0].turn_id)
            assert sum(event['type'] == 'tool.intent.recorded' for event in child_events) == 1
            assert sum(event['type'] == 'tool.completed' for event in child_events) == 1
    assert env.clients and all(client.is_closed for client in env.clients)
    assert env.responses and all(response.is_closed for response in env.responses)


@pytest.mark.parametrize('streaming', [False, True])
def test_missing_observer_protocol_closes_real_attempt_before_any_http(env, streaming):
    from backend.memory_app.structured_generation import AskOutput
    from backend.memory_app.v2.policies.retry import decide
    from core.ai_kernel import SQLiteAITurnStore, SynchronousAIRuntime, ScopedCapabilityRegistry
    from core.ai_kernel.turn_kinds import freeze_turn_request
    store = SQLiteAITurnStore(env.root / 'protocol.sqlite3')
    identity = 'invalid-observer-protocol'
    toggle(env)
    class MissingObserverProtocol:
        # This external consumer protocol forwards the real physical owner.
        # It is not a substitute execution Handle or an alternate authority.
        observe_provider_checkpoint = None
        def __init__(self, handle):
            self.original = handle
        def __getattr__(self, name):
            return getattr(self.original, name)
    class Sink:
        def __init__(self, control):
            self.control = control
        def begin_model_wire_attempt(self):
            return MissingObserverProtocol(self.control.begin_model_wire_attempt())
    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control):
            cfg = env.models.public()['generation']
            public = {key: cfg[key] for key in ('purpose', 'provider', 'base_url', 'model',
                'allow_remote', 'revision', 'configured', 'has_api_key')}
            ref = store.get_or_create_immutable_payload(identity, 'protocol-route', public)
            route = {'payload_ref': ref, 'revision': 'a' * 64,
                'prompt_cache_scope_identity': 'b' * 64, 'configuration': public,
                'execution_location': 'local_loopback'}
            env.models.complete_governed([{'role': 'user', 'content': 'Synthetic'}],
                routing_snapshot=route, execution_control=execution_control,
                metadata_sink=execution_control, wire_attempt_sink=Sink(execution_control),
                validate_current=execution_control.checkpoint, retry_policy=decide,
                response_model=AskOutput if streaming else None,
                on_delta=(lambda _text: None) if streaming else None,
                provider_store_activation=ProviderStoreActivation(
                    env.models.provider_store_adapter(), execution_control.checkpoint))
            return {'type': 'complete', 'summary': 'Unexpected'}
    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store)
    request = freeze_turn_request('project.answer', turn_id=identity, session_id='protocol-session',
        operation_id='protocol-operation', idempotency_key=identity, project_id='project-a',
        created_at='2026-10-06T00:00:00Z', text='Synthetic', capabilities=[],
        privacy={'mode': 'remote_allowed', 'allow_remote': True, 'pii': 'possible',
            'consent_refs': ['crp://default/model-settings/generation'], 'retention': 'session'})
    receipt = runtime.submit_turn(request)
    assert receipt.status == 'failed'
    dispatches, terminals, checkpoints, effects = store_facts(store, identity)
    assert len(dispatches) == len(terminals) == 1
    assert terminals[0]['status'] == 'failed_transport'
    assert terminals[0]['usage_status'] == 'unavailable' and terminals[0]['usage'] is None
    assert effects[dispatches[0]['attempt_id']] == 'UNKNOWN' and 'PLANNED' not in effects.values()
    assert checkpoints == [] and env.provider.calls == []


@pytest.mark.parametrize('control', ['off', 'old-missing', 'frozen-off'])
def test_current_toggle_never_adopts_off_or_historical_main(env, control):
    if control == 'old-missing':
        toggle(env)
    choose(env.models, 'quick')
    changed = []
    def after_auxiliary(kind):
        if kind != 'aux' or changed:
            return
        rows = [row for row in env.records.list(BINDINGS) if row.payload['kind'] == 'project.answer']
        assert len(rows) == 1
        changed.append(rows[0].object_id)
        if control == 'old-missing':
            with env.records.begin() as tx:
                tx.delete(BINDINGS, rows[0].object_id, expected_revision=rows[0].revision)
                tx.commit()
        elif control == 'frozen-off':
            toggle(env)
    env.provider.on_post = after_auxiliary
    with TestClient(application(env)) as http:
        identity, data = submit(env, http, 'ask', text='unmatched vocabulary?')
        assert changed == [identity]
        main = [call for call in env.provider.calls if call['kind'] == 'answer']
        assert len(main) == 1
        assert 'store' not in main[0]['body'] and 'background' not in main[0]['body']
        assert not any(call['method'] == 'GET' for call in env.provider.calls)
        dispatches, terminals, checkpoints, _effects = facts(env, identity)
        assert len(dispatches) == len(terminals) == 2
        assert sorted(row['model_id'] for row in dispatches) == ['main', 'quick']
        assert sorted(row['model_id'] for row in terminals) == ['main', 'quick']
        assert all(row['status'] == 'succeeded' for row in terminals)
        assert checkpoints == [] and data['turn']['receipt']['ask']['answer'] == 'Synthetic answer'
        binding = env.records.read(BINDINGS, identity)
        assert binding is None if control == 'old-missing' else binding.payload['enabled'] is False


def primary_identity(env):
    rows = [row for row in env.records.list(BINDINGS) if row.payload['kind'] == 'project.answer']
    assert len(rows) == 1
    return rows[0].object_id


@pytest.mark.parametrize('revocation', ['generation', 'remote', 'source', 'selection', 'execution'])
@pytest.mark.parametrize('phase', ['before_output', 'body'])
def test_each_get_revalidates_current_source_configuration_and_selection(env, revocation, phase):
    from backend.memory_app.source_egress import SourceEgressService
    env.provider.phase = phase
    toggle(env)
    application(env)
    def revoke_after_checkpoint():
        identity = primary_identity(env)
        deadline = monotonic() + 8
        while monotonic() < deadline:
            if facts(env, identity)[2]:
                break
            sleep(.01)
        assert facts(env, identity)[2]
        if revocation in {'generation', 'remote'}:
            cfg = env.models.public()['generation']
            env.models.update('generation', {'base_url': 'https://synthetic.invalid/v1',
                'model': 'different' if revocation == 'generation' else cfg['model'],
                'allow_remote': revocation != 'remote', 'expected_revision': cfg['revision']})
        elif revocation == 'source':
            SourceEgressService(env.records).set_policy(WorkScope('local-user', 'project-a'),
                'recognition', env.source.id, env.source.revision, 0, [])
        elif revocation == 'selection':
            toggle(env, False)
        else:
            runtime = env.application.state.ai_runtime
            from core.ai_kernel.event_store import RunLeaseRevoked
            action = {'schema_version': '1.0.0', 'action_id': 'action-background-cancel',
                'turn_id': identity, 'type': 'cancel', 'target_event_id': None,
                'reason': 'Synthetic user cancelled', 'actor': 'user',
                'expected_sequence': len(tuple(runtime.events_after(identity))),
                'idempotency_key': 'background-cancel', 'created_at': '2026-10-06T00:00:01Z'}
            with pytest.raises(RunLeaseRevoked):
                runtime.apply_action(action)
            assert len(tuple(runtime.events_after(identity))) == action['expected_sequence']
            assert env.application.state.ai_turn_runner.request_turn_cancel(identity,
                reason='Synthetic user cancelled') is True
    env.provider.after_emitted = revoke_after_checkpoint
    with TestClient(env.application) as http:
        response = http.post('/api/v2/workbench/turns', json={
            'project_id': 'project-a', 'intent': 'ask', 'text': 'alpha beta gamma?'})
        assert [type(error).__name__ for error in env.provider.callback_errors] == []
        assert response.status_code >= 400
        identity = primary_identity(env)
        assert sum(call['method'] == 'POST' and call['kind'] == 'answer' for call in env.provider.calls) == 1
        assert not any(call['method'] == 'GET' for call in env.provider.calls)
        dispatches, terminals, checkpoints, effects = facts(env, identity)
        assert len(dispatches) == len(terminals) == 1
        assert terminals[0]['status'] != 'succeeded' and not terminals[0]['usage']
        assert effects[dispatches[0]['attempt_id']] == 'UNKNOWN'
        assert checkpoints and env.application.state.ai_runtime.receipt_for(identity).status == (
            'cancelled' if revocation == 'execution' else 'failed')
    assert all(client.is_closed for client in env.clients)
    assert all(response.is_closed for response in env.responses)


@pytest.mark.parametrize('failure', ['observer', 'close'])
def test_provider_checkpoint_or_owned_close_failure_never_creates_another_post(env, failure):
    toggle(env)
    application(env)
    if failure == 'close':
        env.provider.fail_close = True
    with TestClient(env.application) as http:
        if failure == 'observer':
            from backend.memory_app.kernel.ai_runtime import get_or_build_ai_runtime
            get_or_build_ai_runtime(SimpleNamespace(app=env.application), SimpleNamespace(root_dir=env.root))
            with sqlite3.connect(env.application.state.ai_turn_store._path) as connection:
                connection.execute("CREATE TRIGGER synthetic_checkpoint_failure BEFORE INSERT ON ai_turn_immutable_payloads "
                    "WHEN NEW.kind LIKE 'model-provider-checkpoint-%' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        response = http.post('/api/v2/workbench/turns', json={
            'project_id': 'project-a', 'intent': 'ask', 'text': 'alpha beta gamma?'})
        assert response.status_code >= 400
        identity = primary_identity(env)
        posts = [call for call in env.provider.calls if call['method'] == 'POST' and call['kind'] == 'answer']
        assert len(posts) == 1 and posts[0]['body']['background'] is True
        assert not any(call['method'] == 'GET' for call in env.provider.calls)
        dispatches, terminals, checkpoints, effects = facts(env, identity)
        assert len(dispatches) == len(terminals) == 1
        assert terminals[0]['status'] != 'succeeded' and not terminals[0]['usage']
        assert effects[dispatches[0]['attempt_id']] == 'UNKNOWN'
        if failure == 'observer':
            assert checkpoints == []
    assert env.clients and all(client.is_closed for client in env.clients)
    assert env.responses and all(response.is_closed for response in env.responses)


def local_endpoint_factory(env):
    from tests.backend.unit.llm.test_provider_background import LocalProviderTransport
    def factory():
        client = httpx.Client(transport=LocalProviderTransport(env.provider.base),
            trust_env=False, follow_redirects=False)
        client.event_hooks['response'].append(env.responses.append)
        env.clients.append(client)
        return client
    return factory


def test_exact_official_default_loader_activates_its_existing_native_adapter(env):
    env.models = ModelConfiguration(env.records, env.root, secrets=env.models.secrets,
        model_http_client_factory=local_endpoint_factory(env))
    env.models.update('generation', {'base_url': 'https://api.openai.com/v1',
        'expected_revision': 1})
    env.settings = ProviderStoreSettings(env.records, env.models)
    loader = env.models._completion_fn
    assert not isinstance(loader, ResponsesCompletion)
    toggle(env)
    with TestClient(application(env)) as http:
        identity, data = submit(env, http, 'ask')
        assert env.models._completion_fn is loader
        assert [(call['method'], call['path']) for call in env.provider.calls] == [
            ('POST', '/v1/responses'), ('GET', '/v1/responses/resp_product_0?stream=true&starting_after=1')]
        body = env.provider.calls[0]['body']
        assert body['background'] is True and body['store'] is True and body['model'] == 'main'
        dispatches, terminals, checkpoints, effects = facts(env, identity)
        assert len(dispatches) == len(terminals) == 1 and terminals[0]['status'] == 'succeeded'
        assert terminals[0]['usage'] == {'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7}
        assert effects[dispatches[0]['attempt_id']] == 'SETTLED_OK' and len(checkpoints) == 4
        assert data['turn']['receipt']['ask']['answer'] == 'Synthetic answer'
    assert all(client.is_closed for client in env.clients)
    assert all(response.is_closed for response in env.responses)


def test_real_subscription_keeps_original_store_false_and_never_adopts_api_choice(env, tmp_path):
    from backend.memory_app.chatgpt_subscription import ChatGPTSubscriptions
    from tests.memory_app.v2.test_chatgpt_subscription import env as auth_fixture, login
    toggle(env)
    auth = auth_fixture.__wrapped__(tmp_path / 'synthetic-auth')
    original, state, _secrets, _records = next(auth)
    subscriptions = ChatGPTSubscriptions(env.records, env.models.secrets, client=original.client)
    try:
        _attempt, _params, callback = login((subscriptions, state, env.models.secrets, env.records))
        assert callback.status_code == 200 and subscriptions.status()['connected']
        env.models = ModelConfiguration(env.records, env.root, secrets=env.models.secrets,
            subscriptions=subscriptions, completion_fn=env.models._completion_fn,
            model_http_client_factory=local_endpoint_factory(env))
        env.models.select_subscription(model='gpt-fixture', expected_revision=0)
        env.settings = ProviderStoreSettings(env.records, env.models)
        assert env.models.provider_store_adapter() is None and env.settings.get()['enabled'] is False
        with TestClient(application(env)) as http:
            identity, data = submit(env, http, 'ask')
            assert len(env.provider.calls) == 1 and env.provider.calls[0]['method'] == 'POST'
            body = env.provider.calls[0]['body']
            assert body['model'] == 'gpt-fixture' and body['store'] is False and 'background' not in body
            assert env.records.read(BINDINGS, identity).payload['enabled'] is False
            dispatches, terminals, checkpoints, effects = facts(env, identity)
            assert len(dispatches) == len(terminals) == 1 and terminals[0]['status'] == 'succeeded'
            assert effects[dispatches[0]['attempt_id']] == 'SETTLED_OK' and checkpoints == []
            assert data['turn']['receipt']['ask']['answer'] == 'Synthetic answer'
        assert all(client.is_closed for client in env.clients)
        assert all(response.is_closed for response in env.responses)
    finally:
        subscriptions.close()
        original.client.close()
        auth.close()
