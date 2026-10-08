"""Local Responses HTTP streams with real kernel model-attempt ownership."""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import httpx
import pytest

from backend.shared.llm.litellm_gateway import (
    LiteLLMCompletionGateway, ModelRetryControl, _model_transport_failure,
)
from backend.shared.llm.openai_responses import (
    ResponsesCompletion, ProviderBackgroundOptions, ResponsesBackgroundInterrupted,
)
from backend.shared.llm.model_capabilities import ModelCapabilities
from backend.memory_app.v2.policies.retry import decide
from core.ai_kernel import SQLiteAITurnStore, SynchronousAIRuntime, ScopedCapabilityRegistry
from tests.backend.unit.llm.test_model_transport_watchdog import Answer


class BackgroundProvider:
    def __init__(self, mode='resume'):
        self.calls, self.bodies = [], []
        self.mode = mode
        self.release = Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def emit(self, events, *, stall=False):
                raw = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                if not stall:
                    self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()
                if stall:
                    owner.release.wait(2)

            def do_POST(self):
                owner.calls.append(('POST', self.path))
                owner.bodies.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                events = [{'type': 'response.created', 'sequence_number': 0,
                           'response': {'id': 'resp_local', 'status': 'in_progress'}},
                          {'type': 'response.output_text.delta', 'sequence_number': 1,
                           'delta': '{"answer":"hel'}]
                if owner.mode == 'no_id':
                    events = []  # A real HTTP 200 accepted create, before ID bytes.
                elif owner.mode in {'get_eof', 'stall'}:
                    events = events[:1]
                elif owner.mode.startswith('bad_'):
                    events[1]['sequence_number'] = {'bad_bool': True, 'bad_negative': -1,
                        'bad_string': '1', 'bad_gap': 2}[owner.mode]
                elif owner.mode == 'full':
                    events[1]['delta'] = '{"answer":"hello"}'
                    events.append(owner.completed(2))
                self.emit(events, stall=owner.mode == 'stall')

            def do_GET(self):
                owner.calls.append(('GET', self.path))
                events = [{'type': 'response.output_text.delta', 'sequence_number': 1,
                            'delta': '{"answer":"hel'},
                           {'type': 'response.output_text.delta', 'sequence_number': 2,
                            'delta': 'lo"}'},
                           owner.completed(3)]
                if owner.mode == 'get_eof':
                    events = []
                elif owner.mode == 'foreign':
                    events[-1]['response']['id'] = 'resp_foreign'
                elif owner.mode == 'conflicting_duplicate':
                    events[0]['delta'] = '{"answer":"other'
                self.emit(events)

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}/v1'

    @staticmethod
    def completed(sequence):
        return {'type': 'response.completed', 'sequence_number': sequence,
            'response': {'id': 'resp_local', 'status': 'completed',
                'output': [{'type': 'message', 'content': [
                    {'type': 'output_text', 'text': '{"answer":"hello"}'}]}],
                'usage': {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}}}

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class LocalProviderTransport(httpx.BaseTransport):
    """External endpoint mapping; the real HTTPX/socket parser stays in use."""
    def __init__(self, base):
        self.base, self.inner = base, httpx.HTTPTransport()

    def handle_request(self, request):
        target = self.base.removesuffix('/v1') + request.url.raw_path.decode()
        return self.inner.handle_request(httpx.Request(request.method, target,
            headers=request.headers, stream=request.stream, extensions=request.extensions))

    def close(self):
        self.inner.close()


def background_call(tmp_path, provider, *, enabled=True, subscription=False,
                    resume_budget=1, withdraw=False, close_fails=False, factory_fails=False,
                    budget_fails=False, cancel=False, policy=decide, observation=None,
                    provider_observer=None, resume_only=False, resume_cursor=None):
    store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    clients, responses, deltas, decisions = [], [], [], []
    withdrawn = False
    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        if factory_fails and factory_calls == 2:
            raise ConnectionError('synthetic_get_client_unavailable')
        class FailingCloseClient(httpx.Client):
            def close(self):
                super().close()
                raise RuntimeError('synthetic_owned_close_failure')
        client_type = FailingCloseClient if close_fails else httpx.Client
        client = client_type(transport=LocalProviderTransport(provider.base), follow_redirects=False)
        client.event_hooks['response'].append(responses.append)
        clients.append(client)
        return client

    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control):
            execution_control.model_call_routed(
                snapshot_ref='crp://session/test/turn-model-routing-snapshot-v1/frozen',
                snapshot_revision='a' * 64, prompt_cache_scope_identity='b' * 64,
                provider='openai', model='synthetic', execution_location='local_loopback')
            execution_control.model_call_started(provider='openai', model='synthetic')

            class Lease:
                def finish(self, *_args, **_kwargs):
                    return

            def checkpoint():
                execution_control.checkpoint()
                if withdrawn:
                    raise PermissionError('synthetic_permission_revoked')

            def resume(value):
                nonlocal withdrawn
                decisions.append(value)
                if budget_fails:
                    raise ConnectionError('synthetic_get_budget_unavailable')
                if withdraw:
                    withdrawn = True
                return len(decisions) <= resume_budget

            capabilities = ModelCapabilities(background_resume=True)
            native = ResponsesCompletion(api_base=None if subscription else provider.base,
                capabilities=capabilities)
            control = ModelRetryControl(policy=policy, checkpoint=checkpoint,
                owned_client_factory=factory, wait=lambda _delay: None, jitter=lambda: 0)
            options_type, extra = ProviderBackgroundOptions, {}
            if resume_only:
                from backend.shared.llm.openai_responses import ProviderResponseCursor, ProviderResumeOnlyOptions
                options_type = ProviderResumeOnlyOptions
                extra['cursor'] = (ProviderResponseCursor(**resume_cursor)
                    if type(resume_cursor) is dict else resume_cursor)
            gateway = LiteLLMCompletionGateway(provider='openai', model='synthetic',
                base_url=provider.base, api_key='synthetic', completion_fn=native,
                capabilities=capabilities,
                background_options=options_type(checkpoint=checkpoint, resume=resume,
                    **({'observe': provider_observer} if provider_observer is not None else {}), **extra) if enabled else None,
                egress_guard=lambda *_args: (checkpoint() or Lease()))
            def consume_delta(text):
                deltas.append(text)
                if cancel:
                    raise asyncio.CancelledError()

            try:
                result, usage = gateway.stream_structured_with_usage(
                    [{'role': 'user', 'content': 'synthetic'}], response_model=Answer,
                    on_delta=consume_delta, validate_current=checkpoint, wire_attempt_sink=execution_control,
                    retry_control=control, timeout=20)
            except BaseException as error:
                if observation is not None:
                    observation.update(error=error, phase=control.phase, retry_counts=dict(control.counts))
                raise
            execution_control.model_call_completed(usage=usage)
            return {'type': 'complete', 'summary': result.answer}

    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store)
    request = json.loads((Path(__file__).resolve().parents[4] /
        'core-contracts/ai/fixtures/turn-request/valid-project-answer.json').read_text(encoding='utf-8'))
    try:
        receipt = runtime.submit_turn(request)
    finally:
        if observation is not None:
            observation.update(clients=clients, responses=responses, deltas=deltas, decisions=decisions,
                dispatches=[event for event in runtime.events_after(request['turn_id'])
                            if event['type'] == 'model.attempt.dispatched'],
                terminal=[store.get(event['data']['receipt_ref']) for event in runtime.events_after(request['turn_id'])
                          if event['type'] == 'model.attempt.terminal'])
    terminal = [store.get(event['data']['receipt_ref']) for event in runtime.events_after(receipt.turn_id)
                if event['type'] == 'model.attempt.terminal']
    return receipt, terminal, clients, responses, deltas, decisions


@pytest.mark.parametrize('kind', ['connection', 'timeout', 'stalled', 'header_timeout', 'rate_limit', 'server'])
def test_background_marker_classifies_safe_transport_without_general_provider_retry(kind):
    now = datetime.now(timezone.utc)
    assert _model_transport_failure(ResponsesBackgroundInterrupted(kind), now) == (kind, None)
    ordinary = RuntimeError('synthetic_provider_rejected')
    ordinary.kind = kind
    assert _model_transport_failure(ordinary, now) == ('provider_rejected', None)


def test_closed_background_body_interruption_is_transport_but_never_new_attempt(tmp_path):
    provider = BackgroundProvider()
    observation = {}
    try:
        receipt, terminal, clients, responses, deltas, decisions = background_call(
            tmp_path, provider, resume_budget=0, observation=observation)
        assert receipt.status == 'failed'
        assert provider.calls == [('POST', '/v1/responses')]
        assert isinstance(observation['error'], ResponsesBackgroundInterrupted)
        assert _model_transport_failure(observation['error'], datetime.now(timezone.utc)) == ('connection', None)
        assert observation['phase'] == 'body' and observation['retry_counts'] == {}
        assert len(observation['dispatches']) == 1
        assert len(terminal) == 1 and terminal[0]['status'] == 'failed_transport'
        assert terminal[0].get('usage') is None
        assert deltas == ['hel'] and len(decisions) == 1
        assert decisions[0]['sequence'] == 1 and decisions[0]['kind'] == 'connection'
        assert len(clients) == len(responses) == 1
        assert clients[0].is_closed and responses[0].is_closed
        with sqlite3.connect(f"file:{tmp_path / 'turns.sqlite3'}?mode=ro", uri=True) as connection:
            assert connection.execute('SELECT state FROM effect').fetchall() == [('UNKNOWN',)]
    finally:
        provider.close()


def test_background_disconnect_reads_same_response_without_new_dispatch(tmp_path):
    provider = BackgroundProvider()
    try:
        receipt, terminal, clients, responses, deltas, decisions = background_call(tmp_path, provider)
        assert receipt.status == 'completed'
        assert provider.calls == [('POST', '/v1/responses'),
            ('GET', '/v1/responses/resp_local?stream=true&starting_after=1')]
        assert provider.bodies[0]['background'] is provider.bodies[0]['store'] is True
        assert len(terminal) == 1 and terminal[0]['status'] == 'succeeded'
        assert terminal[0]['usage'] == {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}
        assert ''.join(deltas) == 'hello' and len(decisions) == 1
        assert clients and all(client.is_closed for client in clients)
        assert len(responses) == 2 and all(response.is_closed for response in responses)
    finally:
        provider.close()


@pytest.mark.parametrize('subscription', [False, True])
def test_background_off_preserves_api_and_subscription_wire(tmp_path, subscription):
    provider = BackgroundProvider('full')
    try:
        receipt, terminal, clients, responses, deltas, decisions = background_call(
            tmp_path, provider, enabled=False, subscription=subscription)
        assert receipt.status == 'completed'
        assert provider.calls == [('POST', '/v1/responses')]
        assert 'background' not in provider.bodies[0]
        if subscription:
            assert provider.bodies[0]['store'] is False
        else:
            assert 'store' not in provider.bodies[0]
        assert len(terminal) == 1 and terminal[0]['status'] == 'succeeded'
        assert ''.join(deltas) == 'hello' and decisions == []
        assert all(client.is_closed for client in clients)
        assert all(response.is_closed for response in responses)
    finally:
        provider.close()


@pytest.mark.parametrize('mode,resume_budget,expected_gets', [('no_id', 1, 0),
    ('get_eof', 1, 1), ('resume', 0, 0)])
def test_accepted_response_stop_never_dispatches_another_post(tmp_path, mode, resume_budget, expected_gets):
    provider = BackgroundProvider(mode)
    try:
        receipt, terminal, clients, responses, _deltas, decisions = background_call(
            tmp_path, provider, resume_budget=resume_budget)
        assert receipt.status == 'failed'
        assert [method for method, _path in provider.calls] == ['POST'] + ['GET'] * expected_gets
        assert len(terminal) == 1 and terminal[0]['status'] == 'failed_transport'
        assert terminal[0].get('usage') is None  # Unknown provider cost is not zero.
        assert all(client.is_closed for client in clients)
        assert all(response.is_closed for response in responses)
        assert all(set(value) == {'kind', 'sequence', 'remaining'}
            and type(value['sequence']) is int and value['remaining'] > 0 for value in decisions)
    finally:
        provider.close()


@pytest.mark.parametrize('mode', ['bad_bool', 'bad_negative', 'bad_string', 'bad_gap',
                                 'foreign', 'conflicting_duplicate'])
def test_untrusted_response_identity_and_sequence_fail_closed(tmp_path, mode):
    provider = BackgroundProvider(mode)
    try:
        receipt, terminal, clients, responses, _deltas, _decisions = background_call(tmp_path, provider)
        assert receipt.status == 'failed'
        assert sum(method == 'POST' for method, _path in provider.calls) == 1
        assert sum(method == 'GET' for method, _path in provider.calls) == int(mode in {'foreign', 'conflicting_duplicate'})
        assert len(terminal) == 1 and terminal[0]['status'] == 'failed_transport'
        assert all(client.is_closed for client in clients)
        assert all(response.is_closed for response in responses)
    finally:
        provider.close()


@pytest.mark.parametrize('failure', ['withdraw', 'close_fails'])
def test_checkpoint_and_owned_cleanup_stop_get_without_new_post(tmp_path, failure):
    provider = BackgroundProvider()
    try:
        receipt, terminal, clients, responses, _deltas, _decisions = background_call(
            tmp_path, provider, **{failure: True})
        assert receipt.status == 'failed'
        assert provider.calls == [('POST', '/v1/responses')]
        assert len(terminal) == 1 and terminal[0]['status'] == 'failed_transport'
        assert all(client.is_closed for client in clients)
        assert all(response.is_closed for response in responses)
    finally:
        provider.close()


def test_subscription_and_unproven_proxy_cannot_enable_background():
    options = ProviderBackgroundOptions(checkpoint=lambda: None, resume=lambda _value: True)
    for native, capabilities in [(ResponsesCompletion(), ModelCapabilities(background_resume=True)),
            (ResponsesCompletion(api_base='https://example.test/v1'), ModelCapabilities(background_resume=True)),
            (ResponsesCompletion(api_base='https://example.test/v1', capabilities=ModelCapabilities(background_resume=True)),
             ModelCapabilities())]:
        with pytest.raises(ValueError, match='provider_background_adapter_invalid'):
            LiteLLMCompletionGateway(provider='openai', model='synthetic',
                base_url='https://example.test/v1', api_key='synthetic', completion_fn=native,
                capabilities=capabilities, background_options=options)


def test_accepted_response_get_client_failure_cannot_create_new_response(tmp_path):
    provider = BackgroundProvider('get_eof')
    try:
        receipt, terminal, clients, responses, deltas, _decisions = background_call(
            tmp_path, provider, factory_fails=True)
        assert receipt.status == 'failed'
        assert provider.calls == [('POST', '/v1/responses')]
        assert len(terminal) == 1 and terminal[0]['status'] == 'failed_transport'
        assert terminal[0].get('usage') is None and deltas == []
        assert len(clients) == len(responses) == 1
        assert clients[0].is_closed and responses[0].is_closed
    finally:
        provider.close()


def test_accepted_response_budget_callback_failure_cannot_create_new_response(tmp_path):
    provider = BackgroundProvider('get_eof')
    try:
        receipt, terminal, clients, responses, deltas, decisions = background_call(
            tmp_path, provider, budget_fails=True)
        assert receipt.status == 'failed'
        assert provider.calls == [('POST', '/v1/responses')]
        assert len(terminal) == 1 and terminal[0]['status'] == 'failed_transport'
        assert terminal[0].get('usage') is None and deltas == [] and len(decisions) == 1
        assert len(clients) == len(responses) == 1
        assert clients[0].is_closed and responses[0].is_closed
    finally:
        provider.close()


def test_created_response_body_watchdog_cannot_create_another_response(tmp_path):
    def short_idle(value):
        result = dict(decide(value))
        if value['kind'] == 'limits':
            result.update(idle_timeout=0.1, header_timeout=1)
        return result

    provider = BackgroundProvider('stall')
    try:
        receipt, terminal, clients, responses, deltas, decisions = background_call(
            tmp_path, provider, resume_budget=0, policy=short_idle)
        assert receipt.status == 'failed'
        assert provider.calls == [('POST', '/v1/responses')]
        assert len(terminal) == 1 and terminal[0]['status'] == 'failed_transport'
        assert terminal[0].get('usage') is None and deltas == []
        assert len(decisions) == 1 and decisions[0]['kind'] == 'stalled' and decisions[0]['sequence'] == 0
        assert len(clients) == len(responses) == 1
        assert clients[0].is_closed and responses[0].is_closed
    finally:
        provider.close()


def test_background_consumer_cancel_closes_actual_socket_without_get(tmp_path):
    provider = BackgroundProvider()
    observation = {}
    try:
        with pytest.raises(asyncio.CancelledError):
            background_call(tmp_path, provider, cancel=True, observation=observation)
        assert provider.calls == [('POST', '/v1/responses')]
        assert len(observation['terminal']) == 1
        assert observation['terminal'][0]['status'] == 'consumer_cancelled'
        assert observation['terminal'][0].get('usage') is None
        assert observation['deltas'] == ['hel'] and observation['decisions'] == []
        assert len(observation['clients']) == len(observation['responses']) == 1
        assert observation['clients'][0].is_closed and observation['responses'][0].is_closed
    finally:
        provider.close()


def test_background_off_preserves_legacy_derived_cache_identity():
    # Literal read from the real HEAD ModelCapabilities instance, before this leaf.
    legacy = ("ModelCapabilities(structured_modes=('schema', 'json_object', 'prompt'), "
        "developer_role=True, stream_usage=True, max_tokens_field='max_tokens', "
        "reasoning_efforts=('low', 'medium', 'high'), prices=None)")
    capabilities = ModelCapabilities()
    assert repr(capabilities) == legacy
    gateway = LiteLLMCompletionGateway(provider='openai', model='synthetic',
        base_url='https://example.test/v1', api_key='synthetic',
        completion_fn=ResponsesCompletion(api_base='https://example.test/v1'))
    assert gateway.cache_identity.endswith('|' + legacy)
    assert gateway._structured_mode_cache_key.endswith('|' + legacy)
    assert capabilities.background_resume is False
    assert ModelCapabilities(background_resume=True) != capabilities
    with pytest.raises(ValueError, match='invalid model capability declaration'):
        ModelCapabilities(background_resume=1)
