from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def application(tmp_path):
    from backend.api.routes import realtime_asr
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.security.device_auth import install_device_authentication
    from backend.security.secrets import InMemorySecretStore
    from backend.shared.deployment import DeploymentLayout
    app = FastAPI()
    devices = DeviceRegistry(tmp_path / 'server')
    app.state.container = SimpleNamespace(root_dir=tmp_path / 'user', secret_store=InMemorySecretStore())
    app.state.deployment = DeploymentLayout('server', tmp_path / 'user', tmp_path)
    app.state.device_registry = devices
    app.state.server_realtime_ticket_consumer = realtime_asr.consume_server_ticket
    app.include_router(realtime_asr.router)
    install_device_authentication(app, registry=devices)
    paired = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='电脑')
    return app, devices, paired


TICKET = '/api/rebuild/workbench/realtime-asr/ticket'
SOCKET = '/api/rebuild/workbench/realtime-asr/ws'


def test_ticket_subject_is_one_time_and_retains_existing_expiry_boundary():
    from backend.api.realtime_asr_ticket import RealtimeAsrTicketAuthority
    clock = [100.]
    authority = RealtimeAsrTicketAuthority(now=lambda: clock[0])
    token = authority.issue(subject='device:local-user:one')
    clock[0] += 15
    assert authority.consume_subject(token) == 'device:local-user:one'
    assert authority.consume_subject(token) is None
    expired = authority.issue(subject='device:local-user:two')
    clock[0] += 16
    assert authority.consume_subject(expired) is None


def test_server_issued_ticket_requires_device_and_uses_nonsecret_public_subprotocol(tmp_path):
    app, _, paired = application(tmp_path)
    client = TestClient(app, base_url='https://brain.example')
    assert client.post(TICKET).status_code == 401
    issued = client.post(TICKET, headers={'Authorization': 'Bearer ' + paired['key']})
    assert issued.status_code == 201
    value = issued.json()
    assert value['transport'] == 'subprotocol' and value['protocol'] == 'chriptmas-asr'
    assert value['single_use'] is True and value['expires_in_seconds'] == 15
    assert paired['key'] not in issued.text
    offered = ['chriptmas-asr', 'chriptmas-asr-ticket.' + value['ticket']]
    with client.websocket_connect('wss://brain.example' + SOCKET, subprotocols=offered,
            headers={'Origin': 'https://brain.example'}) as ws:
        assert ws.accepted_subprotocol == 'chriptmas-asr'
        assert ws.receive_json()['code'] == 'realtime_asr_disabled'
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect(SOCKET, subprotocols=offered):
            pass
    assert denied.value.code == 4401


def test_revoked_device_and_wrong_user_tickets_cannot_open_server_socket(tmp_path):
    from backend.api.routes.realtime_asr import _TICKET_AUTHORITY
    app, devices, paired = application(tmp_path)
    client = TestClient(app)
    identity = paired['device']
    issued = client.post(TICKET, headers={'Authorization': 'Bearer ' + paired['key']}).json()['ticket']
    wrong = _TICKET_AUTHORITY.issue(subject='device:someone-else:' + identity['device_id'])
    devices.revoke('local-user', identity['device_id'], expected_revision=identity['revision'])
    for ticket in (issued, wrong):
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(SOCKET, subprotocols=['chriptmas-asr', 'chriptmas-asr-ticket.' + ticket]):
                pass
        assert denied.value.code == 4401


@pytest.mark.parametrize('suffix,origin', [('', 'https://foreign.example'), ('?ticket=not-a-secret', 'https://brain.example'),
    ('?device_key=not-a-secret', 'https://brain.example')])
def test_valid_ticket_does_not_allow_credential_query_or_cross_origin(tmp_path, suffix, origin):
    app, _, paired = application(tmp_path)
    client = TestClient(app, base_url='https://brain.example')
    ticket = client.post(TICKET, headers={'Authorization': 'Bearer ' + paired['key']}).json()['ticket']
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect('wss://brain.example' + SOCKET + suffix,
                subprotocols=['chriptmas-asr', 'chriptmas-asr-ticket.' + ticket], headers={'Origin': origin}):
            pass
    assert denied.value.code in (4401, 4403)


@pytest.mark.parametrize('revocation_boundary', ['receive', 'send'])
def test_live_asr_revocation_stops_audio_and_closes_the_real_session_transport(tmp_path, monkeypatch, revocation_boundary):
    import asyncio
    import json
    import threading
    from backend.api.qwen_realtime_asr import qwen_realtime_egress_manifest
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from backend.security.provider_egress import ProviderEgressPolicyStore
    from core.product_core.realtime_asr_provider_settings import SaveRealtimeAsrProviderSettings, QWEN_REALTIME_ASR_SECRET_REF
    app, devices, paired = application(tmp_path)
    root = app.state.container.root_dir
    app.state.container.secret_store.set(QWEN_REALTIME_ASR_SECRET_REF, 'synthetic-transport-key')
    store, _ = build_rebuild_object_store(root)
    settings = SaveRealtimeAsrProviderSettings(store, now='2026-10-05T00:00:00Z').execute(enabled=True, confirm_enable=True)
    manifest = qwen_realtime_egress_manifest(root, endpoint=settings.endpoint)
    ProviderEgressPolicyStore(root).grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
    # The same wire protocol fixture as the preserved provider tests, without
    # importing their global legacy-app bootstrap into this isolated test.
    class Upstream:
        def __init__(self):
            self.queue = None
            self.sent = []
        async def send(self, value):
            self.sent.append(value)
            if self.queue is None:
                self.queue = asyncio.Queue()
            if isinstance(value, str) and json.loads(value).get('header', {}).get('action') == 'run-task':
                await self.queue.put(json.dumps({'header': {'event': 'task-started'}, 'payload': {}}))
            elif isinstance(value, bytes):
                await self.queue.put(json.dumps({'header': {'event': 'result-generated'}, 'payload': {
                    'output': {'sentence': {'text': '实时结果', 'sentence_end': True, 'sentence_id': 1}}, 'usage': {'duration': 1}}}))
        async def recv(self):
            if self.queue is None:
                self.queue = asyncio.Queue()
            return await self.queue.get()
    upstream = Upstream()
    closed = threading.Event()
    class Connector:
        async def __aenter__(self):
            return upstream
        async def __aexit__(self, *args):
            closed.set()
    app.state.qwen_realtime_connector = lambda endpoint, headers: Connector()
    with TestClient(app) as client:
        with client.websocket_connect(SOCKET, headers={'Authorization': 'Bearer ' + paired['key']}) as ws:
            assert ws.receive_json()['type'] == 'ready'
            before = b'\x00\x01' * 160
            ws.send_bytes(before)
            assert ws.receive_json()['type'] == 'final'
            devices.revoke('local-user', paired['device']['device_id'], expected_revision=1)
            if revocation_boundary == 'receive':
                ws.send_bytes(b'\x02\x03' * 160)
            else:
                async def result_after_revoke():
                    await upstream.queue.put(json.dumps({'header': {'event': 'result-generated'}, 'payload': {'output': {'sentence': {'text': 'late', 'sentence_end': True}}}}))
                client.portal.call(result_after_revoke)
            with pytest.raises(WebSocketDisconnect) as revoked:
                ws.receive_json()
            assert revoked.value.code == 4401
            assert closed.wait(3), 'revocation did not close the actual provider transport'
        assert [value for value in upstream.sent if isinstance(value, bytes)] == [before]
    snapshots = store.list('realtime_asr_session_snapshots')
    assert len(snapshots) == 1
    receipt = store.read('realtime_asr_session_receipts', 'receipt-' + snapshots[0]['id'])
    assert receipt['status'] == 'unknown' and receipt['error_code'] == 'client_disconnected'
    assert paired['key'] not in json.dumps(snapshots)
