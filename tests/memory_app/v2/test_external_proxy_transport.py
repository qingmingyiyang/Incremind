"""代理本身使用真实 HTTPX；仅上游网络换成合成传输。"""
import asyncio
from copy import deepcopy
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import httpx
import pytest


class UpstreamStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, fail=False):
        self.chunks, self.fail, self.closed = chunks, fail, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.fail:
            raise httpx.ReadError('synthetic upstream failure')

    async def aclose(self):
        self.closed = True


def module():
    from backend.memory_app.v2.external_proxy_transport import ProxyTransport, ProxyTransportError
    return ProxyTransport, ProxyTransportError


def test_before_send_selects_exact_final_body_once():
    ProxyTransport, _ = module()
    order, seen = [], []
    async def handler(request):
        order.append('send')
        seen.append(request)
        return httpx.Response(200, stream=UpstreamStream([b'{}']))
    def validate():
        order.append('validate')
        return b' {"original":true}\n'
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async with ProxyTransport(client).open('responses', b'{"injected":true}', [],
                    before_send=validate) as response:
                assert b''.join([chunk async for chunk in response.iter_bytes()]) == b'{}'
    asyncio.run(run())
    assert order == ['validate', 'send']
    assert seen[0].content == b' {"original":true}\n'
    assert seen[0].headers['content-length'] == str(len(seen[0].content))


@pytest.mark.parametrize('callback', [lambda: 'not bytes', lambda: 1,
    lambda: (_ for _ in ()).throw(ValueError('synthetic-private-detail'))])
def test_invalid_before_send_never_opens_upstream_or_exposes_detail(callback):
    ProxyTransport, ProxyTransportError = module()
    calls = []
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: calls.append(request))) as client:
            with pytest.raises(ProxyTransportError) as caught:
                async with ProxyTransport(client).open('messages', b'{}', [], before_send=callback):
                    pytest.fail('invalid final body must not open')
            assert str(caught.value) in {'external_proxy_body_invalid', 'external_proxy_before_send_failed'}
            assert 'synthetic-private-detail' not in str(caught.value)
    asyncio.run(run())
    assert calls == []


@pytest.mark.parametrize('protocol,path', [
    ('chat_completions', '/v1/chat/completions'), ('responses', '/v1/responses'),
    ('messages', '/v1/messages'),
])
def test_original_body_credentials_and_protocol_headers_only(protocol, path):
    ProxyTransport, _ = module()
    secret = 'synthetic-' + 'credential-private-marker'
    incoming = [('Authorization', 'Bearer ' + secret), ('X-Api-Key', secret),
        ('Anthropic-Version', '2023-06-01'), ('Anthropic-Beta', 'synthetic-beta'),
        ('OpenAI-Organization', 'org-synthetic'), ('OpenAI-Project', 'proj-synthetic'),
        ('X-Chriptmas-Device-Key', 'synthetic-device'), ('Cookie', 'synthetic-cookie'),
        ('Host', 'malicious.invalid'), ('Connection', 'keep-alive'),
        ('Content-Length', '999999'), ('X-Forwarded-For', '198.51.100.4')]
    original = deepcopy(incoming)
    body = b'{ "model":"synthetic", "messages": [] }\n'
    seen = []
    stream = UpstreamStream([b'data: a\r\n\r\n', b'\xff\x00raw'])

    async def handler(request):
        seen.append(request)
        return httpx.Response(200, stream=stream, headers={
            'content-type': 'text/event-stream', 'cache-control': 'no-store',
            'set-cookie': 'synthetic-cookie', 'x-api-key': secret})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                headers={'X-Default-Secret': secret}, cookies={'private': secret}) as client:
            proxy = ProxyTransport(client)
            async with proxy.open(protocol, body, incoming) as response:
                assert response.status_code == 200
                assert response.headers == {'content-type':'text/event-stream', 'cache-control':'no-store'}
                assert b''.join([chunk async for chunk in response.iter_bytes()]) == b'data: a\r\n\r\n\xff\x00raw'
            assert secret not in repr(proxy)
    asyncio.run(run())
    assert incoming == original and stream.closed and len(seen) == 1
    request = seen[0]
    assert request.content == body and request.url.path == path
    assert request.headers['accept-encoding'] == 'identity'
    assert request.headers['content-type'] == 'application/json'
    assert request.headers['authorization'] == 'Bearer ' + secret
    assert request.headers['x-api-key'] == secret
    for name in ('cookie','x-default-secret','x-chriptmas-device-key','x-forwarded-for','connection'):
        assert name not in request.headers
    assert request.headers['host'] in {'api.openai.com', 'api.anthropic.com'}
    assert request.headers['content-length'] == str(len(body))


@pytest.mark.parametrize('status', [400, 401, 429, 500, 307])
def test_status_body_and_redirect_are_transparent_without_following(status):
    ProxyTransport, _ = module()
    calls, stream = [], UpstreamStream([b'opaque error bytes'])
    async def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status, headers={'location':'https://elsewhere.invalid/',
            'retry-after':'3', 'content-type':'application/json'}, stream=stream)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            async with ProxyTransport(client).open('responses', b'{}', []) as response:
                assert response.status_code == status
                assert response.headers == {'retry-after':'3','content-type':'application/json'}
                assert b''.join([part async for part in response.iter_bytes()]) == b'opaque error bytes'
    asyncio.run(run())
    assert calls == ['https://api.openai.com/v1/responses'] and stream.closed


def test_partial_stream_close_and_stream_failure_release_upstream():
    ProxyTransport, ProxyTransportError = module()
    async def run():
        for failure in (False, True):
            stream = UpstreamStream([b'first', b'second'], fail=failure)
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, stream=stream))) as client:
                if failure:
                    received = []
                    with pytest.raises(ProxyTransportError, match='^external_proxy_stream_failed$'):
                        async with ProxyTransport(client).open('messages', b'{}', []) as response:
                            async for part in response.iter_bytes():
                                received.append(part)
                    assert received == [b'first', b'second']
                else:
                    async with ProxyTransport(client).open('messages', b'{}', []) as response:
                        assert await anext(response.iter_bytes()) == b'first'
                assert stream.closed
    asyncio.run(run())


def test_cancelled_relay_closes_without_swallowing_cancellation():
    ProxyTransport, _ = module()
    stream, started = UpstreamStream([b'first']), asyncio.Event()
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=stream))) as client:
            async def consume():
                async with ProxyTransport(client).open('messages', b'{}', []) as response:
                    assert await anext(response.iter_bytes()) == b'first'
                    started.set()
                    await asyncio.Future()
            task = asyncio.create_task(consume())
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert stream.closed
    asyncio.run(run())


def test_open_failure_fixed_code_no_credentials_in_error_or_logs(caplog):
    ProxyTransport, ProxyTransportError = module()
    secret = 'synthetic-' + 'transport-private-marker'
    async def handler(request):
        raise httpx.ConnectError(secret, request=request)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ProxyTransportError) as failure:
                async with ProxyTransport(client).open('responses', b'{}', [('Authorization', secret)]):
                    pytest.fail('transport failure must not yield a response')
            assert str(failure.value) == 'external_proxy_upstream_failed'
            assert secret not in repr(failure.value)
    asyncio.run(run())
    assert secret not in caplog.text


@pytest.mark.parametrize('headers', [
    [('Authorization','a'), ('authorization','b')], [('X-Api-Key','a'), ('x-api-key','b')],
    [('Authorization','a\r\nX-Leak: b')], [('Authorization', 1)],
])
def test_invalid_credentials_rejected_without_contacting_upstream(headers):
    ProxyTransport, ProxyTransportError = module()
    calls = []
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: calls.append(request))) as client:
            with pytest.raises(ProxyTransportError, match='^external_proxy_headers_invalid$'):
                async with ProxyTransport(client).open('responses', b'{}', headers):
                    pytest.fail('invalid credential headers were forwarded')
    asyncio.run(run())
    assert calls == []


@pytest.mark.parametrize('url', ['http://api.openai.com/v1/responses',
    'https://user:password@api.openai.com/v1/responses',
    'https://api.openai.com/v1/responses?secret=x',
    'https://api.openai.com/v1/responses#fragment', 'file:///tmp/upstream'])
def test_invalid_trusted_endpoint_rejected(url):
    ProxyTransport, ProxyTransportError = module()
    async def run():
        async with httpx.AsyncClient() as client:
            with pytest.raises(ProxyTransportError, match='^external_proxy_endpoint_invalid$'):
                ProxyTransport(client, endpoints={'responses':url})
    asyncio.run(run())


def test_literal_loopback_endpoint_for_mock_server_and_unknown_protocol():
    ProxyTransport, ProxyTransportError = module()
    calls = []
    async def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, stream=UpstreamStream([b'ok']))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            proxy = ProxyTransport(client, endpoints={'responses':'http://127.0.0.1:1234/v1/responses'})
            async with proxy.open('responses', b'{}', []) as response:
                assert [part async for part in response.iter_bytes()] == [b'ok']
            with pytest.raises(ProxyTransportError, match='^external_proxy_protocol_invalid$'):
                async with proxy.open('https://malicious.invalid', b'{}', []):
                    pytest.fail('arbitrary request endpoint accepted')
    asyncio.run(run())
    assert calls == ['http://127.0.0.1:1234/v1/responses']


def test_compressed_response_preserves_wire_bytes_and_encoding():
    ProxyTransport, _ = module()
    compressed = gzip.compress(b'data: synthetic\n\n')
    stream = UpstreamStream([compressed[:5], compressed[5:]])
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, headers={'content-encoding':'gzip'}, stream=stream))) as client:
            async with ProxyTransport(client).open('responses', b'{}', []) as response:
                assert response.headers == {'content-encoding':'gzip'}
                assert b''.join([part async for part in response.iter_bytes()]) == compressed
    asyncio.run(run())
    assert stream.closed


def test_real_loopback_upstream_emits_first_chunk_before_finishing():
    ProxyTransport, _ = module()
    released, finished, observed = threading.Event(), threading.Event(), []
    chunks = [b'data: first\n\n', 'data: 合成\n\n'.encode(), b'data: [DONE]\n\n']
    body = b'{ "stream":true,"input":"synthetic" }\n'
    secret = 'synthetic-' + 'native-network-private-marker'

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            # 合成上游也不记录请求或凭据。
            return None

        def do_POST(self):
            observed.append((self.path, self.headers['Authorization'],
                self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            self.wfile.write(f'{len(chunks[0]):X}\r\n'.encode() + chunks[0] + b'\r\n')
            self.wfile.flush()
            if released.wait(3):
                for chunk in chunks[1:]:
                    self.wfile.write(f'{len(chunk):X}\r\n'.encode() + chunk + b'\r\n')
                self.wfile.write(b'0\r\n\r\n')
                self.wfile.flush()
            finished.set()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    async def run():
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            proxy = ProxyTransport(client, endpoints={
                'responses':f'http://127.0.0.1:{server.server_port}/v1/responses'})
            async with proxy.open('responses', body, [('Authorization',secret)]) as response:
                iterator = response.iter_bytes()
                first = await asyncio.wait_for(anext(iterator), 2)
                assert first == chunks[0] and not finished.is_set()
                released.set()
                assert first + b''.join([part async for part in iterator]) == b''.join(chunks)
    try:
        asyncio.run(run())
        assert observed == [('/v1/responses', secret, body)]
    finally:
        released.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert not thread.is_alive() and finished.is_set()


@pytest.mark.parametrize('primary', ['none', 'stream', 'cancel'])
def test_close_fault_is_fixed_and_preserves_primary_failure(primary, caplog):
    ProxyTransport, ProxyTransportError = module()
    secret = 'synthetic-' + 'close-fault-private-marker'
    class CloseFault(UpstreamStream):
        async def aclose(self):
            self.closed = True
            raise httpx.ReadError(secret)
    stream = CloseFault([b'first'], fail=primary == 'stream')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=stream))) as client:
            expected = asyncio.CancelledError if primary == 'cancel' else ProxyTransportError
            with pytest.raises(expected) as failure:
                async with ProxyTransport(client).open('responses', b'{}', []) as response:
                    if primary == 'cancel':
                        raise asyncio.CancelledError()
                    if primary == 'stream':
                        async for part in response.iter_bytes():
                            assert part == b'first'
            assert secret not in str(failure.value) and secret not in repr(failure.value)
            if primary != 'cancel':
                assert str(failure.value) == ('external_proxy_stream_failed' if primary == 'stream'
                                              else 'external_proxy_close_failed')
            assert stream.closed
    asyncio.run(run())
    assert secret not in caplog.text
