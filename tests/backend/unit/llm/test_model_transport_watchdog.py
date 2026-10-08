"""Real loopback HTTP socket, existing SDK/Responses and durable SQLite attempts."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from time import monotonic, sleep

import httpx
import pytest
from pydantic import BaseModel

from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway, ModelRetryControl
from backend.shared.llm.openai_responses import ResponsesCompletion
from backend.shared.llm.model_capabilities import ModelCapabilities
from backend.memory_app.v2.policies.retry import decide
from core.ai_kernel import SQLiteAITurnStore, SynchronousAIRuntime, ScopedCapabilityRegistry


class Answer(BaseModel):
    answer: str


class LoopbackProvider:
    def __init__(self, mode, protocol):
        self.calls, self.mode, self.protocol = 0, mode, protocol
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if protocol == 'responses':
                    assert body['store'] is False and body['stream'] is True
                owner.calls += 1
                call = owner.calls
                self.close_connection = True
                if mode == 'header' and call == 1:
                    sleep(.18)
                try:
                    if call == 1 and mode in {'server', 'rate_limit', 'quota', 'unauthorized', 'forbidden'}:
                        status = {'server': 503, 'rate_limit': 429, 'quota': 429,
                            'unauthorized': 401, 'forbidden': 403}[mode]
                        self.send_response(status)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Retry-After', '2' if mode == 'server' else '0')
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        self.wfile.write(json.dumps({'error': {'code': 'insufficient_quota'
                            if mode == 'quota' else 'synthetic_provider_failure'}}).encode())
                        return
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.flush()

                    def event(value):
                        self.wfile.write(('data: ' + json.dumps(value) + '\n\n').encode())
                        self.wfile.flush()

                    if call == 1 and mode in {'early_eof', 'body_eof', 'refusal', 'explicit_incomplete'}:
                        if mode == 'body_eof':
                            event({'type': 'response.output_text.delta', 'delta': '{"answer":"partial\\n\\n'})
                        elif mode == 'refusal':
                            event({'type': 'response.refusal.delta', 'delta': 'synthetic refusal'})
                        elif mode == 'explicit_incomplete':
                            event({'type': 'response.incomplete', 'response': {'status': 'incomplete'}})
                        return

                    if mode in {'heartbeat', 'total'}:
                        for _ in range(5 if mode == 'heartbeat' else 80):
                            self.wfile.write(b': keep-alive\n\n')
                            self.wfile.flush()
                            sleep(.025)
                    if mode in {'idle', 'body'} and call == 1:
                        if protocol == 'responses':
                            if mode == 'body':
                                event({'type': 'response.output_text.delta', 'delta': '{"answer":"partial\\n\\n'})
                        else:
                            delta = {'reasoning_content': 'thinking'} if mode == 'idle' else {
                                'content': '{"answer":"partial\\n\\n'}
                            event({'choices': [{'delta': delta}]})
                        sleep(.18)
                    text = '{"answer":"done"}'
                    if protocol == 'responses':
                        event({'type': 'response.output_text.delta', 'delta': text})
                        event({'type': 'response.completed', 'response': {'status': 'completed',
                            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}],
                            'usage': {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}}})
                    else:
                        event({'choices': [{'delta': {'content': text}}]})
                        event({'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                            'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}})
                        self.wfile.write(b'data: [DONE]\n\n')
                except (OSError, BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}/v1'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class RoutedTransport(httpx.BaseTransport):
    """Only the test rewrites the native fixed URL to its fake local provider."""
    def __init__(self, base=None, observation=None):
        self.base, self.transport, self.observation = base, httpx.HTTPTransport(), observation

    def handle_request(self, request):
        if self.base is not None:
            request.url = httpx.URL(self.base + '/responses')
        response = self.transport.handle_request(request)
        if self.observation is not None:
            response.stream = ObservedBytes(response.stream, self.observation)
        return response

    def close(self):
        self.transport.close()


class ObservedBytes(httpx.SyncByteStream):
    def __init__(self, inner, observation):
        self.inner, self.observation = inner, observation

    def __iter__(self):
        for value in self.inner:
            self.observation['raw_bytes'] += len(value)
            yield value

    def close(self):
        self.inner.close()
        self.observation['body_closed'] = True


def run_http(tmp_path, mode, protocol, *, native_capabilities=False, streaming=True, owned_clients=None,
             observation=None):
    provider = LoopbackProvider(mode, protocol)
    store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    responses, retries, deltas = [], [], []
    fake_elapsed = [0.0]
    if observation is not None:
        observation.update(raw_bytes=0, body_closed=False)
    client = httpx.Client(transport=RoutedTransport(provider.base), trust_env=False,
        event_hooks={'response': [responses.append]}) if protocol == 'responses' else None

    def recipe(request):
        if request.get('kind') == 'limits':
            # Injectable scaled test limits exercise real I/O without a 3m wait.
            return {'header_timeout': .06, 'idle_timeout': .06,
                    'total_timeout': 1.0 if mode == 'total' else 20.0}
        return decide(request)

    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control):
            execution_control.model_call_routed(
                snapshot_ref='crp://session/test/turn-model-routing-snapshot-v1/frozen',
                snapshot_revision='a' * 64, prompt_cache_scope_identity='b' * 64,
                provider='openai', model='gpt-5.4-mini', execution_location='local_loopback')
            execution_control.model_call_started(provider='openai', model='gpt-5.4-mini')

            class Lease:
                def finish(self, *_args, **_kwargs):
                    pass

            def authorize(*_args):
                execution_control.checkpoint()
                return Lease()

            gateway = LiteLLMCompletionGateway(provider='openai', model='gpt-5.4-mini',
                base_url=provider.base, api_key='synthetic-private-test',
                completion_fn=ResponsesCompletion(client=client) if client is not None else None,
                egress_guard=authorize,
                **({'capabilities': ModelCapabilities(
                    structured_modes=('prompt',))} if native_capabilities else {}))
            if client is None:
                actual = gateway._completion

                def observed(**kwargs):
                    result = actual(**kwargs)
                    responses.append(result.completion_stream.response)
                    return result

                gateway._completion = observed

            def retried(info):
                terminals = [event for event in runtime.events_after(request['turn_id'])
                    if event['type'] == 'model.attempt.terminal']
                assert len(terminals) == info['attempt']
                assert all(response.is_closed for response in responses)
                retries.append(info)

            retry = ModelRetryControl(policy=recipe, checkpoint=execution_control.checkpoint,
                on_retry=retried, jitter=lambda: 0,
                clock=lambda: monotonic() + fake_elapsed[0],
                wait=lambda delay: fake_elapsed.__setitem__(0, fake_elapsed[0] + delay))
            if protocol == 'responses' or observation is not None:
                def owned():
                    result = httpx.Client(transport=RoutedTransport(
                        provider.base if protocol == 'responses' else None, observation), trust_env=False,
                        event_hooks={'response': [responses.append]})
                    if owned_clients is not None:
                        owned_clients.append(result)
                    return result
                retry.owned_client_factory = owned
            try:
                options = dict(response_model=Answer, wire_attempt_sink=execution_control,
                    timeout=20, retry_control=retry)
                if streaming:
                    result, usage = gateway.stream_structured_with_usage(
                        [{'role': 'user', 'content': 'synthetic'}], on_delta=deltas.append,
                        validate_current=execution_control.checkpoint, **options)
                else:
                    result, usage, _ = gateway.complete_structured_with_usage(
                        [{'role': 'user', 'content': 'synthetic'}], retries=0, max_wire_attempts=1, **options)
                    deltas.append(result.answer)
            except BaseException as error:
                print('TRANSPORT_DIAGNOSTIC', type(error).__name__,
                    type(error.__cause__).__name__, 'counts=', retry.counts,
                    'calls=', provider.calls, 'closed=', [r.is_closed for r in responses])
                raise
            execution_control.model_call_completed(usage=usage)
            return {'type': 'complete', 'summary': result.answer}

    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store)
    request = json.loads((__import__('pathlib').Path(__file__).resolve().parents[4] /
        'core-contracts/ai/fixtures/turn-request/valid-project-answer.json').read_text(encoding='utf-8'))
    try:
        started = monotonic()
        receipt = runtime.submit_turn(request)
        if observation is not None:
            observation['elapsed_seconds'] = monotonic() - started
            observation['total_seconds'] = 1.0 if mode == 'total' else 20.0
        terminals = [store.get(event['data']['receipt_ref']) for event in runtime.events_after(receipt.turn_id)
            if event['type'] == 'model.attempt.terminal']
        return receipt, provider.calls, retries, deltas, terminals, responses
    finally:
        if client is not None:
            client.close()
        provider.close()


@pytest.mark.parametrize('protocol', ['chat', 'responses'])
@pytest.mark.parametrize('mode', ['header', 'idle'])
def test_header_and_byte_idle_close_before_distinct_retry(tmp_path, protocol, mode):
    receipt, calls, retries, deltas, terminals, responses = run_http(tmp_path, mode, protocol)
    assert receipt.status == 'completed'
    assert calls == len(terminals) == 2
    assert len(retries) == 1
    expected = 'header' if mode == 'header' else ('thinking_stall' if protocol == 'chat' else 'before_output')
    assert retries[0]['budget'] == expected
    assert deltas == ['done']
    assert responses and all(response.is_closed for response in responses)


@pytest.mark.parametrize('protocol', ['chat', 'responses'])
def test_raw_heartbeat_bytes_reset_idle_without_model_delta(tmp_path, protocol):
    receipt, calls, retries, deltas, terminals, responses = run_http(tmp_path, 'heartbeat', protocol)
    assert receipt.status == 'completed'
    assert calls == len(terminals) == 1 and retries == []
    assert deltas == ['done'] and all(response.is_closed for response in responses)


@pytest.mark.parametrize('protocol', ['chat', 'responses'])
def test_raw_heartbeat_cannot_extend_total_deadline(tmp_path, protocol):
    observation = {}
    receipt, calls, retries, deltas, terminals, responses = run_http(tmp_path, 'total', protocol,
        observation=observation)
    assert receipt.status == 'failed'
    assert calls == len(terminals) == 1 and retries == [] and deltas == []
    assert responses and all(response.is_closed for response in responses)
    assert observation['raw_bytes'] > 0 and observation['body_closed']
    assert observation['elapsed_seconds'] >= observation['total_seconds']
    assert [row['status'] for row in terminals] == ['failed_transport']
    print('HEARTBEAT_TOTAL_PROOF', {'protocol': protocol, **observation,
        'provider_calls': calls, 'terminal_status': terminals[0]['status']})


@pytest.mark.parametrize('protocol', ['chat', 'responses'])
def test_body_started_stall_does_not_replay(tmp_path, protocol):
    receipt, calls, retries, deltas, terminals, responses = run_http(tmp_path, 'body', protocol)
    assert receipt.status == 'failed'
    assert calls == len(terminals) == 1 and retries == []
    assert deltas == ['partial\n\n']
    assert responses and all(response.is_closed for response in responses)
