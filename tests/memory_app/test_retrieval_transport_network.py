from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
from threading import Thread

import httpcore
import httpx
import pytest
from litellm.llms.custom_httpx.http_handler import get_ssl_configuration

from backend.memory_app.retrieval_models import ConfiguredTransport


@contextmanager
def rejecting_proxy_environment():
    values = {'HTTP_PROXY': 'http://127.0.0.1:9', 'HTTPS_PROXY': 'http://127.0.0.1:9',
              'ALL_PROXY': 'http://127.0.0.1:9', 'NO_PROXY': ''}
    values.update({name.lower(): value for name, value in tuple(values.items())})
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.mark.parametrize('host', ['127.0.0.1', 'localhost', '::1'])
def test_real_loopback_request_stays_direct_with_empty_no_proxy(host):
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            received.append((self.headers['Authorization'], json.loads(
                self.rfile.read(int(self.headers['Content-Length'])))))
            body = json.dumps({'received': True}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class IPv6Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6

    server_type = IPv6Server if host == '::1' else ThreadingHTTPServer
    server = server_type(('::1' if host == '::1' else '127.0.0.1', 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        address = '[' + host + ']' if host == '::1' else host
        endpoint = 'http://' + address + ':' + str(server.server_port) + '/transport-check'
        with rejecting_proxy_environment():
            result = ConfiguredTransport('synthetic-local-only').post_json(
                endpoint=endpoint, payload={'marker': 'local-transport-check'})
        assert result == {'received': True}
        assert received == [('Bearer synthetic-local-only', {'marker': 'local-transport-check'})]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_real_remote_httpx_pool_uses_environment_proxy_without_sending():
    with rejecting_proxy_environment(), httpx.Client(timeout=35, follow_redirects=False,
            verify=get_ssl_configuration(), trust_env=True) as client:
        transport = client._transport_for_url(httpx.URL('https://api.openai.com/v1/embeddings'))
        assert isinstance(transport._pool, httpcore.HTTPProxy)
        assert client.trust_env is True
        assert client.follow_redirects is False
