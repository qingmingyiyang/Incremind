"""Real SDK, local sockets and SQLite model-attempt ownership."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

import httpx
import pytest

from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway, ModelRetryControl
from backend.memory_app.v2.policies.retry import decide
from core.ai_kernel import SQLiteAITurnStore, SynchronousAIRuntime, ScopedCapabilityRegistry
from tests.backend.unit.llm.test_model_transport_watchdog import Answer


class OwnedProvider:
    def __init__(self, mode):
        self.calls, self.written, self.closed = [], [], []
        owner = self
        body = json.dumps({'id': 'synthetic', 'object': 'chat.completion',
            'model': 'gpt-5.4-mini', 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': '{"answer":"done"}'}}],
            'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}}).encode()
        self.body_size = len(body)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                owner.calls.append((self.path, self.client_address))
                call = len(owner.calls)
                owner.written.append(0)
                try:
                    if mode == 'error' and call == 1:
                        self.send_response(503)
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return
                    if mode == 'redirect' and call == 1:
                        self.send_response(307)
                        self.send_header('Location', '/v1/final')
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return
                    trickle = (mode == 'headers' and call == 1) or (mode == 'redirect' and call == 2)
                    if trickle:
                        head = (b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                            + b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n')
                        for byte in head:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            sleep(.005)
                    else:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Content-Length', str(len(body)))
                        self.end_headers()
                    if mode == 'idle' and call == 1:
                        self.wfile.write(body[:1])
                        self.wfile.flush()
                        owner.written[call - 1] += 1
                        sleep(.18)
                        self.wfile.write(body[1:])
                        owner.written[call - 1] += len(body) - 1
                    elif mode == 'total' and call == 1:
                        for index in range(0, len(body), 2):
                            part = body[index:index + 2]
                            self.wfile.write(part)
                            self.wfile.flush()
                            owner.written[call - 1] += len(part)
                            sleep(.015)
                    else:
                        self.wfile.write(body)
                        owner.written[call - 1] += len(body)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    owner.closed.append(call)
                    self.close_connection = True

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


def real_owned_call(tmp_path, provider, *, clients=None, factory=None, total=20,
                    retry_enabled=True, turn_id=None):
    store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    retries, output, controls = [], [], []
    fake_elapsed = [0.0]

    def policy(request):
        if request['kind'] == 'limits':
            return {'header_timeout': .5 if provider.mode in {'idle', 'total'} else .07,
                'idle_timeout': .04, 'total_timeout': total}
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
                    return

            def authorize(*_args):
                execution_control.checkpoint()
                return Lease()

            def retried(info):
                terminals = [event for event in runtime.events_after(request['turn_id'])
                    if event['type'] == 'model.attempt.terminal']
                assert len(terminals) == info['attempt']
                if clients is not None:
                    assert all(client.is_closed for client in clients['owned'])
                retries.append(info)

            control = ModelRetryControl(policy=policy, checkpoint=execution_control.checkpoint,
                on_retry=retried, jitter=lambda: 0,
                clock=lambda: monotonic() + fake_elapsed[0],
                wait=lambda delay: fake_elapsed.__setitem__(0, fake_elapsed[0] + delay))
            # Explicit transport owner input; no policy/provider is mocked.
            control.owned_client_factory = factory
            controls.append(control)
            gateway = LiteLLMCompletionGateway(provider='openai', model='gpt-5.4-mini',
                base_url=provider.base, api_key='synthetic-private-test', egress_guard=authorize)
            try:
                result, usage, _ = gateway.complete_structured_with_usage(
                    [{'role': 'user', 'content': 'synthetic'}], response_model=Answer,
                    retries=0, max_wire_attempts=1, wire_attempt_sink=execution_control,
                    timeout=20, **({'retry_control': control} if retry_enabled else {}))
            except Exception as error:
                from datetime import datetime, timezone
                from backend.shared.llm.litellm_gateway import _model_transport_failure
                print('OWNED_DIAGNOSTIC', type(error).__name__,
                    _model_transport_failure(error, datetime.now(timezone.utc))[0],
                    control.counts, len(provider.calls))
                raise
            output.append(result.answer)
            execution_control.model_call_completed(usage=usage)
            return {'type': 'complete', 'summary': result.answer}

    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store)
    request = json.loads((Path(__file__).resolve().parents[4] /
        'core-contracts/ai/fixtures/turn-request/valid-project-answer.json').read_text(encoding='utf-8'))
    if turn_id is not None:
        request['turn_id'] = turn_id
    receipt = runtime.submit_turn(request)
    terminals = [store.get(event['data']['receipt_ref']) for event in runtime.events_after(receipt.turn_id)
        if event['type'] == 'model.attempt.terminal']
    return receipt, retries, output, terminals, controls


@pytest.fixture
def observed_factory(monkeypatch):
    from litellm.llms.openai.openai import OpenAIChatCompletion
    clients = {'all': [], 'owned': [], 'responses': []}
    original = OpenAIChatCompletion._get_sync_http_client

    def record():
        client = original()
        clients['all'].append(client)
        return client

    def owned():
        client = record()
        client.event_hooks['response'].append(clients['responses'].append)
        clients['owned'].append(client)
        return client

    clients['factory'] = owned

    monkeypatch.setattr(OpenAIChatCompletion, '_get_sync_http_client', staticmethod(record))
    yield clients
    # Only the fixture closes its factory-original cached HTTP client.
    for client in clients['all']:
        if not client.is_closed:
            client.close()


@pytest.mark.parametrize('mode,budget', [('idle', 'before_output'), ('headers', 'header'),
                                      ('redirect', 'header')])
def test_real_sdk_nonstream_and_cached_headers_close_owned_attempt_before_retry(tmp_path, observed_factory, mode, budget):
    provider = OwnedProvider(mode)
    provider.mode = mode
    try:
        receipt, retries, output, terminals, _ = real_owned_call(tmp_path, provider,
            clients=observed_factory, factory=observed_factory['factory'])
        assert receipt.status == 'completed' and output == ['done']
        assert len(retries) == 1 and retries[0]['budget'] == budget
        assert [row['status'] for row in terminals] == ['failed_transport', 'succeeded']
        assert len({row['attempt_id'] for row in terminals}) == 2
        assert len(observed_factory['owned']) == 2
        assert all(client.is_closed for client in observed_factory['owned'])
        assert all(response.is_closed for response in observed_factory['responses'])
        for client in observed_factory['owned']:
            attempt = client.event_hooks['request'][0].__self__
            assert attempt.closed and not attempt.total.is_alive()
            assert attempt.header is None or not attempt.header.is_alive()
        assert all(client.is_closed is False for client in observed_factory['all']
            if client not in observed_factory['owned'])
        if mode == 'redirect':
            assert len(provider.calls) == 3
            assert provider.calls[0][1] == provider.calls[1][1]
            assert provider.calls[2][1] != provider.calls[1][1]
        else:
            assert len(provider.calls) == 2
    finally:
        provider.close()


def test_real_sdk_nonstream_raw_bytes_cannot_extend_absolute_total(tmp_path, observed_factory):
    warm = OwnedProvider('healthy')
    warm.mode = 'healthy'
    try:
        warm_receipt, _, warm_output, warm_terminals, _ = real_owned_call(tmp_path / 'warm', warm,
            retry_enabled=False, turn_id='turn-owned-total-warm')
        assert warm_receipt.status == 'completed' and warm_output == ['done']
        assert len(warm_terminals) == len(warm.calls) == 1
        print('OWNED_TOTAL_WARM', {'turn_id': warm_receipt.turn_id,
            'provider_requests': len(warm.calls),
            'terminals': [{'status': row['status'], 'usage': row.get('usage')} for row in warm_terminals]})
    finally:
        warm.close()
    provider = OwnedProvider('total')
    provider.mode = 'total'
    try:
        receipt, retries, output, terminals, _ = real_owned_call(tmp_path, provider,
            clients=observed_factory, factory=observed_factory['factory'], total=1,
            turn_id='turn-owned-total-target')
        assert receipt.status == 'failed' and output == [] and retries == []
        assert len(terminals) == len(provider.calls) == 1
        assert terminals[0]['status'] == 'failed_transport'
        assert 0 < provider.written[0] < provider.body_size
        assert len(observed_factory['owned']) == 1 and observed_factory['owned'][0].is_closed
        assert len(observed_factory['responses']) == 1 and observed_factory['responses'][0].is_closed
        attempt = observed_factory['owned'][0].event_hooks['request'][0].__self__
        assert attempt.closed and not attempt.total.is_alive()
        assert attempt.header is not None and not attempt.header.is_alive()
        print('OWNED_TOTAL_TARGET', {'turn_id': receipt.turn_id,
            'provider_requests': len(provider.calls), 'retry_count': len(retries),
            'written_bytes': provider.written[0], 'complete_bytes': provider.body_size,
            'response_closed': observed_factory['responses'][0].is_closed,
            'client_closed': observed_factory['owned'][0].is_closed,
            'timers_joined': not attempt.header.is_alive() and not attempt.total.is_alive(),
            'terminals': [{'status': row['status'], 'usage': row.get('usage')} for row in terminals]})
        assert all(client.is_closed is False for client in observed_factory['all']
            if client not in observed_factory['owned'])
    finally:
        provider.close()


def test_real_sdk_default_none_keeps_original_cached_client_owner(tmp_path, observed_factory):
    provider = OwnedProvider('idle')
    provider.mode = 'idle'
    try:
        receipt, retries, output, terminals, _ = real_owned_call(tmp_path, provider, retry_enabled=False)
        assert receipt.status == 'completed' and output == ['done'] and retries == []
        assert len(terminals) == len(provider.calls) == len(observed_factory['all']) == 1
        assert observed_factory['owned'] == []
        assert observed_factory['all'][0].is_closed is False
    finally:
        provider.close()


def test_owned_client_close_failure_prevents_any_successor_wire(tmp_path):
    provider = OwnedProvider('error')
    provider.mode = 'error'
    clients = []

    class FailingClose(httpx.Client):
        close_calls = 0

        def close(self):
            self.close_calls += 1
            super().close()
            raise OSError('synthetic owned close failure')

    def factory():
        client = FailingClose()
        clients.append(client)
        return client

    try:
        receipt, retries, output, terminals, _ = real_owned_call(tmp_path, provider, factory=factory)
        assert receipt.status == 'failed' and retries == [] and output == []
        assert len(provider.calls) == len(terminals) == len(clients) == 1
        assert clients[0].close_calls == 1 and clients[0].is_closed
        assert terminals[0]['status'] == 'failed_transport'
    finally:
        provider.close()


def test_borrowed_shared_client_without_owned_factory_is_not_mutated_or_dispatched(tmp_path, monkeypatch):
    import litellm
    provider = OwnedProvider('idle')
    provider.mode = 'idle'
    with httpx.Client() as shared:
        hooks = {kind: list(values) for kind, values in shared.event_hooks.items()}
        monkeypatch.setattr(litellm, 'client_session', shared)
        try:
            receipt, retries, output, terminals, _ = real_owned_call(tmp_path, provider)
            assert receipt.status == 'failed' and output == retries == []
            assert provider.calls == [] and len(terminals) == 1
            assert shared.is_closed is False and shared.event_hooks == hooks
        finally:
            provider.close()


def test_owned_factory_cannot_return_the_borrowed_shared_client(tmp_path, monkeypatch):
    import litellm
    provider = OwnedProvider('idle')
    provider.mode = 'idle'
    with httpx.Client() as shared:
        hooks = {kind: list(values) for kind, values in shared.event_hooks.items()}
        monkeypatch.setattr(litellm, 'client_session', shared)
        try:
            receipt, retries, output, terminals, _ = real_owned_call(tmp_path, provider, factory=lambda: shared)
            assert receipt.status == 'failed' and output == retries == []
            assert provider.calls == [] and len(terminals) == 1
            assert shared.is_closed is False and shared.event_hooks == hooks
        finally:
            provider.close()


def test_existing_public_sdk_stream_response_retains_its_owned_close(tmp_path):
    from tests.backend.unit.llm.test_model_transport_watchdog import run_http
    receipt, calls, retries, deltas, terminals, responses = run_http(tmp_path, 'header', 'chat')
    assert receipt.status == 'completed'
    assert calls == len(terminals) == 2 and len(retries) == 1
    assert deltas == ['done'] and responses and all(response.is_closed for response in responses)


def test_native_responses_uses_only_explicit_attempt_owned_factory(tmp_path):
    from tests.backend.unit.llm.test_model_transport_watchdog import run_http
    clients = []
    receipt, calls, retries, deltas, terminals, responses = run_http(tmp_path, 'header', 'responses',
        owned_clients=clients)
    assert receipt.status == 'completed'
    assert calls == len(terminals) == len(clients) == 2 and len(retries) == 1
    assert deltas == ['done'] and all(response.is_closed for response in responses)
    assert all(client.is_closed for client in clients)
