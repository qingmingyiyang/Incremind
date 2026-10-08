"""真实设备和权限 owner 校验安装边界；只隔离网络与权重安装。"""
from backend.memory_app.v2.embedding_settings import EmbeddingSettings, vector_policy
from types import SimpleNamespace
from threading import Event

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from core.storage_provider import SQLiteStructuredRecordStore
from backend.security.secrets import InMemorySecretStore
from backend.security.user_context import USER_ACCESS, authorize_user, user_context
from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.settings import install_settings_routes
from tests.memory_app.v2.test_server_users import owners


@pytest.fixture
def server_install(tmp_path, monkeypatch):
    users, devices, admin, _ = owners(tmp_path)
    target = users.create(admin, name='安装权限样本')
    pairing = devices.issue_pairing(user_id=target['user_id'], actor=admin.device_id)
    paired = devices.exchange(pairing['code'], name='普通设备')
    ordinary = devices.authenticate(paired['key'])
    root = users.root_for(target['user_id'])
    records = SQLiteStructuredRecordStore(root / 'vector-settings.sqlite3')
    models = ModelConfiguration(records, root, InMemorySecretStore(), local_models_root=tmp_path / 'models')
    app = FastAPI()
    owner = EmbeddingSettings(records, models._local_models_root)
    app.state.memory_embedding_settings = owner
    models.bind_embedding(owner.project, vector_policy)
    app.state.deployment = SimpleNamespace(mode='server')
    app.state.server_users = users
    identities = {'admin': admin, 'ordinary': ordinary}
    @app.middleware('http')
    async def identity_context(request, call_next):
        caller = identities.get(request.headers.get('test-caller'))
        access = authorize_user(users, caller, target['user_id']) if caller else None
        with user_context(access):
            return await call_next(request)
    install_settings_routes(app, runtime_root=root, records=records, models=models)
    fetched, release, completed = Event(), Event(), Event()
    actor = []
    def fake_fetch(models_root, *, model, progress):
        actor.append(USER_ACCESS.get())
        progress({'done': 1, 'total': 2})
        fetched.set()
        try:
            assert release.wait(5)
        finally:
            completed.set()
    monkeypatch.setattr('backend.memory_app.v2.embedding_settings.install_embedding', fake_fetch)
    with TestClient(app) as client:
        try:
            yield SimpleNamespace(client=client, records=records, users=users, admin=admin,
                ordinary=ordinary, target=target, fetched=fetched, release=release,
                actor=actor, completed=completed)
        finally:
            release.set()
            if fetched.is_set():
                assert completed.wait(5)
            models.close()


@pytest.mark.parametrize('caller', ['ordinary', 'missing'])
def test_server_install_rejects_non_admin_without_job_or_fetch(server_install, caller):
    env = server_install
    response = env.client.post('/api/v2/settings/embedding/install',
        headers={'test-caller': caller}, json={'expected_revision': 0})
    assert response.status_code == 403
    assert env.records.list('v2_embedding_install') == ()
    assert not env.fetched.is_set()
    public = env.client.get('/api/v2/settings', headers={'test-caller': caller}).json()
    assert public['model']['embedding']['local']['can_install'] is False


def test_server_admin_install_keeps_actor_and_duplicate_job_is_idempotent(server_install):
    env = server_install
    headers = {'test-caller': 'admin'}
    assert env.client.get('/api/v2/settings', headers=headers).json()['model']['embedding']['local']['can_install']
    response = env.client.post('/api/v2/settings/embedding/install', headers=headers,
        json={'expected_revision': 0})
    assert response.status_code == 202 and env.fetched.wait(5)
    assert env.actor[0].caller == env.admin
    assert env.actor[0].target_user_id == env.target['user_id']
    assert env.actor[0].by == 'admin'
    duplicate = env.client.post('/api/v2/settings/embedding/install', headers=headers,
        json={'expected_revision': 0})
    assert duplicate.status_code == 202 and duplicate.json() == response.json()
    assert len(env.actor) == 1


def test_server_install_stale_mode_revision_never_fetches(server_install):
    env = server_install
    headers = {'test-caller': 'admin'}
    assert env.client.patch('/api/v2/settings/embedding-mode', headers=headers,
        json={'mode': 'local', 'expected_revision': 0}).status_code == 200
    response = env.client.post('/api/v2/settings/embedding/install', headers=headers,
        json={'expected_revision': 0})
    assert response.status_code == 409
    assert env.records.list('v2_embedding_install') == ()
    assert not env.fetched.is_set()
