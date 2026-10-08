"""实际应用入口和原 HTTP 转发；仅上游 socket 用合成 transport。"""
import asyncio
import gzip
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.memory_app.v2.test_external_agent_settings import factory as factory
from tests.memory_app.v2.test_external_proxy_handoff import env as env, enabled, add_document
from backend.memory_app.v2.external_proxy_transport import ProxyTransport
from backend.memory_app.v2.external_context import DELIVERIES
from backend.memory_app.v2.privacy import set_private_project
from tests.memory_app.v2.test_external_proxy_recording import QUESTION, ANSWER, response as terminal_response


PREFIX = '/api/v2/external-agent/proxy'


class Chunks(httpx.AsyncByteStream):
    def __init__(self, body, *, fail=False):
        self.body, self.fail, self.closed = body, fail, False

    async def __aiter__(self):
        yield self.body[:7]
        if self.fail:
            raise httpx.ReadError('synthetic stream failed')
        yield self.body[7:]

    async def aclose(self):
        self.closed = True


def upstream(app, body='{"choices":[{"message":{"role":"assistant","content":"合成回答"}}]}'.encode(), *, fail=False, before=None, content_encoding=None):
    captured, stream = [], Chunks(body, fail=fail)
    async def handle(request):
        captured.append(request)
        if before:
            before()
        headers = {'content-type':'application/json'}
        if content_encoding:
            headers['content-encoding'] = content_encoding
        return httpx.Response(200, headers=headers, stream=stream)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    app.state.external_proxy_transport = ProxyTransport(client)
    return captured, stream


@pytest.mark.parametrize('client,endpoint,payload', [
    ('codex','chat/completions', {'model':'synthetic','messages':[{'role':'user','content':'合成问题'}]}),
    ('codex','responses', {'model':'synthetic','input':'合成问题'}),
    ('claude','messages', {'model':'synthetic','messages':[{'role':'user','content':'合成问题'}]}),
])
def test_original_bytes_and_model_headers_forward_without_injection(factory, client, endpoint, payload):
    app, _ = factory()
    captured, stream = upstream(app, b'original response bytes')
    raw = json.dumps(payload, ensure_ascii=False, indent=3).encode()
    secret = 'synthetic-' + 'credential-value'
    with TestClient(app, client=('127.0.0.1', 5123)) as http:
        response = http.post(f'{PREFIX}/{client}/v1/{endpoint}', content=raw,
            headers={'Authorization':'Bearer '+secret,'x-api-key':secret,'Host':'untrusted.example','Forwarded':'for=evil'})
    assert response.status_code == 200 and response.content == b'original response bytes'
    assert captured[0].content == raw
    assert captured[0].headers['authorization'] == 'Bearer '+secret
    assert 'forwarded' not in captured[0].headers and captured[0].headers['host'] in {'api.openai.com','api.anthropic.com'}
    assert stream.closed
    assert app.state.recognition_records.list(DELIVERIES) == ()


@pytest.mark.parametrize('address', ['203.0.113.5','testclient'])
def test_socket_nonloopback_denied_despite_local_host_forwarded(factory, address):
    app, _ = factory(); captured, _ = upstream(app)
    with TestClient(app, client=(address, 5123)) as http:
        response = http.post(f'{PREFIX}/codex/v1/responses', json={'input':'合成'},
            headers={'Host':'localhost','Forwarded':'for=127.0.0.1','X-Forwarded-For':'127.0.0.1'})
    assert response.status_code == 403 and captured == []


def test_server_missing_user_domains_refuses_before_upstream(factory):
    app, _ = factory(); app.state.deployment = SimpleNamespace(mode='server')
    registry = app.state.device_registry
    pairing = registry.issue_pairing(user_id='local-user', actor='install')
    key = registry.exchange(pairing['code'], name='合成设备')['key']
    captured, _ = upstream(app)
    with TestClient(app, client=('127.0.0.1', 5123)) as http:
        response = http.post(f'{PREFIX}/codex/v1/responses', json={'input':'合成'}, headers={'Authorization':'Bearer '+key})
    assert response.status_code == 503 and captured == []


@pytest.mark.parametrize('raw', [b'not json', b'{"input":[]}', b'{"input":" "}'])
def test_unknown_shapes_keep_exact_bytes(factory, raw):
    app, _ = factory(); captured, _ = upstream(app, b'unchanged')
    with TestClient(app, client=('::1', 5123)) as http:
        response = http.post(f'{PREFIX}/codex/v1/responses', content=raw)
    assert response.status_code == 200 and captured[0].content == raw


@pytest.mark.parametrize('case', ['default_off','other_client','unknown_scope'])
def test_valid_complete_response_does_not_record_without_client_and_scope_qualification(factory, case):
    app, _ = factory()
    from backend.memory_app.v2.external_proxy_settings import replace_external_proxy_settings
    if case != 'default_off':
        replace_external_proxy_settings(app.state.recognition_records,
            {'record_conversations':{'codex':True,'claude':False}}, expected_revision=0)
    payload = {'messages':[{'role':'user','content':('#unknown ' if case == 'unknown_scope' else '')+QUESTION}]}
    client, protocol = ('claude','messages') if case == 'other_client' else ('codex','chat_completions')
    upstream(app, json.dumps(terminal_response(protocol)).encode())
    with TestClient(app, client=('127.0.0.1',5123)) as http:
        response = http.post(f'{PREFIX}/{client}/v1/{protocol.replace("_","/") if protocol != "messages" else protocol}', json=payload)
    assert response.status_code == 200
    assert app.state.recognition_records.list('workspace_items') == ()


def test_real_handoff_inserts_user_context_and_original_receipt(env):
    add_document(env, project='alpha', summary='alpha预算规则', body='alpha预算规则正文', original='alpha原件')
    enabled(env)
    captured, _ = upstream(env.http.app)
    with TestClient(env.http.app, client=('127.0.0.1', 5123)) as http:
        response = http.post(f'{PREFIX}/codex/v1/chat/completions', json={
            'model':'synthetic','messages':[{'role':'system','content':'原指令'}, {'role':'user','content':'#alpha alpha预算规则？'}]})
    assert response.status_code == 200
    sent = json.loads(captured[0].content)
    assert sent['messages'][0] == {'role':'system','content':'原指令'}
    assert len(sent['messages']) == 3 and sent['messages'][1]['role'] == 'user'
    assert '第二大脑上下文' in sent['messages'][1]['content']
    assert len(env.records.list(DELIVERIES)) == 1 and env.model.calls == 0


def test_late_private_change_before_actual_send_restores_exact_client_bytes(env, monkeypatch):
    add_document(env, project='alpha', summary='alpha预算规则', body='alpha预算规则正文', original='alpha原件')
    enabled(env)
    captured, stream = upstream(env.http.app, b'unchanged')
    transport = env.http.app.state.external_proxy_transport
    original_open = transport.open
    def after_preparation(protocol, body, headers, *, before_send=None):
        assert len(env.records.list(DELIVERIES)) == 1
        set_private_project(env.records, 'alpha', True, expected_revision=0)
        return original_open(protocol, body, headers, before_send=before_send)
    monkeypatch.setattr(transport, 'open', after_preparation)
    raw = json.dumps({'model':'synthetic', 'messages':[{'role':'user','content':'#alpha alpha预算规则？'}]},
        ensure_ascii=False, indent=3).encode()
    with TestClient(env.http.app, client=('127.0.0.1',5123)) as http:
        response = http.post(f'{PREFIX}/codex/v1/chat/completions', content=raw)
    assert response.status_code == 200 and response.content == b'unchanged'
    assert captured[0].content == raw and stream.closed
    assert len(env.records.list(DELIVERIES)) == 1 and env.model.calls == 0


@pytest.mark.parametrize('scheme', ['Bearer', 'Basic'])
def test_complete_recording_rechecks_switch_and_sanitizes_credentials(factory, caplog, scheme):
    app, _ = factory()
    secret = 'synthetic-' + 'credential-value'
    answer = terminal_response('chat_completions', '回答 '+ANSWER+' '+secret)
    captured, stream = upstream(app, json.dumps(answer).encode())
    with TestClient(app, client=('127.0.0.1', 5123)) as http:
        saved = http.patch('/api/v2/settings/external-proxy', json={
            'expected_revision':0,'record_conversations':{'codex':True,'claude':False}})
        assert saved.status_code == 200
        response = http.post(f'{PREFIX}/codex/v1/chat/completions', json={
            'messages':[{'role':'user','content':QUESTION+' '+secret}]}, headers={'Authorization':scheme+' '+secret})
    assert response.status_code == 200 and stream.closed and len(captured) == 1
    rows = app.state.recognition_records.list('workspace_items')
    assert len(rows) == 1 and rows[0].payload['title'] == 'codex 对话'
    assert rows[0].payload['status'] == 'staged'
    assert rows[0].payload['draft'] is rows[0].payload['document_id'] is None
    assert app.state.recognition_records.list('recognitions') == ()
    assert secret not in str(rows[0].payload) and '回答' in str(rows[0].payload)
    assert all(secret not in str(row.payload) for row in app.state.recognition_records.list_all())
    assert secret not in caplog.text
    for path in app.state.recognition_records.database_path.parent.glob(app.state.recognition_records.database_path.name+'*'):
        assert secret.encode() not in path.read_bytes()


def test_recording_off_during_upstream_drops_observation(factory):
    app, _ = factory()
    records = app.state.recognition_records
    from backend.memory_app.v2.external_proxy_settings import replace_external_proxy_settings
    replace_external_proxy_settings(records, {'record_conversations':{'codex':True,'claude':False}}, expected_revision=0)
    def disable():
        replace_external_proxy_settings(records, {'record_conversations':{'codex':False,'claude':False}}, expected_revision=1)
    upstream(app, json.dumps(terminal_response('chat_completions')).encode(), before=disable)
    with TestClient(app, client=('127.0.0.1', 5123)) as http:
        response = http.post(f'{PREFIX}/codex/v1/chat/completions', json={'messages':[{'role':'user','content':QUESTION}]})
    assert response.status_code == 200 and records.list('workspace_items') == ()


def test_broken_stream_closes_upstream_without_recording(factory):
    app, _ = factory()
    _, stream = upstream(app, json.dumps(terminal_response('chat_completions')).encode(), fail=True)
    with TestClient(app, client=('127.0.0.1', 5123)) as http:
        assert http.patch('/api/v2/settings/external-proxy', json={
            'expected_revision':0,'record_conversations':{'codex':True,'claude':False}}).status_code == 200
        with pytest.raises(Exception) as failure:
            http.post(f'{PREFIX}/codex/v1/chat/completions', json={'messages':[{'role':'user','content':QUESTION}]})
    def errors(error):
        return [error] + [child for nested in getattr(error, 'exceptions', ()) for child in errors(nested)]
    assert any(str(error) == 'external_proxy_stream_failed' for error in errors(failure.value))
    assert stream.closed and app.state.recognition_records.list('workspace_items') == ()


def test_original_asgi_disconnect_closes_upstream_without_recording(factory):
    app, _ = factory()
    from backend.memory_app.v2.external_proxy_settings import replace_external_proxy_settings
    replace_external_proxy_settings(app.state.recognition_records,
        {'record_conversations':{'codex':True,'claude':False}}, expected_revision=0)
    async def scenario():
        first = asyncio.Event()
        class WaitingStream(httpx.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                yield b'{"choices":'
                await asyncio.Event().wait()
            async def aclose(self):
                self.closed = True
        stream = WaitingStream()
        async def handle(request):
            return httpx.Response(200, headers={'content-type':'application/json'}, stream=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream_client:
            app.state.external_proxy_transport = ProxyTransport(upstream_client)
            raw = json.dumps({'messages':[{'role':'user','content':QUESTION}]}).encode()
            received = False
            async def receive():
                nonlocal received
                if not received:
                    received = True
                    return {'type':'http.request','body':raw,'more_body':False}
                await first.wait()
                return {'type':'http.disconnect'}
            async def send(event):
                if event['type'] == 'http.response.body' and event.get('body'):
                    first.set()
            scope = {'type':'http','asgi':{'version':'3.0','spec_version':'2.3'},'http_version':'1.1',
                'scheme':'http','method':'POST','path':PREFIX+'/codex/v1/chat/completions',
                'raw_path':(PREFIX+'/codex/v1/chat/completions').encode(),'query_string':b'',
                'headers':[(b'content-type',b'application/json')], 'client':('127.0.0.1',5123),
                'server':('localhost',80),'root_path':''}
            await asyncio.wait_for(app(scope, receive, send), timeout=5)
        assert first.is_set() and stream.closed
    asyncio.run(scenario())
    assert app.state.recognition_records.list('workspace_items') == ()


def test_recording_read_corruption_after_send_does_not_break_original_stream(factory):
    app, _ = factory(); records = app.state.recognition_records
    from backend.memory_app.v2.external_proxy_settings import replace_external_proxy_settings, COLLECTION
    replace_external_proxy_settings(records, {'record_conversations':{'codex':True,'claude':False}}, expected_revision=0)
    def corrupt():
        with records.begin() as tx:
            tx.put(COLLECTION, 'default', {'record_conversations':{'codex':'not-bool','claude':False}}, expected_revision=1)
            tx.commit()
    body = json.dumps(terminal_response('chat_completions')).encode()
    _, stream = upstream(app, body, before=corrupt)
    with TestClient(app, client=('127.0.0.1',5123)) as http:
        response = http.post(f'{PREFIX}/codex/v1/chat/completions', json={'messages':[{'role':'user','content':QUESTION}]})
    assert response.status_code == 200 and response.content == body and stream.closed
    assert records.list('workspace_items') == ()


def test_cancellation_before_upstream_headers_does_not_record_or_swallow_cancel(factory):
    app, _ = factory()
    from backend.memory_app.v2.external_proxy_settings import replace_external_proxy_settings
    replace_external_proxy_settings(app.state.recognition_records,
        {'record_conversations':{'codex':True,'claude':False}}, expected_revision=0)
    async def scenario():
        entered, exited = asyncio.Event(), asyncio.Event()
        async def handle(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream_client:
            app.state.external_proxy_transport = ProxyTransport(upstream_client)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1',5123)), base_url='http://local') as http:
                task = asyncio.create_task(http.post(PREFIX+'/codex/v1/chat/completions', json={'messages':[{'role':'user','content':QUESTION}]}))
                await asyncio.wait_for(entered.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert exited.is_set()
        assert upstream_client.is_closed
    asyncio.run(scenario())
    assert app.state.recognition_records.list('workspace_items') == ()


def capture_production_client(monkeypatch, stream):
    from backend.memory_app.v2 import external_proxy
    captured = []
    async def handle(request):
        return httpx.Response(200, headers={'content-type':'application/json'}, stream=stream)
    def construct(**kwargs):
        client = httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(handle))
        captured.append(client)
        return client
    # 仅替换上游传输的创建位置，返回真实 AsyncClient；ProxyTransport 不替换。
    monkeypatch.setattr(external_proxy, 'httpx', SimpleNamespace(AsyncClient=construct, Timeout=httpx.Timeout))
    return captured


async def endpoint_response(app):
    from starlette.requests import Request
    path = PREFIX+'/codex/v1/chat/completions'
    scope = {'type':'http','app':app,'asgi':{'version':'3.0','spec_version':'2.4'},'http_version':'1.1',
        'scheme':'http','method':'POST','path':path,'raw_path':path.encode(),'query_string':b'',
        'headers':[(b'content-type',b'application/json')], 'client':('127.0.0.1',5123),
        'server':('localhost',80),'root_path':''}
    async def receive():
        return {'type':'http.request','body':json.dumps({'messages':[{'role':'user','content':QUESTION}]}).encode(),'more_body':False}
    def routes(router):
        for route in router.routes:
            included = getattr(route, 'original_router', None)
            if included is not None:
                yield from routes(included)
            else:
                yield route
    route = next(route for route in routes(app.router) if getattr(route, 'path', None) == PREFIX+'/{client}/v1/chat/completions')
    response = await route.endpoint(Request(scope, receive), client='codex')
    return response, scope, receive


@pytest.mark.parametrize('primary', ['send', 'cancel'])
def test_before_headers_owned_client_close_fault_preserves_primary(factory, monkeypatch, primary):
    from backend.memory_app.v2 import external_proxy
    from fastapi import HTTPException
    app, _ = factory()
    entered, captured = asyncio.Event(), []
    async def handle(request):
        entered.set()
        if primary == 'send':
            raise httpx.ConnectError('synthetic primary detail')
        await asyncio.Event().wait()
    class OwnedClient(httpx.AsyncClient):
        async def aclose(self):
            await super().aclose()
            raise httpx.ReadError('synthetic secondary detail')
    def construct(**kwargs):
        client = OwnedClient(**kwargs, transport=httpx.MockTransport(handle))
        captured.append(client)
        return client
    monkeypatch.setattr(external_proxy, 'httpx', SimpleNamespace(AsyncClient=construct,
        Timeout=httpx.Timeout, HTTPError=httpx.HTTPError))
    async def scenario():
        if primary == 'send':
            with pytest.raises(HTTPException) as failure:
                await endpoint_response(app)
            assert failure.value.status_code == 502
            assert failure.value.detail == 'external_proxy_upstream_failed'
        else:
            task = asyncio.create_task(endpoint_response(app))
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert captured[0].is_closed
    asyncio.run(scenario())
    assert app.state.recognition_records.list('workspace_items') == ()


def test_production_client_only_bounds_connect_without_default_stream_timeout(factory, monkeypatch):
    app, _ = factory(); stream = Chunks(b'original bytes')
    captured = capture_production_client(monkeypatch, stream)
    async def scenario():
        response, scope, receive = await endpoint_response(app)
        async def send(event):
            pass
        await response(scope, receive, send)
        assert captured[0].timeout.connect == 10.0
        assert captured[0].timeout.read is captured[0].timeout.write is captured[0].timeout.pool is None
        assert captured[0].is_closed and stream.closed
    asyncio.run(scenario())


def test_asgi24_send_start_failure_closes_uniterated_upstream_and_owned_client(factory, monkeypatch):
    from starlette.requests import ClientDisconnect
    app, _ = factory(); stream = Chunks(json.dumps(terminal_response('chat_completions')).encode())
    captured = capture_production_client(monkeypatch, stream)
    from backend.memory_app.v2.external_proxy_settings import replace_external_proxy_settings
    replace_external_proxy_settings(app.state.recognition_records,
        {'record_conversations':{'codex':True,'claude':False}}, expected_revision=0)
    async def scenario():
        response, scope, receive = await endpoint_response(app)
        async def send(event):
            assert event['type'] == 'http.response.start'
            raise OSError('synthetic disconnected before body')
        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)
        assert stream.closed and captured[0].is_closed
    asyncio.run(scenario())
    assert app.state.recognition_records.list('workspace_items') == ()


@pytest.mark.parametrize('primary', ['stream','cancel'])
def test_original_stream_or_cancel_error_survives_upstream_close_fault(factory, monkeypatch, primary):
    app, _ = factory()
    class CloseFault(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield b'partial'
            if primary == 'stream':
                raise httpx.ReadError('synthetic original stream error')
            await asyncio.Event().wait()
        async def aclose(self):
            self.closed = True
            raise httpx.ReadError('synthetic secondary close error')
    stream = CloseFault(); captured = capture_production_client(monkeypatch, stream)
    async def scenario():
        response, scope, receive = await endpoint_response(app)
        first = asyncio.Event()
        async def send(event):
            if event['type'] == 'http.response.body':
                first.set()
        if primary == 'stream':
            from backend.memory_app.v2.external_proxy_transport import ProxyTransportError
            with pytest.raises(ProxyTransportError, match='^external_proxy_stream_failed$'):
                await response(scope, receive, send)
        else:
            task = asyncio.create_task(response(scope, receive, send))
            await asyncio.wait_for(first.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stream.closed and captured[0].is_closed
    asyncio.run(scenario())
    assert app.state.recognition_records.list('workspace_items') == ()


def test_gzip_wire_stays_exact_while_observer_records_decoded_staged_text(factory):
    app, _ = factory()
    from backend.memory_app.v2.external_proxy_settings import replace_external_proxy_settings
    replace_external_proxy_settings(app.state.recognition_records,
        {'record_conversations':{'codex':True,'claude':False}}, expected_revision=0)
    original = json.dumps(terminal_response('chat_completions'), ensure_ascii=False).encode()
    compressed = gzip.compress(original)
    _, stream = upstream(app, compressed, content_encoding='gzip')
    async def scenario():
        response, scope, receive = await endpoint_response(app)
        chunks = []
        async def send(event):
            if event['type'] == 'http.response.start':
                assert (b'content-encoding', b'gzip') in event['headers']
            if event['type'] == 'http.response.body':
                chunks.append(event['body'])
        await response(scope, receive, send)
        assert b''.join(chunks) == compressed and stream.closed
    asyncio.run(scenario())
    rows = app.state.recognition_records.list('workspace_items')
    assert len(rows) == 1 and QUESTION in rows[0].payload['source_text'] and ANSWER in rows[0].payload['source_text']
    assert rows[0].payload['status'] == 'staged'
    assert rows[0].payload['document_id'] is rows[0].payload['draft'] is None
    assert app.state.recognition_records.list('recognitions') == ()
