"""The actual ticket route seals caller and selected space for the real WS gate."""
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.shared.deployment import DeploymentLayout
from backend.security.secrets import InMemorySecretStore
from backend.api.routes import realtime_asr
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.product_core.realtime_asr_provider_settings import SaveRealtimeAsrProviderSettings
from backend.memory_app.server_runtime import create_server_application
from backend.video_summary.infrastructure.in_memory_progress_tracker import InMemoryProgressTracker
from backend.video_summary.infrastructure.rag_models import RagModelManager

TICKET = '/api/rebuild/workbench/realtime-asr/ticket'
SOCKET = '/api/rebuild/workbench/realtime-asr/ws'


def fixture(tmp_path):
    def child(root, user_id, context):
        app = FastAPI()
        app.state.deployment = DeploymentLayout('server', root, tmp_path)
        app.state.device_registry = context.registry
        # 用真实下载进度所有者补齐子应用的生命周期合同。
        app.state.container = SimpleNamespace(
            root_dir=root, secret_store=InMemorySecretStore(),
            model_download_progress_tracker=InMemoryProgressTracker(),
            rag_model_manager=RagModelManager(
                root_dir=root, models_root=context.resources.model_path('fastembed'),
                progress_tracker=InMemoryProgressTracker()),
        )
        app.include_router(realtime_asr.router)
        return app
    app = create_server_application(DeploymentLayout('server', tmp_path/'users/local-user', tmp_path), child_factory=child)
    registry, users = app.state.device_registry, app.state.server_users
    admin_pair = registry.exchange(registry.issue_pairing(user_id='local-user', actor='install')['code'], name='admin')
    admin = registry.authenticate(admin_pair['key'])
    target = users.create(admin, name='乙')
    user_pair = registry.exchange(registry.issue_pairing(user_id=target['user_id'], actor=admin.device_id)['code'], name='乙')
    store, _ = build_rebuild_object_store(users.root_for(target['user_id']))
    SaveRealtimeAsrProviderSettings(store, now='2026-10-05T00:00:00+00:00').execute(enabled=True, confirm_enable=True)
    return app, admin_pair, user_pair, admin, target


def offered(ticket):
    return ['chriptmas-asr', 'chriptmas-asr-ticket.' + ticket]


def test_admin_ticket_restores_target_but_keeps_admin_as_caller_and_is_single_use(tmp_path):
    app, pair, _, _, target = fixture(tmp_path)
    with TestClient(app) as client:
        result = client.post(TICKET, headers={'Authorization':'Bearer '+pair['key'], 'X-Chriptmas-Target-User':target['user_id']})
        assert result.status_code == 201
        with client.websocket_connect(SOCKET, subprotocols=offered(result.json()['ticket']), headers={'Origin':'http://testserver'}) as ws:
            assert ws.accepted_subprotocol == 'chriptmas-asr'
            assert ws.receive_json()['code'] == 'realtime_asr_api_key_required'
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(SOCKET, subprotocols=offered(result.json()['ticket'])):
                pass
        assert denied.value.code == 4401
        assert app.state.server_user_pool.loaded_user_ids == (target['user_id'],)


@pytest.mark.parametrize('boundary', ['disabled', 'revoked', 'expired', 'foreign'])
def test_target_bound_ticket_revalidates_every_authority_before_loading_child(tmp_path, boundary, monkeypatch):
    from backend.api.realtime_asr_ticket import RealtimeAsrTicketAuthority
    clock = [100.0]
    monkeypatch.setattr(realtime_asr, '_TICKET_AUTHORITY', RealtimeAsrTicketAuthority(now=lambda: clock[0]))
    app, pair, user_pair, admin, target = fixture(tmp_path)
    with TestClient(app) as client:
        assert client.post(TICKET, headers={'Authorization':'Bearer '+user_pair['key'], 'X-Chriptmas-Target-User':'local-user'}).status_code == 403
        ticket = client.post(TICKET, headers={'Authorization':'Bearer '+pair['key'], 'X-Chriptmas-Target-User':target['user_id']}).json()['ticket']
        if boundary == 'disabled':
            app.state.server_users.update(admin, target['user_id'], expected_revision=1, disabled=True)
        elif boundary == 'revoked':
            row = pair['device']
            app.state.device_registry.revoke('local-user', row['device_id'], expected_revision=row['revision'])
        elif boundary == 'expired':
            clock[0] += 16
        origin = 'http://foreign.example' if boundary == 'foreign' else 'http://testserver'
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(SOCKET, subprotocols=offered(ticket), headers={'Origin':origin}):
                pass
        assert denied.value.code in (4401, 4403)
