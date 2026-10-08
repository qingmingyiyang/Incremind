"""真实 Responses、网关、retry 与 Core/SQLite 尝试契约，隔离外部 HTTP。

Planner、路由事实、权限端口与退避时钟为合成驱动，不覆盖真实
ModelConfiguration、SourceEgressService 或公开 Do 的装配。
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import httpx

from backend.memory_app.v2.policies import get
from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway, ModelRetryControl
from backend.shared.llm.model_capabilities import ModelCapabilities
from backend.shared.llm.openai_responses import ResponsesCompletion, ResponsesProviderError
from core.ai_kernel.registry import ScopedCapabilityRegistry
from core.ai_kernel.runtime import SynchronousAIRuntime
from core.ai_kernel.sqlite_store import SQLiteAITurnStore


def test_native_subscription_usage_limit_stops_before_second_durable_wire(tmp_path):
    calls, clients, responses, guards, finishes, observed, waits = [], [], [], [], [], [], []
    elapsed = [0.0]

    def provider_http(request):
        calls.append((request.method, request.url.path, json.loads(request.content)))
        if len(calls) == 1:
            return httpx.Response(429, headers={'Retry-After': '1'}, json={
                'error': {'code': 'subscription_sharing_usage_limit_exceeded',
                          'message': 'synthetic provider detail'},
            })
        # 第二次若被误发，给合法成功响应，避免传输夹具错误掩盖错误重试。
        events = [
            {'type': 'response.output_text.delta', 'delta': 'done'},
            {'type': 'response.completed', 'response': {
                'id': 'resp_native_quota_fixture', 'status': 'completed',
                'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'done'}]}],
                'usage': {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6},
            }},
        ]
        raw = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, text=raw)

    def client_factory():
        client = httpx.Client(transport=httpx.MockTransport(provider_http), trust_env=False,
            follow_redirects=False, event_hooks={'response': [responses.append]})
        clients.append(client)
        return client

    def wait(delay):
        waits.append(delay)
        elapsed[0] += delay

    class Lease:
        def finish(self, status, *, error_code=None):
            finishes.append((status, error_code))

    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control):
            execution_control.model_call_routed(
                snapshot_ref=f"crp://session/{request['turn_id']}/turn-model-routing-snapshot-v1/frozen",
                snapshot_revision='a' * 64, prompt_cache_scope_identity='b' * 64,
                provider='openai', model='synthetic', execution_location='remote',
            )
            execution_control.model_call_started(provider='openai', model='synthetic')

            def authorize(*args):
                execution_control.checkpoint()
                guards.append(args)
                return Lease()

            def observe(status, error):
                # 只记录安全错误类型及原生类别，不采集请求头或服务商正文。
                observed.append((status, type(error), getattr(error, 'category', None),
                    getattr(error, 'code', None), getattr(error, 'retryable', None)))

            native = ResponsesCompletion()
            gateway = LiteLLMCompletionGateway(provider='openai', model='synthetic',
                base_url='https://api.openai.com/v1', api_key='synthetic-only',
                completion_fn=native, capabilities=ModelCapabilities(structured_modes=('prompt',)),
                egress_guard=authorize, provider_attempt_observer=observe)
            retry = ModelRetryControl(policy=get('retry', version='@1'),
                checkpoint=execution_control.checkpoint, clock=lambda: elapsed[0],
                wait=wait, jitter=lambda: 0, owned_client_factory=client_factory)
            text, usage, _ = gateway.complete_text_with_usage(
                [{'role': 'user', 'content': 'synthetic question'}], timeout=10,
                wire_attempt_sink=execution_control, retry_control=retry)
            assert text == 'done'
            execution_control.model_call_completed(usage=usage)
            return {'type': 'complete', 'summary': text}

    database = tmp_path / 'turns.sqlite3'
    store = SQLiteAITurnStore(database)
    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store)
    fixture = Path(__file__).resolve().parents[4] / 'core-contracts/ai/fixtures/turn-request/valid-project-answer.json'
    request = json.loads(fixture.read_text(encoding='utf-8'))
    receipt = runtime.submit_turn(request)
    terminals = [store.get(event['data']['receipt_ref']) for event in runtime.events_after(receipt.turn_id)
        if event['type'] == 'model.attempt.terminal']
    with sqlite3.connect(database) as connection:
        states = connection.execute('SELECT state FROM effect ORDER BY rowid').fetchall()

    assert clients and all(client.is_closed for client in clients)
    assert responses and all(response.is_closed for response in responses)
    assert observed[0] == ('failed', ResponsesProviderError, 'usage_limit',
        'subscription_sharing_usage_limit_exceeded', False)
    facts = {'wire_count': len(calls), 'waits': waits, 'guard_count': len(guards), 'lease_finishes': finishes,
        'terminals': [(item['status'], item['usage_status']) for item in terminals], 'effect_states': states,
        'provider_errors': [(status, kind.__name__, category, code, retryable)
            for status, kind, category, code, retryable in observed]}
    assert receipt.status == 'failed', json.dumps(facts, sort_keys=True)
    assert [(method, path) for method, path, _ in calls] == [('POST', '/v1/responses')]
    assert calls[0][2]['store'] is False and calls[0][2]['stream'] is True
    assert waits == []
    assert len(guards) == 1
    assert finishes == [('failed', 'provider_request_failed')]
    assert len(terminals) == 1
    assert terminals[0]['attempt_number'] == 1
    assert terminals[0]['status'] == 'failed_transport'
    assert terminals[0]['usage_status'] == 'unavailable'
    assert states == [('UNKNOWN',)]
