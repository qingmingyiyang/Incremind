import pytest
from contextvars import copy_context
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from backend.security.user_context import UserError
from backend.shared.deployment import DeploymentLayout


class _Factory:
    def __init__(self, app, credential):
        self.app, self._credential = app, credential

    def headers(self):
        return {'Authorization': 'Bearer ' + self._credential}


@pytest.fixture
def server(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp('s')
    assert root.is_dir() and root.is_absolute()
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'server')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root))
    monkeypatch.setenv('CHRIPTMAS_SERVER_MASTER_KEY', Fernet.generate_key().decode('ascii'))
    from backend.memory_app.app import create_server_app
    app = create_server_app(layout=DeploymentLayout('server', root / 'users' / 'local-user', root), port=18101)
    registry = app.state.device_registry
    pairing = registry.issue_pairing(user_id='local-user', actor='install')
    device = registry.exchange(pairing['code'], name='synthetic ASR owner check')
    return _Factory(app, device['key'])


def test_wire_owner_never_loads_a_child_from_a_path_and_only_accepts_a_real_live_job(server):
    context = server.app.state.server_context
    root = context.users.root_for('local-user')
    getter = context.resources.server_asr_wire_owner_for
    with pytest.raises(UserError, match='user_unauthorized'):
        getter(root)
    assert context.pool.loaded_user_ids == ()
    observed = []
    captured = []
    class Observer:
        async def __call__(self, scope, receive, send):
            async def inspect(message):
                if scope['type'] == 'http' and scope['path'] == '/api/v2/settings' and message['type'] == 'http.response.start':
                    if captured:
                        with pytest.raises(UserError, match='user_unauthorized'):
                            captured[0].run(getter, root)
                    observed.append(getter(root))
                    captured.append(copy_context())
                await send(message)
            await server.app(scope, receive, inspect)
    with TestClient(Observer(), base_url='http://127.0.0.1:18101', client=('127.0.0.1', 1234)) as client:
        assert client.get('/api/v2/settings', headers=server.headers()).status_code == 200
        child = context.pool._children['local-user'].application
        assert observed == [(child.state.recognition_records, child.state.recognition_models)]
        assert context.pool._request_leases == {}
        assert client.get('/api/v2/settings', headers=server.headers()).status_code == 200
        assert len(observed) == 2 and context.pool._request_leases == {}
        with pytest.raises(UserError, match='user_unauthorized'):
            getter(root)
        records, models = client.portal.call(context.jobs.submit, 'local-user', 'metadata', lambda: getter(root))
        assert records is models.records and models is child.state.recognition_models
        assert models.server_model_balances is context.model_balances
        assert models.server_model_access._owner is context.models
        assert context.jobs.active_for('local-user') == 0
        with pytest.raises(UserError, match='user_unauthorized'):
            getter(root)
