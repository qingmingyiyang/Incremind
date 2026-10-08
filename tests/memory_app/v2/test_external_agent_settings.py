"""External-agent preferences use the installed app and its SQLite authority."""
from pathlib import Path
from shutil import copyfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


COLLECTION = 'v2_external_agent_settings'
DEFAULT = {'revision': 0, 'allow_remote': False, 'include_profile': True,
           'daily_limit': 200, 'clients': {'claude': True, 'codex': True}}


def body(current=DEFAULT, **changes):
    return {**{key: value for key, value in current.items() if key != 'revision'},
            'expected_revision': current['revision'], **changes}


@pytest.fixture
def factory(tmp_path, monkeypatch):
    config = tmp_path / 'config'
    config.mkdir()
    copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example', config / 'settings.toml')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'desktop')
    from backend.memory_app.app import create_app

    def make(name='runtime'):
        app = create_app(runtime_root=tmp_path / name, legacy_app=FastAPI())
        return app, TestClient(app)
    return make


def read(client):
    response = client.get('/api/v2/settings')
    assert response.status_code == 200
    return response.json()['external_agent']


def test_default_projection_is_read_only_and_detached(factory):
    app, client = factory()
    assert read(client) == DEFAULT
    changed = read(client)
    changed['clients']['claude'] = False
    assert read(client) == DEFAULT
    assert app.state.recognition_records.list(COLLECTION) == ()
    response = client.patch('/api/v2/settings/external-agent', json=body())
    assert response.status_code == 200 and response.json() == DEFAULT
    assert app.state.recognition_records.list(COLLECTION) == ()


def test_patch_sqlite_restart_noop_and_stale_same_value_cas(factory):
    app, client = factory()
    wanted = body(allow_remote=True, include_profile=False, daily_limit=321,
                  clients={'claude': False, 'codex': True})
    response = client.patch('/api/v2/settings/external-agent', json=wanted)
    assert response.status_code == 200
    saved = response.json()
    assert saved == {key: value for key, value in wanted.items() if key != 'expected_revision'} | {'revision': 1}
    row = app.state.recognition_records.read(COLLECTION, 'default')
    assert row.revision == 1 and dict(row.payload) == {key: value for key, value in saved.items() if key != 'revision'}
    restarted, again = factory()
    assert restarted.state.recognition_records.database_path == app.state.recognition_records.database_path
    assert read(again) == saved
    assert again.patch('/api/v2/settings/external-agent', json=body(saved)).json() == saved
    assert again.patch('/api/v2/settings/external-agent', json=wanted).status_code == 409
    assert read(client) == saved
    assert app.state.recognition_records.read(COLLECTION, 'default').revision == 1


def test_two_installed_instances_share_cas_without_overwriting(factory):
    first, a = factory()
    second, b = factory()
    assert first.state.recognition_records is not second.state.recognition_records
    a_revision, b_revision = read(a), read(b)
    saved = a.patch('/api/v2/settings/external-agent', json=body(a_revision, daily_limit=234))
    assert saved.status_code == 200
    assert b.patch('/api/v2/settings/external-agent', json=body(b_revision, include_profile=False)).status_code == 409
    assert read(a) == read(b) == saved.json()


@pytest.mark.parametrize('change', [
    {'allow_remote': 1}, {'include_profile': 'true'}, {'daily_limit': True},
    {'daily_limit': 0}, {'daily_limit': -1}, {'daily_limit': 1.5}, {'daily_limit': '200'},
    {'clients': []}, {'clients': {'codex': True}},
    {'clients': {'claude': True, 'codex': True, 'other': True}},
    {'clients': {'claude': False, 'codex': 1}},
    {'expected_revision': True}, {'expected_revision': -1}, {'expected_revision': '0'},
    {'unknown': 'field'},
])
def test_invalid_complete_fields_do_not_write(factory, change):
    app, client = factory()
    response = client.patch('/api/v2/settings/external-agent', json=body(**change))
    assert response.status_code == 400
    assert app.state.recognition_records.list(COLLECTION) == ()


@pytest.mark.parametrize('missing', ['allow_remote', 'include_profile', 'daily_limit', 'clients', 'expected_revision'])
def test_partial_replacement_is_rejected(factory, missing):
    app, client = factory()
    wanted = body()
    del wanted[missing]
    assert client.patch('/api/v2/settings/external-agent', json=wanted).status_code == 400
    assert app.state.recognition_records.list(COLLECTION) == ()


def test_settings_never_change_model_privacy_or_source_authority(factory):
    app, client = factory()
    assert client.put('/api/recognition/settings', json={
        'purpose': 'generation', 'base_url': 'https://example.invalid/v1', 'model': 'synthetic',
        'api_key': '', 'allow_remote': False, 'expected_revision': 0}).status_code == 200
    project = client.post('/api/v2/projects', json={'name': '合成私密项目'})
    assert project.status_code == 200
    assert client.patch('/api/v2/settings/privacy', json={
        'private_projects': [project.json()['id']], 'expected_revision': 0}).status_code == 200
    before = client.get('/api/v2/settings').json()
    records = app.state.recognition_records
    protected = ('recognition_model_config', 'recognition_generation_mode', 'v2_generation_fast_model',
                 'v2_subscription_selection', 'v2_private_scopes', 'v2_privacy_state',
                 'workspace_items', 'recognitions', 'documents')
    rows = {name: records.list(name) for name in protected}
    assert client.patch('/api/v2/settings/external-agent', json=body(allow_remote=True)).status_code == 200
    after = client.get('/api/v2/settings').json()
    assert after['model'] == before['model'] and after['privacy'] == before['privacy']
    assert {name: records.list(name) for name in protected} == rows
    assert client.get('/api/v2/settings/egress-receipts').json() == []


def test_distinct_runtime_authorities_do_not_share_preferences(factory):
    first, a = factory('first')
    second, b = factory('second')
    assert first.state.recognition_records.database_path != second.state.recognition_records.database_path
    assert a.patch('/api/v2/settings/external-agent', json=body(allow_remote=True)).status_code == 200
    assert read(b) == DEFAULT and second.state.recognition_records.list(COLLECTION) == ()


def test_installed_desktop_origin_guard_rejects_write(factory):
    app, client = factory()
    response = client.patch('/api/v2/settings/external-agent', json=body(allow_remote=True),
                            headers={'Origin': 'https://untrusted.invalid'})
    assert response.status_code == 403 and response.json()['detail'] == 'local_origin_required'
    assert app.state.recognition_records.list(COLLECTION) == ()


def test_existing_server_device_gate_applies_to_new_settings(tmp_path, monkeypatch):
    config = tmp_path / 'config'
    config.mkdir()
    copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example', config / 'settings.toml')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'server')
    from backend.memory_app.app import create_app
    app = create_app(runtime_root=tmp_path / 'users/local-user', legacy_app=FastAPI())
    client = TestClient(app, base_url='https://brain.example')
    assert client.get('/api/v2/settings').status_code == 401
    assert client.patch('/api/v2/settings/external-agent', json=body(allow_remote=True)).status_code == 401
    registry = app.state.device_registry
    paired = registry.exchange(registry.issue_pairing(user_id='local-user', actor='install')['code'], name='合成设备')
    authorized = TestClient(app, base_url='https://brain.example', headers={'Authorization': 'Bearer ' + paired['key']})
    assert read(authorized) == DEFAULT
    assert authorized.patch('/api/v2/settings/external-agent', json=body(allow_remote=True)).status_code == 200
    response = authorized.patch('/api/v2/settings/external-agent', json=body(expected_revision=1),
                                headers={'Origin': 'https://untrusted.invalid'})
    assert response.status_code == 403
    device = paired['device']
    registry.revoke('local-user', device['device_id'], expected_revision=device['revision'])
    assert authorized.get('/api/v2/settings').status_code == 401


def test_malformed_stored_preferences_fail_closed(factory):
    app, client = factory()
    records = app.state.recognition_records
    with records.begin() as tx:
        tx.put(COLLECTION, 'default', {'allow_remote': True}, expected_revision=0)
        tx.commit()
    before = records.read(COLLECTION, 'default')
    response = client.get('/api/v2/settings')
    assert response.status_code == 503 and response.json()['detail'] == 'external_agent_settings_invalid'
    assert records.read(COLLECTION, 'default') == before
