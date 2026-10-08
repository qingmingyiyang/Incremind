"""原治理模型执行消费恢复数据；产品胶囊与动作许可仍由原主人接入。"""
from copy import deepcopy
from functools import partial
import json
import sqlite3
import sys
from uuid import uuid4

import httpx
from pydantic import BaseModel
import pytest

from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError, ProviderStoreActivation, _GenerationEgressLease
from backend.memory_app.turn_routing import RecognitionModelRoutingSnapshotAuthority
from backend.memory_app.v2.policies.retry import decide
from backend.security.secrets import InMemorySecretStore
from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway
from backend.shared.llm.model_capabilities import ModelCapabilities
from backend.shared.llm.openai_responses import ResponsesCompletion
from core.storage_provider import SQLiteStructuredRecordStore
from tests.backend.unit.llm.test_provider_store_capability import api_mode
from tests.rebuild.test_ai_model_provider_resume import _Owner


class _Answer(BaseModel):
    answer: str


class _ConfigurationOwner(_Owner):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.calls, self.clients, self.responses, self.observers, self.lease_finishes = [], [], [], [], []
        self.native = ResponsesCompletion(api_base='http://127.0.0.1:9987/v1',
            capabilities=ModelCapabilities(background_resume=True))
        self.models = ModelConfiguration(SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3'), tmp_path,
            InMemorySecretStore(), completion_fn=self.native, model_http_client_factory=self.client,
            gateway_factory=partial(LiteLLMCompletionGateway, provider_attempt_observer=self.observe))
        self.models.update('generation', {'base_url': self.native.api_base, 'model': 'synthetic',
            'api_key': 'synthetic-only', 'allow_remote': True, 'enabled': True, 'expected_revision': 0})
        api_mode(self.models)
        # 原路由权威和原内存协议生成身份；不构造应用或文件摘要。
        self.route = RecognitionModelRoutingSnapshotAuthority(self.models, self.store).acquire(
            turn_id=self.request['turn_id'], project_id=self.request['scope']['project_id'],
            context_packet_id='synthetic-context', project_profile_id='synthetic-project', project_profile_revision=1,
            boundary_profile_id='synthetic-boundary', boundary_profile_revision=1,
            capability_ids=(), agent_binding=None, allow_remote=False, expected_execution_location='local_loopback')

    def begin(self, **changes):
        values = {'revision': self.route.revision, 'model': 'synthetic', 'route_ref': self.route.payload_ref}
        values.update(changes)
        control = self.runtime._begin_planner_control(self.request['turn_id'],
            step_id='configuration-old-step-' + uuid4().hex, model_request_id='configuration-old-model-' + uuid4().hex)
        control.model_call_routed(snapshot_ref=values['route_ref'], snapshot_revision=values['revision'],
            prompt_cache_scope_identity=self.route.payload['prompt_cache_scope_identity'], provider='openai',
            model=values['model'], execution_location='local_loopback')
        control.model_call_started(provider='openai', model=values['model'])
        self.control = control
        return control.begin_model_wire_attempt()

    def observe(self, status, error):
        self.observers.append((status, error))

    def client(self):
        # 仅替换外部 HTTP provider，配置、网关、native、Handle 和 Effect 使用原实现。
        client = httpx.Client(transport=httpx.MockTransport(self.transport), trust_env=False, follow_redirects=False)
        client.event_hooks['response'].append(self.responses.append)
        self.clients.append(client)
        return client

    def transport(self, request):
        self.calls.append((request.method, str(request.url), json.loads(request.content) if request.content else None))
        raw = json.dumps({'answer': 'complete original answer'})
        event = {'type': 'response.completed', 'sequence_number': 3,
            'response': {'id': 'resp_owned_resume', 'status': 'completed',
                'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': raw}]}],
                'usage': {'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7}}}
        events = ([{'type': 'response.output_text.delta', 'sequence_number': 2, 'delta': raw}]
            if request.method == 'POST' else []) + [event]
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
            content=''.join('data: ' + json.dumps(value) + '\n\n' for value in events).encode())

    def complete(self, activation, deltas, *, route=None):
        control = self.runtime._begin_planner_control(self.request['turn_id'],
            step_id='configuration-step-' + uuid4().hex, model_request_id='configuration-model-' + uuid4().hex)
        self.control = control
        previous = sys.getprofile()
        def observe_lease(frame, event, _arg):
            if event == 'call' and frame.f_code is _GenerationEgressLease.finish.__code__:
                self.lease_finishes.append(frame.f_locals['status'])
        sys.setprofile(observe_lease)
        try:
            return self.models.complete_governed([{'role': 'user', 'content': 'synthetic current request'}],
                routing_snapshot=route or self.route.generation_binding(), execution_control=control,
                metadata_sink=control, wire_attempt_sink=control, response_model=_Answer,
                on_delta=deltas.append, retry_policy=decide, provider_store_activation=activation)
        finally:
            sys.setprofile(previous)

    def latest_terminal(self):
        events = self.store.events_after(self.request['turn_id'])
        dispatched = [event for event in events if event['type'] == 'model.attempt.dispatched']
        terminals = [event for event in events if event['type'] == 'model.attempt.terminal']
        assert len(dispatched) == len(terminals) == 2
        terminal = self.store.get(terminals[-1]['data']['receipt_ref'])
        with sqlite3.connect(self.database) as connection:
            state = connection.execute('SELECT state FROM effect WHERE operation_id=?', (terminal['attempt_id'],)).fetchone()[0]
        return terminal, state


@pytest.fixture
def owner(tmp_path):
    value = _ConfigurationOwner(tmp_path)
    try:
        yield value
    finally:
        value.runtime._run_lease_context.reset(value.token)


def test_real_configuration_get_only_binds_frozen_source_in_original_effect(owner):
    old, _ = owner.failed_source()
    source, old_facts = owner.resolve(old), deepcopy(owner.source_facts(old))
    activation = ProviderStoreActivation(adapter=owner.native, validate_current=lambda: None, resume_source=source)
    source['cursor']['sequence_number'] = 17
    source['checkpoint_ref'] = 'crp://session/forged/checkpoint'
    assert activation.resume_source == owner.resolve(old)
    with pytest.raises(TypeError):
        activation.resume_source['cursor']['sequence_number'] = 17
    deltas = []
    result, usage = owner.complete(activation, deltas)
    assert result.answer == 'complete original answer' and ''.join(deltas) == result.answer
    assert set(usage) == {'model', 'configuration_revision', 'usage', 'context_budget'}
    assert usage['model'] == 'synthetic' and usage['configuration_revision'] == owner.models.public()['generation']['revision']
    assert usage['usage'] == {'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7}
    assert set(usage['context_budget']) == {'estimated_input_tokens', 'reserve', 'window'}
    assert type(usage['context_budget']['estimated_input_tokens']) is int and usage['context_budget']['estimated_input_tokens'] > 0
    assert usage['context_budget']['reserve'] == 1800 and usage['context_budget']['window'] == 16000
    assert owner.calls == [('GET', owner.native.api_base + '/responses/resp_owned_resume?stream=true&starting_after=2', None)]
    terminal, state = owner.latest_terminal()
    assert terminal['status'] == 'succeeded' and terminal['usage'] == usage['usage'] and state == 'SETTLED_OK'
    link = owner.store.get_immutable_payload(owner.request['turn_id'], 'model-provider-resume-' + terminal['attempt_id'])
    assert link is not None and link[1]['source'] == owner.resolve(old)
    assert owner.source_facts(old) == old_facts
    assert owner.lease_finishes == ['succeeded'] and len(owner.observers) == 1 and owner.observers[0][0] == 'succeeded'
    assert len(owner.clients) == len(owner.responses) == 1
    assert all(value.is_closed for value in owner.clients + owner.responses)


@pytest.mark.parametrize('failure', ['sql', 'source-drift', 'route-drift'])
def test_real_configuration_binding_failure_has_one_original_terminal_and_zero_wire(owner, failure):
    old, refs = owner.failed_source()
    old_facts = deepcopy(owner.source_facts(old))
    activation = ProviderStoreActivation(adapter=owner.native, validate_current=lambda: None, resume_source=owner.resolve(old))
    route = owner.route.generation_binding()
    with sqlite3.connect(owner.database) as connection:
        if failure == 'sql':
            connection.execute("CREATE TRIGGER reject_resume BEFORE INSERT ON ai_turn_immutable_payloads "
                "WHEN NEW.kind LIKE 'model-provider-resume-%' BEGIN SELECT RAISE(ABORT,'synthetic binding failure'); END")
        elif failure == 'source-drift':
            payload = json.loads(connection.execute('SELECT payload_json FROM ai_turn_immutable_payloads WHERE payload_ref=?',
                (refs[-1],)).fetchone()[0])
            payload['cursor']['sequence_number'] = 17
            connection.execute('UPDATE ai_turn_immutable_payloads SET payload_json=? WHERE payload_ref=?',
                (json.dumps(payload), refs[-1]))
        else:
            route['payload_ref'] = owner.store.get_or_create_immutable_payload(owner.request['turn_id'], 'another-configuration-route',
                dict(owner.route.payload))
    with pytest.raises(ModelConfigurationError, match='provider_resume_binding_invalid'):
        owner.complete(activation, [], route=route)
    terminal, state = owner.latest_terminal()
    assert terminal['status'] == 'failed_transport' and state == 'UNKNOWN'
    assert owner.source_facts(old) == old_facts
    assert owner.calls == owner.clients == owner.responses == []
    assert owner.lease_finishes == ['failed'] and len(owner.observers) == 1 and owner.observers[0][0] == 'failed'
    assert owner.store.get_immutable_payload(owner.request['turn_id'], 'model-provider-resume-' + terminal['attempt_id']) is None


def test_absent_resume_source_keeps_original_default_off_wire(owner):
    old, _ = owner.failed_source()
    result, usage = owner.complete(None, [])
    assert result.answer == 'complete original answer' and usage['usage'] == {'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7}
    assert len(owner.calls) == 1 and owner.calls[0][0] == 'POST'
    assert 'store' not in owner.calls[0][2] and 'background' not in owner.calls[0][2]
    terminal, state = owner.latest_terminal()
    assert terminal['status'] == 'succeeded' and state == 'SETTLED_OK'
    assert owner.store.get_immutable_payload(owner.request['turn_id'], 'model-provider-resume-' + terminal['attempt_id']) is None
    assert owner.resolve(old)['cursor']['sequence_number'] == 2


def test_resume_data_cannot_enable_auxiliary_purpose(owner):
    old, _ = owner.failed_source()
    with pytest.raises(ModelConfigurationError, match='provider_resume_source_invalid'):
        ProviderStoreActivation(adapter=owner.native, validate_current=lambda: None,
            expected_purpose='aux', resume_source=owner.resolve(old))
    assert owner.calls == owner.clients == owner.observers == owner.lease_finishes == []
