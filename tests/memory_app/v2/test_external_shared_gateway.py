import json
import asyncio
import secrets
import sqlite3
from decimal import Decimal
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import httpx
import pytest

from backend.memory_app.v2.external_shared_gateway import SharedExternalGateway
from backend.memory_app.v2.external_proxy_transport import ProxyTransport
from tests.memory_app.v2.test_server_models import owners, server_master_key


def _body(usage):
    return json.dumps({'choices': [], 'usage': usage}).encode()


def test_json_usage_preserves_observed_cache_and_unknown_cache():
    known = SharedExternalGateway._usage(_body({'prompt_tokens': 20, 'completion_tokens': 3,
        'prompt_cache_hit_tokens': 12, 'prompt_cache_miss_tokens': 8}), {'content-type': 'application/json'})
    assert known == {'usage': {'input_tokens': 20, 'output_tokens': 3},
        'cache_observation': {'cache_read_input_tokens': 12, 'cache_miss_input_tokens': 8,
            'cache_creation_input_tokens': None}}
    unknown = SharedExternalGateway._usage(_body({'prompt_tokens': 20, 'completion_tokens': 3}), {})
    assert unknown['cache_observation'] == {'cache_read_input_tokens': None, 'cache_miss_input_tokens': None,
        'cache_creation_input_tokens': None}


def test_sse_usage_uses_complete_original_events_and_requires_done():
    raw = b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\r\n\r\n'
    raw += b'data: ' + _body({'prompt_tokens': 20, 'completion_tokens': 3}) + b'\r\n\r\n'
    headers = {'content-type': 'text/event-stream'}
    assert SharedExternalGateway._usage(raw, headers) is None
    result = SharedExternalGateway._usage(raw + b'data: [DONE]\r\n\r\n', headers)
    assert result['usage'] == {'input_tokens': 20, 'output_tokens': 3}


@pytest.mark.parametrize('usage', [
    {'prompt_tokens': True, 'completion_tokens': 1},
    {'prompt_tokens': -1, 'completion_tokens': 1},
    {'prompt_tokens': 2, 'completion_tokens': 1, 'prompt_cache_hit_tokens': 3},
    {'prompt_tokens': 2, 'completion_tokens': 1, 'prompt_cache_hit_tokens': 1, 'prompt_cache_miss_tokens': 2},
    {'prompt_tokens': 2},
])
def test_invalid_usage_never_becomes_zero_cost(usage):
    assert SharedExternalGateway._usage(_body(usage), {}) is None


def test_unsupported_encoding_and_conflicting_sse_usage_remain_unknown():
    assert SharedExternalGateway._usage(b'{}', {'content-encoding': 'gzip'}) is None
    raw = b'data: ' + _body({'prompt_tokens': 2, 'completion_tokens': 1}) + b'\n\n'
    raw += b'data: ' + _body({'prompt_tokens': 2, 'completion_tokens': 2}) + b'\n\n'
    raw += b'data: [DONE]\n\n'
    assert SharedExternalGateway._usage(raw, {'content-type': 'text/event-stream'}) is None


@pytest.mark.parametrize('body', [b'[]', b'{"usage":[]}', b'{"usage":null}', b'{"usage":{"prompt_tokens":NaN}}'])
def test_non_object_or_non_finite_usage_remains_unknown(body):
    assert SharedExternalGateway._usage(body, {'content-type': 'application/json'}) is None


@pytest.fixture
def gateway_owner(owners):
    from backend.memory_app.v2.server_model_balance import ServerModelBalances
    users, registry, actor, user, _, models = owners
    rates = {'input_per_million': '2', 'output_per_million': '8', 'cache_read_per_million': '0.04'}
    credential = secrets.token_urlsafe(32)
    profile = models.create_profile(actor, name='共享模型', purpose='generation', provider='openai',
        base_url='https://api.deepseek.com/v1', model='deepseek-flash', allowed_models=['deepseek-flash'],
        enabled=True, api_key=credential, prices={'deepseek-flash': rates})
    models.update_allocation(actor, user['user_id'], expected_revision=0, expected_user_revision=user['revision'],
        mode='shared', profiles={'generation': profile['profile_id']})
    root = users.root_for(user['user_id'])
    access = models.access_for(user['user_id'], root)
    selected = access.selection('generation')
    balances = ServerModelBalances(users, models)
    gateway = SharedExternalGateway(access=access, balances=balances, actor=actor, user_root=root,
        selection=selected, validate=partial(access.validate_binding, selected['server_model_binding']),
        task_id='turn-controlled-gateway')
    gateway.start()
    try:
        yield gateway, balances, models, actor, user, profile, credential
    finally:
        gateway.close()


def test_real_loopback_rejects_wrong_credential_and_does_not_leak_central_key(gateway_owner):
    gateway, balances, _, actor, user, _, credential = gateway_owner
    response = httpx.post(gateway.base_url + '/chat/completions',
        headers={'authorization': 'Bearer invalid-lease'}, json={'model': 'deepseek-flash', 'messages': []})
    assert response.status_code == 403
    assert credential not in response.text
    assert balances.attempts(actor, user['user_id'], state='all')['items'] == []


def test_real_loopback_refuses_unallocated_model_before_reserving_money(gateway_owner):
    gateway, balances, _, actor, user, _, _ = gateway_owner
    response = httpx.post(gateway.base_url + '/chat/completions',
        headers={'authorization': 'Bearer ' + gateway.credential},
        json={'model': 'unallocated-model', 'messages': []})
    assert response.status_code == 403
    assert balances.attempts(actor, user['user_id'], state='all')['items'] == []


def test_real_loopback_zero_balance_blocks_before_creating_attempt(gateway_owner):
    gateway, balances, _, actor, user, _, _ = gateway_owner
    response = httpx.post(gateway.base_url + '/chat/completions',
        headers={'authorization': 'Bearer ' + gateway.credential},
        json={'model': 'deepseek-flash', 'messages': [], 'max_tokens': 10})
    assert response.status_code == 403
    assert balances.attempts(actor, user['user_id'], state='all')['items'] == []
    assert balances.read(actor, user['user_id'])['reserved'] == '0'


def test_real_loopback_refuses_changed_binding_and_revoked_lease(gateway_owner):
    gateway, balances, models, actor, user, profile, _ = gateway_owner
    token = gateway.credential
    models.update_profile(actor, profile['profile_id'], expected_revision=profile['revision'], enabled=False)
    response = httpx.post(gateway.base_url + '/chat/completions',
        headers={'authorization': 'Bearer ' + token}, json={'model': 'deepseek-flash', 'messages': []})
    assert response.status_code == 403
    gateway.revoke()
    response = httpx.post(gateway.base_url + '/chat/completions',
        headers={'authorization': 'Bearer ' + token}, json={'model': 'deepseek-flash', 'messages': []})
    assert response.status_code == 403
    assert balances.attempts(actor, user['user_id'], state='all')['items'] == []


def test_real_reservation_uses_trusted_task_identity_and_ignores_payload_identity(gateway_owner):
    gateway, balances, _, actor, user, _, _ = gateway_owner
    balances.adjust(actor, user['user_id'], amount='10', expected_revision=0,
        expected_user_revision=user['revision'], reason='隔离预留检查')
    held = gateway._reserve({'model': 'deepseek-flash', 'max_tokens': 10,
        'external_task_id': 'forged-body-task'}, 'external-trusted-task-check')
    assert held['status'] == 'reserved'
    assert held['external_task_id'] == 'turn-controlled-gateway'
    assert balances.attempts(actor, user['user_id'], state='all')['items'][0]['external_task_id'] == 'turn-controlled-gateway'


def test_profile_change_after_real_reservation_stays_pending_and_can_be_reconciled(gateway_owner):
    gateway, balances, models, actor, user, profile, _ = gateway_owner
    balances.adjust(actor, user['user_id'], amount='10', expected_revision=0,
        expected_user_revision=user['revision'], reason='隔离竞态检查')
    token = gateway.credential
    # 实际SQLite提交使授权在reserve成功后失效，无需替换任何owner或执行上游请求。
    with sqlite3.connect(models.records.database_path) as connection:
        connection.execute("""CREATE TRIGGER revoke_profile_after_reserve
            AFTER INSERT ON crp_structured_records
            WHEN NEW.collection = 'server_model_reservations'
            BEGIN
                UPDATE crp_structured_records
                SET payload_json = json_set(payload_json, '$.enabled', json('false')), revision = revision + 1
                WHERE collection = 'server_model_profiles';
            END""")
    response = httpx.post(gateway.base_url + '/chat/completions',
        headers={'authorization': 'Bearer ' + token},
        json={'model': 'deepseek-flash', 'messages': [], 'max_tokens': 10})
    assert response.status_code == 403
    assert models.records.read('server_model_profiles', profile['profile_id']).revision == profile['revision'] + 1
    attempts = balances.attempts(actor, user['user_id'], state='all')['items']
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt['status'] == 'pending_review'
    assert attempt['pending_reason'] == 'transport_failed'
    assert attempt['charged_amount'] is None
    assert Decimal(balances.read(actor, user['user_id'])['reserved']) > 0
    reviewed = balances.reconcile(actor, user['user_id'], attempt['attempt_id'], amount='0',
        expected_revision=attempt['revision'], expected_user_revision=user['revision'],
        reason='真实credential校验拒绝，确认尚未发送')
    assert reviewed['status'] == 'settled'
    assert balances.read(actor, user['user_id'])['reserved'] == '0'
    assert balances.read(actor, user['user_id'])['balance'] == '10'


def test_real_http_transport_preserves_tool_reasoning_and_sse_bytes():
    received = []
    wire = b'data: {"choices":[{"delta":{"reasoning_content":"reason","tool_calls":[{"id":"call-1"}]}}]}\r\n\r\n'
    wire += b'data: ' + _body({'prompt_tokens': 20, 'completion_tokens': 3,
        'prompt_cache_hit_tokens': 12, 'prompt_cache_miss_tokens': 8}) + b'\r\n\r\ndata: [DONE]\r\n\r\n'

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.rfile.read(int(self.headers['content-length'])), dict(self.headers)))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request_body = b'{"model":"deepseek-flash","messages":[],"thinking":{"type":"enabled"},"stream":true}'
    transport_key = secrets.token_urlsafe(32)

    async def run():
        async with httpx.AsyncClient(trust_env=False) as client:
            transport = ProxyTransport(client, endpoints={'chat_completions':
                'http://127.0.0.1:' + str(server.server_port) + '/v1/chat/completions'})
            async with transport.open('chat_completions', request_body,
                    {'authorization': 'Bearer ' + transport_key}) as response:
                raw = b''.join([chunk async for chunk in response.iter_bytes()])
                assert raw == wire
                assert SharedExternalGateway._usage(raw, response.headers)['usage'] == {
                    'input_tokens': 20, 'output_tokens': 3}
    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    assert received[0][0] == '/v1/chat/completions'
    assert received[0][1] == request_body
    assert received[0][2]['authorization'] == 'Bearer ' + transport_key
