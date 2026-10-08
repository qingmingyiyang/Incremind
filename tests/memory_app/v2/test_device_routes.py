import base64
from io import BytesIO

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from PIL import Image
import pytest


def application(tmp_path, mode='server'):
    from backend.memory_app.v2.devices import DeviceRegistry, install_device_routes
    from backend.security.device_auth import install_device_authentication
    from backend.shared.deployment import DeploymentLayout
    devices = DeviceRegistry(tmp_path / 'server')
    app = FastAPI()
    layout = DeploymentLayout(mode, tmp_path / 'users/local-user', tmp_path if mode == 'server' else None)
    app.state.deployment = layout
    app.state.device_registry = devices
    @app.get('/api/private')
    def private():
        return {'private': True}
    @app.get('/api/health')
    def health():
        return {'root': str(tmp_path), 'storage': 'private'}
    @app.get('/pair')
    def bootstrap():
        return 'static'
    @app.websocket('/api/private/ws')
    async def socket(ws: WebSocket):
        await ws.accept()
        while True:
            message = await ws.receive()
            if message['type'] == 'websocket.disconnect':
                return
            await ws.send_text(message.get('text', ''))
    install_device_routes(app, registry=devices)
    install_device_authentication(app, registry=devices)
    return app, devices


def pair(devices):
    return devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='电脑')


@pytest.mark.parametrize('path', ['/api/private', '/openapi.json', '/docs', '/redoc', '/api/health/extra', '/anything'])
def test_server_requires_bearer_even_from_proxy_loopback(tmp_path, path):
    app, _ = application(tmp_path)
    client = TestClient(app, client=('127.0.0.1', 12345))
    assert client.get(path).status_code == 401


def test_only_static_bootstrap_health_and_post_exchange_are_public(tmp_path):
    app, devices = application(tmp_path)
    client = TestClient(app, base_url='https://brain.example')
    assert client.get('/api/health').json() == {'status': 'ok'}
    assert client.get('/pair').status_code == 200
    assert client.post('/pair').status_code == 401
    assert client.get('/api/v2/devices/exchange').status_code == 401
    assert client.post('/api/v2/devices/exchange', json={'code': 'invalid', 'name': '手机'}).status_code == 401
    assert len(devices.list_devices('local-user')) == 0


def test_authenticated_list_then_self_revoke_is_immediate_without_presence_cas_conflict(tmp_path):
    app, devices = application(tmp_path)
    paired = pair(devices)
    client = TestClient(app, headers={'Authorization': 'Bearer ' + paired['key']}, base_url='https://brain.example')
    listed = client.get('/api/v2/devices').json()
    item = listed['items'][0]
    assert listed['device_id'] == item['device_id']
    response = client.post('/api/v2/devices/' + item['device_id'] + '/revoke', json={'expected_revision': item['revision']})
    assert response.status_code == 200
    assert client.get('/api/private').status_code == 401
    assert client.get('/api/v2/devices').status_code == 401


def test_pair_qr_decodes_real_same_origin_fragment_and_never_logs_credentials(tmp_path, caplog):
    import zxingcpp
    app, devices = application(tmp_path)
    paired = pair(devices)
    client = TestClient(app, headers={'Authorization': 'Bearer ' + paired['key']}, base_url='https://brain.example')
    response = client.post('/api/v2/devices/pair', json={}, headers={'Origin': 'https://brain.example'})
    assert response.status_code == 200
    value = response.json()
    image = Image.open(BytesIO(base64.b64decode(value['qr'].split(',', 1)[1])))
    decoded = zxingcpp.read_barcodes(image)
    assert len(decoded) == 1
    from urllib.parse import urlsplit, parse_qs
    url = urlsplit(decoded[0].text)
    assert (url.scheme, url.netloc, url.path, url.query) == ('https', 'brain.example', '/pair', '')
    code = parse_qs(url.fragment)['code'][0]
    received = client.post('/api/v2/devices/exchange', json={'code': code, 'name': '手机'})
    assert received.status_code == 201
    assert client.post('/api/v2/devices/exchange', json={'code': code, 'name': '另一台'}).status_code == 401
    assert all(secret not in caplog.text for secret in (code, paired['key'], received.json()['key']))


def test_cross_origin_pair_write_is_rejected_but_same_origin_is_allowed(tmp_path):
    app, devices = application(tmp_path)
    paired = pair(devices)
    client = TestClient(app, headers={'Authorization': 'Bearer ' + paired['key']}, base_url='https://brain.example')
    assert client.post('/api/v2/devices/pair', json={}, headers={'Origin': 'https://foreign.example'}).status_code == 403
    assert client.post('/api/v2/devices/pair', json={}, headers={'Origin': 'https://brain.example'}).status_code == 200


def test_desktop_requires_no_device_key_and_has_virtual_paired_computer(tmp_path):
    app, _ = application(tmp_path, mode='desktop')
    client = TestClient(app)
    assert client.get('/api/private').status_code == 200
    listed = client.get('/api/v2/devices').json()
    assert listed['mode'] == 'desktop'
    assert listed['device_id'] == 'desktop'
    assert listed['items'][0]['device_id'] == 'desktop'
    assert client.post('/api/v2/devices/pair', json={}).status_code == 200


@pytest.mark.parametrize('authorization', ['Bearer é', 'Bearer ', 'Basic invalid', 'Bearer invalid'])
def test_invalid_bearer_is_controlled_not_an_authentication_exception(tmp_path, authorization):
    app, _ = application(tmp_path)
    # HTTP headers are bytes; HTTPX's str encoder rejects this malformed wire
    # input before ASGI gets the chance to reject it.
    assert TestClient(app).get('/api/private', headers=[(b'authorization', authorization.encode('latin-1'))]).status_code == 401


def test_websocket_uses_real_device_and_is_closed_before_next_message_after_revocation(tmp_path):
    from starlette.websockets import WebSocketDisconnect
    app, devices = application(tmp_path)
    paired = pair(devices)
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect('/api/private/ws'):
            pass
    assert denied.value.code == 4401
    with client.websocket_connect('/api/private/ws', headers={'Authorization': 'Bearer ' + paired['key']}) as ws:
        ws.send_text('before'); assert ws.receive_text() == 'before'
        device = paired['device']
        devices.revoke('local-user', device['device_id'], expected_revision=device['revision'])
        ws.send_text('after')
        with pytest.raises(WebSocketDisconnect) as revoked:
            ws.receive_text()
        assert revoked.value.code == 4401


def test_install_cli_creates_the_first_admin_pairing_without_an_application(tmp_path, capsys):
    from tools.server_admin import main
    from backend.memory_app.v2.devices import DeviceRegistry
    import json
    from urllib.parse import urlsplit, parse_qs
    assert main(['pair', '--root', str(tmp_path), '--url', 'https://brain.example']) == 0
    value = json.loads(capsys.readouterr().out)
    url = urlsplit(value['url'])
    assert url.query == '' and url.path == '/pair'
    devices = DeviceRegistry(tmp_path / 'server')
    received = devices.exchange(parse_qs(url.fragment)['code'][0], name='第一台')
    assert received['device']['user_id'] == 'local-user'
    user = devices.records.read('server_users', 'local-user')
    assert user.payload['role'] == 'admin' and user.payload['disabled_at'] is None
    assert user.payload['storage_limit_mb'] is None and user.payload['job_minutes_per_day'] is None


@pytest.mark.parametrize('socket_url,origin', [
    ('ws://brain.example/api/private/ws', 'http://brain.example'),
    ('wss://brain.example/api/private/ws', 'https://brain.example'),
    ('ws://brain.example:8765/api/private/ws', 'http://brain.example:8765'),
])
def test_websocket_accepts_exact_browser_origin_scheme_mapping(tmp_path, socket_url, origin):
    app, devices = application(tmp_path)
    paired = pair(devices)
    with TestClient(app).websocket_connect(socket_url, headers={'Authorization': 'Bearer ' + paired['key'], 'Origin': origin}) as ws:
        ws.send_text('same origin')
        assert ws.receive_text() == 'same origin'


@pytest.mark.parametrize('suffix,origin,code', [
    ('', 'https://foreign.example', 4403),
    ('', 'https://brain.example:8443', 4403),
    ('?ticket=unlogged-test-value', 'https://brain.example', 4401),
    ('?key=unlogged-test-value', 'https://brain.example', 4401),
    ('?device_key=unlogged-test-value', 'https://brain.example', 4401),
])
def test_websocket_valid_bearer_does_not_allow_foreign_origin_or_credential_query(tmp_path, suffix, origin, code):
    from starlette.websockets import WebSocketDisconnect
    app, devices = application(tmp_path)
    paired = pair(devices)
    with pytest.raises(WebSocketDisconnect) as denied:
        with TestClient(app).websocket_connect('wss://brain.example/api/private/ws' + suffix,
                headers={'Authorization': 'Bearer ' + paired['key'], 'Origin': origin}):
            pass
    assert denied.value.code == code
