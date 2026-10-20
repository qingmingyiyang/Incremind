"""The v2 settings routes expose the existing ModelConfiguration updates."""
from concurrent.futures import ThreadPoolExecutor
import inspect
from pathlib import Path
from shutil import copyfile
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.settings import install_settings_routes
from core.storage_provider import SQLiteStructuredRecordStore


def _forbidden_wire(**_kwargs):
    raise AssertionError('settings writes must not call a model')


def make(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    models = ModelConfiguration(records, tmp_path, completion_fn=_forbidden_wire)
    app = FastAPI()
    install_settings_routes(app, runtime_root=tmp_path, records=records, models=models)
    return TestClient(app), models


def test_put_model_updates_public_configuration(tmp_path):
    http, models = make(tmp_path)
    response = http.put('/api/v2/settings/model', json={
        'purpose': 'rerank', 'base_url': 'http://127.0.0.1:11434/v1', 'model': 'fake-rerank',
        'api_key': '', 'clear_api_key': False, 'allow_remote': False, 'enabled': True,
        'expected_revision': 0,
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['model'] == 'fake-rerank'
    public = models.public()['rerank']
    assert public['model'] == 'fake-rerank'
    assert public['allow_remote'] is False


def test_put_model_rejects_stale_revision(tmp_path):
    http, _ = make(tmp_path)
    first = http.put('/api/v2/settings/model', json={
        'purpose': 'rerank', 'base_url': '', 'model': 'fake-rerank', 'api_key': '',
        'clear_api_key': False, 'allow_remote': False, 'enabled': True, 'expected_revision': 0,
    })
    assert first.status_code == 200, first.text
    stale = http.put('/api/v2/settings/model', json={
        'purpose': 'rerank', 'base_url': '', 'model': 'other-rerank', 'api_key': '',
        'clear_api_key': False, 'allow_remote': False, 'enabled': True, 'expected_revision': 0,
    })
    assert stale.status_code == 409


def test_put_generation_mode_updates_mode(tmp_path):
    http, models = make(tmp_path)
    response = http.put('/api/v2/settings/generation-mode', json={
        'mode': 'api', 'local_enabled': False,
        'local_base_url': 'http://127.0.0.1:8001/local-model/v1', 'expected_revision': 0,
    })
    assert response.status_code == 200, response.text
    assert response.json()['mode'] == 'api'
    assert models.generation_mode()['mode'] == 'api'


def test_put_generation_mode_rejects_stale_revision_without_changing_configuration(tmp_path):
    http, models = make(tmp_path)
    first = http.put('/api/v2/settings/generation-mode', json={
        'mode': 'api', 'local_enabled': False,
        'local_base_url': 'http://127.0.0.1:8001/local-model/v1', 'expected_revision': 0,
    })
    assert first.status_code == 200, first.text
    before = models.generation_mode()
    assert before['revision'] == 1
    generation = models.public()['generation']
    stale = http.put('/api/v2/settings/generation-mode', json={
        'mode': 'api', 'local_enabled': True,
        'local_base_url': 'http://127.0.0.1:8002/local-model/v1', 'expected_revision': 0,
    })
    assert stale.status_code == 409, stale.text
    assert stale.json() == {'detail': 'generation_mode_revision_conflict'}
    assert models.generation_mode() == before
    assert models.public()['generation'] == generation


@pytest.mark.parametrize('mode', ['unsupported', None])
def test_put_generation_mode_preserves_invalid_input_status(tmp_path, mode):
    http, models = make(tmp_path)
    before = models.generation_mode()
    response = http.put('/api/v2/settings/generation-mode', json={
        'mode': mode, 'local_enabled': False,
        'local_base_url': 'http://127.0.0.1:8001/local-model/v1', 'expected_revision': 0,
    })
    assert response.status_code == 400, response.text
    assert response.json() == {'detail': 'invalid_generation_mode'}
    assert models.generation_mode() == before


@pytest.mark.parametrize('mode', [[], {}], ids=['array', 'object'])
def test_put_generation_mode_rejects_containers_without_changing_saved_configuration(tmp_path, mode):
    http, models = make(tmp_path)
    models.update('generation', {
        'base_url': 'http://127.0.0.1:11434/v1', 'model': 'saved-generation',
        'allow_remote': False, 'expected_revision': 0,
    })
    first = http.put('/api/v2/settings/generation-mode', json={
        'mode': 'api', 'local_enabled': False,
        'local_base_url': 'http://127.0.0.1:8001/local-model/v1', 'expected_revision': 0,
    })
    assert first.status_code == 200, first.text
    before = models.generation_mode()
    generation = models.public()['generation']
    stored_generation = models.records.read('recognition_model_config', 'generation')
    stored_mode = models.records.read('recognition_generation_mode', 'default')

    response = http.put('/api/v2/settings/generation-mode', json={
        'mode': mode, 'local_enabled': True,
        'local_base_url': 'http://127.0.0.1:8002/local-model/v1', 'expected_revision': 1,
    })

    assert response.status_code == 400, response.text
    assert response.json() == {'detail': 'invalid_generation_mode'}
    assert models.generation_mode() == before
    assert models.public()['generation'] == generation
    assert models.records.read('recognition_model_config', 'generation') == stored_generation
    assert models.records.read('recognition_generation_mode', 'default') == stored_mode


def _endpoint(application, path):
    pending = list(application.routes)
    while pending:
        route = pending.pop()
        if getattr(route, 'path', None) == path and 'PUT' in getattr(route, 'methods', set()):
            return route.endpoint
        original = getattr(route, 'original_router', None)
        if original is not None:
            pending.extend(original.routes)
    raise AssertionError(f'missing settings route: {path}')


@pytest.mark.parametrize(('suffix', 'body', 'collection', 'object_id'), [
    ('model', {
        'purpose': 'generation', 'base_url': 'https://provider.example/v1',
        'model': 'synthetic-lock-probe', 'api_key': '', 'allow_remote': False,
        'enabled': False, 'clear_api_key': False, 'expected_revision': 0,
    }, 'recognition_model_config', 'generation'),
    ('generation-mode', {
        'mode': 'api', 'local_enabled': False,
        'local_base_url': 'http://127.0.0.1:8001/local-model/v1', 'expected_revision': 0,
    }, 'recognition_generation_mode', 'default'),
])
def test_v2_model_writes_share_task_mutation_lock(tmp_path, monkeypatch, suffix, body, collection, object_id):
    config = tmp_path / 'config'
    config.mkdir()
    copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example', config / 'settings.toml')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'server')
    from backend.memory_app.app import create_app
    from backend.memory_app.storage_authority import resolve_recognition_document_store
    from backend.security.secrets import InMemorySecretStore
    from backend.shared.deployment import DeploymentLayout

    records, namespace = resolve_recognition_document_store(tmp_path)
    assert namespace == 'default'
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=_forbidden_wire)
    app = create_app(runtime_root=tmp_path, legacy_app=FastAPI(), model_configuration=models,
                     deployment_layout=DeploymentLayout('desktop', tmp_path))
    lock = app.state.recognition_mutation_lock
    assert app.state.recognition_models is models
    assert app.state.recognition_turn_dispatcher.lock is lock
    assert lock is not models._lock
    legacy = _endpoint(app, '/api/recognition/settings')
    mutate = inspect.getclosurevars(legacy).nonlocals['mutate']
    assert inspect.getclosurevars(mutate).nonlocals['mutation_lock'] is lock

    class Registry:
        def __init__(self):
            self.registrations = []

        def register(self, *args):
            self.registrations.append(args)

    registry = Registry()
    app.state.recognition_turn_installer(registry, object(), app.state)
    assert len(registry.registrations) == 2
    assert app.state.recognition_turn_authority.lock is lock
    assert app.state.recognition_turn_authority.models is models

    path = f'/api/v2/settings/{suffix}'
    endpoint = _endpoint(app, path)
    source, first_line = inspect.getsourcelines(endpoint)
    lock_line = next((first_line + index for index, line in enumerate(source)
                      if 'with mutation_lock:' in line), None)
    reached_lock = threading.Event()
    attempted_or_finished = threading.Event()

    def trace(frame, event, _argument):
        if frame.f_code is not endpoint.__code__:
            return None
        if event == 'line' and frame.f_lineno == lock_line:
            reached_lock.set()
            attempted_or_finished.set()
        return trace

    def put():
        client = TestClient(app)
        try:
            return client.put(path, json=body)
        finally:
            client.close()

    # 事件区分已到锁边界和绕过锁后完成，避免靠 sleep 猜测请求是否正在等待。
    previous_trace = threading.gettrace()
    threading.settrace(trace)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            lock.acquire()
            try:
                future = pool.submit(put)
                future.add_done_callback(lambda _future: attempted_or_finished.set())
                assert attempted_or_finished.wait(15), 'request did not reach the settings write boundary'
                assert reached_lock.is_set(), f'{path} completed without entering the shared mutation lock'
                assert inspect.getclosurevars(endpoint).nonlocals['mutation_lock'] is lock
                assert not future.done()
                assert records.read(collection, object_id) is None
            finally:
                lock.release()
            response = future.result(timeout=15)
    finally:
        threading.settrace(previous_trace)
    assert response.status_code == 200, response.text
    assert response.json()['revision'] == 1
    assert records.read(collection, object_id).revision == 1
