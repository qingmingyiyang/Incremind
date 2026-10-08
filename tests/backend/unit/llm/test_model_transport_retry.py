from __future__ import annotations

import json
import asyncio
import ssl
from pathlib import Path
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from pydantic import BaseModel

from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway, ModelRetryControl
from core.ai_kernel.registry import ScopedCapabilityRegistry
from core.ai_kernel.recovery import classify_recovery
from core.ai_kernel.runtime import SynchronousAIRuntime
from core.ai_kernel.sqlite_store import SQLiteAITurnStore


class ProviderError(RuntimeError):
    status_code = 503


def test_checkpoint_crossing_backoff_wakeup_never_sleeps_a_negative_interval(monkeypatch):
    from backend.shared.llm import litellm_gateway
    from backend.memory_app.v2.policies.retry import decide
    elapsed, sleeps = [0.0], []
    real_sleep = litellm_gateway.sleep

    def checkpoint():
        elapsed[0] += 0.75

    def observed_sleep(duration):
        sleeps.append(duration)
        real_sleep(duration)

    monkeypatch.setattr(litellm_gateway, 'sleep', observed_sleep)
    control = ModelRetryControl(policy=decide, checkpoint=checkpoint,
        clock=lambda: elapsed[0], jitter=lambda: 0)
    control.start(10)
    error = ProviderError('synthetic')
    control.provider_failed(error)
    assert control.retry(error) is True
    assert all(duration >= 0 for duration in sleeps)
    assert control.counts == {'before_output': 1}


class Provider:
    def __init__(self, *, permanent=False, error=None):
        self.calls = 0
        self.permanent = permanent
        self.error = error or ProviderError('temporary overload')

    def __call__(self, **request):
        self.calls += 1
        assert request['max_retries'] == 0
        if self.calls == 1 or self.permanent:
            raise self.error
        return {'choices': [{'message': {'content': 'done'}}],
                'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}}


class Answer(BaseModel):
    answer: str


class Stream:
    def __init__(self, chunks, error=None, *, fail_close=False):
        self.chunks = iter(chunks)
        self.error = error
        self.closed = False
        self.fail_close = fail_close

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.chunks)
        except StopIteration:
            if self.error is not None:
                raise self.error
            raise

    def close(self):
        if self.fail_close:
            raise OSError('transport did not close')
        self.closed = True


class StreamingProvider:
    def __init__(self, *, phase='thinking', failures=1, fail_close=False, error=None,
                 early_eof=False):
        self.calls = 0
        self.streams = []
        self.phase, self.failures, self.fail_close = phase, failures, fail_close
        self.error = error or ProviderError('temporary stream failure')
        self.early_eof = early_eof

    def __call__(self, **request):
        if self.streams:
            assert self.streams[-1].closed
        self.calls += 1
        assert request['max_retries'] == 0
        if self.calls <= self.failures:
            if self.phase == 'thinking':
                delta = {'reasoning_content': 'synthetic thinking'}
            elif self.phase == 'tool':
                delta = {'tool_calls': [{'index': 0, 'function': {'name': 'fake_tool'}}]}
            else:
                delta = {'content': '{"answer":"first paragraph\\n\\n'}
            stream = Stream([{'choices': [{'delta': delta}],
                              'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}}],
                            None if self.early_eof else self.error, fail_close=self.fail_close)
        else:
            stream = Stream([{'choices': [{'delta': {'content': '{"answer":"done"}'}}]},
                             {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
                             {'choices': [], 'usage': {'prompt_tokens': 4, 'completion_tokens': 2,
                                                       'total_tokens': 6}}])
        self.streams.append(stream)
        return stream


class Lease:
    def __init__(self, finishes):
        self.finishes = finishes

    def finish(self, status, *, error_code=None):
        self.finishes.append(status)


class Planner:
    def __init__(self, provider, *, retry=True, streaming=False, timeout=10, checkpoint=None,
                 on_retry=None, retry_policy=None):
        self.provider = provider
        self.authorizations = []
        self.finishes = []
        self.retry = retry
        self.time = 0.0
        self.waits = []
        self.streaming, self.timeout, self.validate = streaming, timeout, checkpoint
        self.on_retry, self.retry_policy = on_retry, retry_policy
        self.deltas = []

    def wait(self, delay):
        self.waits.append(delay)
        self.time += delay

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        control = execution_control
        assert control is not None
        control.model_call_routed(
            snapshot_ref=f"crp://session/{request['turn_id']}/turn-model-routing-snapshot-v1/frozen",
            snapshot_revision='a' * 64, prompt_cache_scope_identity='b' * 64,
            provider='openai', model='gpt-5.4-mini', execution_location='local_loopback',
        )
        control.model_call_started(provider='openai', model='gpt-5.4-mini')

        def authorize(*args):
            control.checkpoint()
            self.authorizations.append(args)
            return Lease(self.finishes)

        local_http = hasattr(self.provider, 'base_url')
        gateway = LiteLLMCompletionGateway(
            provider='openai', model='gpt-5.4-mini', base_url='http://127.0.0.1:9999/v1',
            api_key=None if local_http else 'synthetic-test-key', anonymous=local_http,
            completion_fn=None if local_http else self.provider,
            acompletion_fn=self.provider, egress_guard=authorize,
        )
        if local_http:
            gateway._base_url = self.provider.base_url
        retry_control = self.retry_control(control) if self.retry else None
        if self.streaming:
            result, usage = gateway.stream_structured_with_usage(
                [{'role': 'user', 'content': 'synthetic question'}], response_model=Answer,
                validate_current=control.checkpoint, on_delta=self.deltas.append,
                wire_attempt_sink=control, timeout=self.timeout, retry_control=retry_control)
            answer = result.answer
        else:
            answer, usage, _ = gateway.complete_text_with_usage(
                [{'role': 'user', 'content': 'synthetic question'}],
                wire_attempt_sink=control, timeout=self.timeout, retry_control=retry_control)
        assert answer == 'done'
        control.model_call_completed(usage=usage)
        return {'type': 'complete', 'summary': answer}

    def retry_control(self, control):
        from backend.memory_app.v2.policies.retry import decide
        def checkpoint():
            control.checkpoint()
            if self.validate is not None:
                self.validate()
        self.last_retry = ModelRetryControl(policy=self.retry_policy or decide, checkpoint=checkpoint,
                                           clock=lambda: self.time, wait=self.wait, jitter=lambda: 0,
                                           on_retry=self.on_retry)
        return self.last_retry


def runtime_at(tmp_path, provider, **options):
    store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    planner = Planner(provider, **options)
    runtime = SynchronousAIRuntime(planner=planner, registry=ScopedCapabilityRegistry(),
                                   events=store, payloads=store, state=store)
    request = json.loads((Path(__file__).resolve().parents[4] / 'core-contracts/ai/fixtures/'
                          'turn-request/valid-project-answer.json').read_text(encoding='utf-8'))
    return runtime, store, planner, request


def attempt_receipts(runtime, store, turn_id):
    return [store.get(event['data']['receipt_ref']) for event in runtime.events_after(turn_id)
            if event['type'] == 'model.attempt.terminal']


def test_current_call_retries_with_distinct_durable_attempts(tmp_path):
    provider = Provider()
    runtime, store, planner, request = runtime_at(tmp_path, provider)
    receipt = runtime.submit_turn(request)

    assert receipt.status == 'completed'
    assert provider.calls == 2
    assert len(planner.authorizations) == 2
    assert planner.finishes == ['failed', 'succeeded']
    assert planner.waits == [1]
    terminals = [store.get(event['data']['receipt_ref']) for event in
                 runtime.events_after(receipt.turn_id) if event['type'] == 'model.attempt.terminal']
    assert [item['attempt_number'] for item in terminals] == [1, 2]
    assert len({item['attempt_id'] for item in terminals}) == 2
    assert {item['turn_id'] for item in terminals} == {receipt.turn_id}
    assert len({item['model_request_id'] for item in terminals}) == 1
    assert [item['status'] for item in terminals] == ['failed_transport', 'succeeded']
    assert terminals[0]['usage_status'] == 'unavailable'
    assert terminals[1]['usage'] == {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}
    with sqlite3.connect(tmp_path / 'turns.sqlite3') as connection:
        states = connection.execute('SELECT state FROM effect ORDER BY rowid').fetchall()
    assert states == [('UNKNOWN',), ('SETTLED_OK',)]


@pytest.mark.parametrize('callback_error', [None, RuntimeError, PermissionError])
def test_retry_display_is_best_effort_after_real_close_and_terminal(tmp_path, callback_error):
    provider = StreamingProvider()
    order, notices, snapshots = [], [], []

    def external_provider(**kwargs):
        order.append('wire')
        stream = provider(**kwargs)
        original_close = stream.close

        def observed_close():
            original_close()
            order.append('close')

        stream.close = observed_close
        return stream

    def notified(info):
        order.append('notify')
        notices.append(dict(info))
        snapshots.append((provider.calls, all(s.closed for s in provider.streams),
            [r['status'] for r in attempt_receipts(runtime, store, request['turn_id'])]))
        if callback_error is not None:
            raise callback_error('synthetic display unavailable')

    runtime, store, planner, request = runtime_at(tmp_path, external_provider,
        streaming=True, on_retry=notified)
    result = runtime.submit_turn(request)
    assert result.status == 'completed'
    assert order == ['wire', 'close', 'notify', 'wire', 'close']
    assert snapshots == [(1, True, ['failed_transport'])]
    assert notices == [{'attempt': 1, 'delay': 1.0, 'budget': 'thinking_error',
                        'reason': 'server', 'used': 1, 'limit': 2}]
    assert provider.calls == 2
    assert planner.waits == [1]
    assert len(planner.authorizations) == 2
    assert planner.finishes == ['failed', 'succeeded']
    attempts = attempt_receipts(runtime, store, request['turn_id'])
    assert [r['status'] for r in attempts] == ['failed_transport', 'succeeded']
    assert len({r['attempt_id'] for r in attempts}) == 2
    with sqlite3.connect(tmp_path / 'turns.sqlite3') as connection:
        assert connection.execute('SELECT state FROM effect ORDER BY rowid').fetchall() == [
            ('UNKNOWN',), ('SETTLED_OK',)]


@pytest.mark.parametrize('callback_error', [asyncio.CancelledError, SystemExit])
def test_retry_display_baseexception_stops_before_another_wire(tmp_path, callback_error):
    provider = StreamingProvider()
    notices = []

    def notified(info):
        notices.append(dict(info))
        raise callback_error('synthetic cancellation')

    runtime, store, planner, request = runtime_at(tmp_path, provider,
        streaming=True, on_retry=notified)
    with pytest.raises(callback_error):
        runtime.submit_turn(request)
    assert provider.calls == 1
    assert provider.streams[0].closed
    assert len(notices) == 1
    assert planner.waits == []
    assert len(planner.authorizations) == 1
    assert [r['status'] for r in attempt_receipts(runtime, store, request['turn_id'])] == [
        'failed_transport']
    with sqlite3.connect(tmp_path / 'turns.sqlite3') as connection:
        assert connection.execute('SELECT state FROM effect').fetchall() == [('UNKNOWN',)]


@pytest.mark.parametrize('unsafe', [
    {'reason': 'unsafe-provider-text', 'used': 1, 'limit': 2},
    {'reason': 'connection', 'used': 1, 'limit': 2},
    {'reason': 'server', 'used': True, 'limit': 2},
    {'reason': 'server', 'used': 2, 'limit': 2},
    {'reason': 'server', 'used': 1, 'limit': float('nan')},
    {'reason': 'server', 'used': 1, 'limit': 0},
    {'reason': 'server', 'used': 1, 'limit': 2, 'body': 'synthetic private data'},
    None,
])
def test_invalid_display_metadata_cannot_leak_or_block_real_retry(tmp_path, unsafe):
    from backend.memory_app.v2.policies.retry import decide
    provider, notices = StreamingProvider(), []

    def selected_policy(value):
        if value.get('kind') == 'retry_display':
            if unsafe is None:
                raise RuntimeError('synthetic display recipe unavailable')
            return unsafe
        return decide(value)

    runtime, store, planner, request = runtime_at(tmp_path, provider,
        streaming=True, retry_policy=selected_policy, on_retry=notices.append)
    assert runtime.submit_turn(request).status == 'completed'
    assert notices == []
    assert provider.calls == 2
    assert all(s.closed for s in provider.streams)
    assert planner.waits == [1]
    assert [r['status'] for r in attempt_receipts(runtime, store, request['turn_id'])] == [
        'failed_transport', 'succeeded']


def test_display_attempt_is_global_but_used_is_the_selected_category():
    from backend.memory_app.v2.policies import get
    from backend.shared.llm.model_transport import ModelTransportError
    notices = []
    control = ModelRetryControl(policy=get('retry', version='@1'), checkpoint=lambda: None,
        on_retry=notices.append, wait=lambda delay: None, jitter=lambda: 0)
    control.start(600)
    failures = [(ProviderError('synthetic'), 'before_output'),
                (ModelTransportError('header_timeout'), 'thinking'),
                (ProviderError('synthetic'), 'thinking')]
    for error, phase in failures:
        control.phase = phase
        control.provider_failed(error)
        assert control.retry(error) is True
    assert notices == [
        {'attempt': 1, 'delay': 1.0, 'budget': 'before_output',
         'reason': 'server', 'used': 1, 'limit': 10},
        {'attempt': 2, 'delay': 1.0, 'budget': 'header',
         'reason': 'header_timeout', 'used': 1, 'limit': 1},
        {'attempt': 3, 'delay': 2.0, 'budget': 'thinking_error',
         'reason': 'server', 'used': 2, 'limit': 2}]
    assert control.counts == {'before_output': 1, 'header': 1, 'thinking_error': 2}


def test_display_disabled_does_not_call_an_extra_policy_operation(tmp_path):
    from backend.memory_app.v2.policies import get
    selected, queries = get('retry', version='@1'), []

    def observed_policy(value):
        queries.append(value['kind'])
        return selected(value)

    provider = StreamingProvider()
    runtime, store, planner, request = runtime_at(tmp_path, provider,
        streaming=True, retry_policy=observed_policy)
    assert runtime.submit_turn(request).status == 'completed'
    assert queries == ['limits', 'server']
    assert provider.calls == 2
    assert planner.waits == [1]
    assert [r['status'] for r in attempt_receipts(runtime, store, request['turn_id'])] == [
        'failed_transport', 'succeeded']


def test_display_failure_does_not_swallow_actual_source_revocation(tmp_path):
    provider, revoked, notices = StreamingProvider(), [], []

    def current_source():
        if revoked:
            raise PermissionError('synthetic actual source revoked')

    def unavailable_display(info):
        notices.append(dict(info))
        revoked.append(True)
        raise PermissionError('synthetic display unavailable')

    runtime, store, planner, request = runtime_at(tmp_path, provider, streaming=True,
        checkpoint=current_source, on_retry=unavailable_display)
    assert runtime.submit_turn(request).status == 'failed'
    assert notices == [{'attempt': 1, 'delay': 1.0, 'budget': 'thinking_error',
                        'reason': 'server', 'used': 1, 'limit': 2}]
    assert provider.calls == 1
    assert provider.streams[0].closed
    assert planner.waits == [1]
    assert len(planner.authorizations) == 1
    assert [r['status'] for r in attempt_receipts(runtime, store, request['turn_id'])] == [
        'failed_transport']


def test_old_unknown_reopens_without_another_provider_wire(tmp_path):
    provider = Provider(permanent=True)
    runtime, store, planner, request = runtime_at(tmp_path, provider, retry=False)
    receipt = runtime.submit_turn(request)
    assert receipt.status == 'failed'
    assert provider.calls == 1
    reopened = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    recovered = SynchronousAIRuntime(planner=planner, registry=ScopedCapabilityRegistry(),
                                     events=reopened, payloads=reopened, state=reopened)
    replay = recovered.submit_turn(request)
    assert recovered.run_accepted_turn(receipt.turn_id).status == 'failed'
    assert replay.replayed
    assert provider.calls == 1
    with sqlite3.connect(tmp_path / 'turns.sqlite3') as connection:
        assert connection.execute('SELECT state FROM effect').fetchall() == [('UNKNOWN',)]


def test_crash_after_failed_attempt_is_quarantined_without_new_wire(tmp_path):
    provider = Provider()
    def stopped_process():
        if provider.calls:
            raise SystemExit('synthetic process stopped before backoff')
    runtime, store, planner, request = runtime_at(tmp_path, provider, checkpoint=stopped_process)
    with pytest.raises(SystemExit):
        runtime.submit_turn(request)
    reopened = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    events = tuple(reopened.events_after(request['turn_id']))
    decision = classify_recovery(request['turn_id'], 1, events, payload_loader=reopened.get)
    assert decision.disposition == 'quarantine'
    assert decision.reason_code == 'ai.recovery_model_incomplete'
    assert events[-1]['type'] == 'model.attempt.terminal'
    assert reopened.get(events[-1]['data']['receipt_ref'])['status'] == 'failed_transport'
    assert provider.calls == 1
    assert planner.waits == []
    with sqlite3.connect(tmp_path / 'turns.sqlite3') as connection:
        assert connection.execute('SELECT state FROM effect').fetchall() == [('UNKNOWN',)]


def test_thinking_failure_usage_is_kept_per_attempt_and_accumulated(tmp_path):
    provider = StreamingProvider()
    runtime, store, planner, request = runtime_at(tmp_path, provider, streaming=True)
    receipt = runtime.submit_turn(request)
    assert receipt.status == 'completed'
    assert provider.calls == 2
    assert planner.deltas == ['done']
    assert all(stream.closed for stream in provider.streams)
    attempts = attempt_receipts(runtime, store, receipt.turn_id)
    assert [item['status'] for item in attempts] == ['failed_transport', 'succeeded']
    assert [item['usage'] for item in attempts] == [
        {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6},
        {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}]
    model = next(event for event in runtime.events_after(receipt.turn_id) if event['type'] == 'model.completed')
    assert store.get(model['data']['receipt_ref'])['usage'] == {
        'input_tokens': 8, 'output_tokens': 4, 'total_tokens': 12}


@pytest.mark.parametrize('phase,fail_close,expected', [('body', False, 1), ('tool', False, 1),
                                                     ('thinking', True, 1),
                                                     ('thinking', False, 3)])
def test_stream_body_closure_and_thinking_budget_fence(tmp_path, phase, fail_close, expected):
    provider = StreamingProvider(phase=phase, failures=20, fail_close=fail_close)
    runtime, store, planner, request = runtime_at(tmp_path, provider, streaming=True, timeout=600)
    receipt = runtime.submit_turn(request)
    assert receipt.status == 'failed'
    assert provider.calls == expected
    assert len(attempt_receipts(runtime, store, receipt.turn_id)) == expected
    assert len(planner.authorizations) == expected
    if phase == 'body':
        assert planner.deltas == ['first paragraph\n\n']
    if phase == 'tool':
        assert planner.deltas == []
    if expected == 3:
        assert planner.last_retry.counts == {'thinking_error': 2}
        assert planner.waits == [1, 2]


def test_revoked_current_check_prevents_wait_and_second_wire(tmp_path):
    provider = Provider()
    def current():
        if provider.calls:
            raise PermissionError('current source became private')
    runtime, store, planner, request = runtime_at(tmp_path, provider, checkpoint=current)
    assert runtime.submit_turn(request).status == 'failed'
    assert provider.calls == 1
    assert planner.waits == []
    assert len(planner.authorizations) == 1


def test_terminal_sql_failure_prevents_a_second_wire(tmp_path):
    provider = Provider()
    runtime, store, planner, request = runtime_at(tmp_path, provider)
    with sqlite3.connect(tmp_path / 'turns.sqlite3') as connection:
        connection.execute("CREATE TRIGGER reject_wire_receipt BEFORE INSERT ON ai_turn_payloads "
                           "WHEN NEW.kind='model-wire-attempt-receipt' BEGIN "
                           "SELECT RAISE(ABORT, 'terminal receipt unavailable'); END")
    receipt = runtime.submit_turn(request)
    assert receipt.status == 'failed'
    assert provider.calls == 1
    assert planner.waits == []
    assert attempt_receipts(runtime, store, receipt.turn_id) == []


@pytest.mark.parametrize('kind', ['quota', '401', '403', 'certificate', 'content_filter', 'rejection'])
def test_real_gateway_does_not_retry_permanent_provider_failures(tmp_path, kind):
    error = ProviderError('synthetic failure')
    if kind == 'quota':
        error.status_code = 429
        error.body = {'error': {'code': 'insufficient_quota'}}
        error.headers = {'Retry-After': '1'}
    elif kind in {'401', '403', 'rejection'}:
        error.status_code = 400 if kind == 'rejection' else int(kind)
    elif kind == 'content_filter':
        error.body = {'error': {'type': 'content_filter'}}
    else:
        error = ssl.SSLCertVerificationError('synthetic certificate failure')
    provider = Provider(error=error)
    runtime, store, planner, request = runtime_at(tmp_path, provider)
    receipt = runtime.submit_turn(request)
    assert receipt.status == 'failed'
    assert provider.calls == 1
    assert planner.waits == []
    assert len(attempt_receipts(runtime, store, receipt.turn_id)) == 1


def test_cancel_during_backoff_is_observed_before_second_wire(tmp_path):
    provider = Provider()
    runtime, store, planner, request = runtime_at(tmp_path, provider)
    def current():
        if planner.time >= 1:
            raise asyncio.CancelledError()
    planner.validate = current
    with pytest.raises(asyncio.CancelledError):
        runtime.submit_turn(request)
    assert provider.calls == 1
    assert planner.waits == [1]
    attempts = attempt_receipts(runtime, store, request['turn_id'])
    assert len(attempts) == 1
    assert attempts[0]['status'] == 'failed_transport'


def test_cancelled_stream_keeps_reported_usage_without_retry(tmp_path):
    provider = StreamingProvider(error=asyncio.CancelledError())
    runtime, store, planner, request = runtime_at(tmp_path, provider, streaming=True)
    with pytest.raises(asyncio.CancelledError):
        runtime.submit_turn(request)
    assert provider.calls == 1
    assert provider.streams[0].closed
    assert planner.waits == []
    attempts = attempt_receipts(runtime, store, request['turn_id'])
    assert len(attempts) == 1
    assert attempts[0]['status'] == 'consumer_cancelled'
    assert attempts[0]['usage'] == {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}


def test_early_stream_eof_before_body_is_not_a_success_terminal(tmp_path):
    provider = StreamingProvider(early_eof=True)
    runtime, store, planner, request = runtime_at(tmp_path, provider, streaming=True)
    receipt = runtime.submit_turn(request)
    assert receipt.status == 'completed'
    assert provider.calls == 2
    assert [item['status'] for item in attempt_receipts(runtime, store, receipt.turn_id)] == [
        'failed_transport', 'succeeded']
    assert planner.deltas == ['done']


def test_deadline_blocks_retry_after_without_extending_timeout(tmp_path):
    error = ProviderError('synthetic rate limit')
    error.status_code = 429
    error.headers = {'Retry-After': '42'}
    provider = Provider(error=error)
    runtime, store, planner, request = runtime_at(tmp_path, provider, timeout=4)
    receipt = runtime.submit_turn(request)
    assert receipt.status == 'failed'
    assert provider.calls == 1
    assert planner.waits == []
    assert len(attempt_receipts(runtime, store, receipt.turn_id)) == 1


@pytest.mark.parametrize('status,code,expected', [(503, None, 2),
                                                (429, 'insufficient_quota', 1)])
def test_real_loopback_http_retries_with_real_sqlite_attempts(tmp_path, status, code, expected):
    class Handler(BaseHTTPRequestHandler):
        calls = 0

        def log_message(self, *args):
            return

        def do_POST(self):
            type(self).calls += 1
            self.rfile.read(int(self.headers['Content-Length']))
            body = json.dumps({'error': {'code': code},
                               'choices': [{'message': {'content': 'done'}}],
                               'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}}).encode()
            self.send_response(status if self.calls == 1 else 200)
            self.send_header('Retry-After', '1')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    provider = Provider()
    provider.base_url = f'http://127.0.0.1:{server.server_port}/v1'
    try:
        runtime, store, planner, request = runtime_at(tmp_path, provider)
        receipt = runtime.submit_turn(request)
        assert receipt.status == ('completed' if expected == 2 else 'failed')
        assert Handler.calls == expected
        assert planner.finishes == (['failed', 'succeeded'] if expected == 2 else ['failed'])
        assert [item['status'] for item in attempt_receipts(runtime, store, receipt.turn_id)] == (
            ['failed_transport', 'succeeded'] if expected == 2 else ['failed_transport'])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.parametrize('kind', ['quota', 'authentication', 'certificate', 'content_filter',
                                'permission', 'privacy', 'provider_rejected'])
def test_retry_policy_rejects_permanent_errors_in_every_phase(kind):
    from backend.memory_app.v2.policies.retry import decide
    for phase in ('before_output', 'thinking', 'body', 'tool'):
        decision = decide({'kind': kind, 'phase': phase, 'counts': {}, 'remaining': 600})
        assert decision['retry'] is False


@pytest.mark.parametrize('phase,kind,budget,maximum', [
    ('before_output', 'server', 'before_output', 10),
    ('before_output', 'connection', 'before_output', 10),
    ('before_output', 'timeout', 'before_output', 10),
    ('before_output', 'stalled', 'before_output', 10),
    ('before_output', 'rate_limit', 'before_output', 10),
    ('thinking', 'server', 'thinking_error', 2),
    ('thinking', 'connection', 'thinking_error', 2),
    ('thinking', 'stalled', 'thinking_stall', 1),
    ('before_output', 'header_timeout', 'header', 1),
    ('before_output', 'malformed_stream', 'fallback', 1),
])
def test_retry_policy_budgets_are_distinct(phase, kind, budget, maximum):
    from backend.memory_app.v2.policies.retry import decide
    for used in range(maximum + 1):
        counts = {'before_output': 10, 'thinking_error': 2, 'thinking_stall': 1,
                  'header': 1, 'fallback': 1}
        counts[budget] = used
        decision = decide({'kind': kind, 'phase': phase, 'counts': counts,
                           'remaining': 600, 'jitter': 0})
        assert decision['retry'] is (used < maximum)
        if used < maximum:
            assert decision['budget'] == budget


def test_retry_policy_preserves_after_body_fence_and_server_wait():
    from backend.memory_app.v2.policies.retry import decide
    assert decide({'kind': 'server', 'phase': 'body', 'counts': {}, 'remaining': 600})['retry'] is False
    assert decide({'kind': 'connection', 'phase': 'tool', 'counts': {}, 'remaining': 600})['retry'] is False
    waits = [decide({'kind': 'server', 'phase': 'before_output',
                     'counts': {'before_output': used}, 'remaining': 600, 'jitter': 0})['delay']
             for used in range(10)]
    assert waits == [1, 2, 4, 8, 16, 32, 32, 32, 32, 32]
    assert decide({'kind': 'rate_limit', 'phase': 'before_output', 'counts': {},
                   'remaining': 600, 'retry_after': 42, 'jitter': 1})['delay'] == 42
    assert decide({'kind': 'rate_limit', 'phase': 'before_output', 'counts': {},
                   'remaining': 4, 'retry_after': 42})['retry'] is False
