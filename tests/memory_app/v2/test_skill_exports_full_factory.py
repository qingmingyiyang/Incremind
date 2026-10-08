"""原完整应用的技能消费者；仅提供方 wire 使用合成返回值。"""
import asyncio
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
from threading import Thread
import time
from types import SimpleNamespace
import zipfile

import httpx
import pytest
import uvicorn
import yaml

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.storage_authority import resolve_recognition_document_store
from backend.memory_app.v2.memory_turn import MemoryTurn
from backend.recognition import WorkScope
from backend.security.secrets import InMemorySecretStore
from tests.memory_app.v2.test_skill_exports import draft, method


ROOT = Path(__file__).resolve().parents[3]
BASE = '/api/v2/projects/alpha/skill-exports'
STARTUP_ENVIRONMENT = (
    'CHRIPTMAS_DESKTOP_SESSION_MODE', 'CHRIPTMAS_DESKTOP_SESSION_SECRET',
    'CHRIPTMAS_DESKTOP_INSTANCE_ID', 'CHRIPTMAS_DESKTOP_NONCE',
    'CHRIPTMAS_DESKTOP_PROTOCOL_VERSION', 'CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT',
    'CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN', 'CHRIPTMAS_WORKER_SECRET',
    'CHRIPTMAS_WORKER_INSTANCE_ID', 'CHRIPTMAS_WORKER_PORT',
    'CHRIPTMAS_RUNTIME_ROOT_VERSION', 'CHRIPTMAS_RUNTIME_ROOT_REVISION',
    'CHRIPTMAS_RUNTIME_VAULT_ROOT', 'CHRIPTMAS_RUNTIME_MODEL_ROOT', 'CHRIPTMAS_RUNTIME_MEDIA_ROOT',
)


def process_memory():
    """只记录当前测试进程的工作集，不读取登录或其他进程。"""
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD),
            *[(name, ctypes.c_size_t) for name in ('peak_working', 'working', 'peak_paged',
                'paged', 'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile')]]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    api = ctypes.WinDLL('psapi', use_last_error=True).GetProcessMemoryInfo
    api.argtypes, api.restype = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD], wintypes.BOOL
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    assert api(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
    return {'rss_bytes': counters.working, 'peak_rss_bytes': counters.peak_working}


def standard_event_loop():
    """先准备 Windows 标准自唤醒通道，再启用业务连接审计。"""
    loops, failures = [], []

    def prepare():
        try:
            loops.append(asyncio.new_event_loop())
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=prepare, name='skill-test-event-loop')
    thread.start()
    thread.join()
    if failures:
        raise failures[0]
    return loops[0]


@contextmanager
def original_lifespan(app, listener, event_loop):
    with listener:
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
            loop=lambda: event_loop, lifespan='on', access_log=False, log_level='error'))
        failures = []

        def run():
            try:
                server.run(sockets=[listener])
            except BaseException as error:
                failures.append(error)

        thread = Thread(target=run, name='skill-full-factory')
        thread.start()
        try:
            deadline = time.monotonic() + 40
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.01)
            assert server.started and thread.is_alive() and not failures
            assert not server.lifespan.startup_failed
            yield f'http://127.0.0.1:{port}'
        finally:
            server.should_exit = True
            thread.join(timeout=20)
            assert not thread.is_alive() and not failures
            assert not server.lifespan.shutdown_failed and not server.lifespan.error_occurred


@pytest.fixture
def original_factory(tmp_path, monkeypatch):
    assert os.name == 'nt'
    for key in STARTUP_ENVIRONMENT:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'desktop')
    monkeypatch.setenv('LITELLM_LOCAL_MODEL_COST_MAP', 'True')
    binaries, authentication = tmp_path / 'empty-bin', tmp_path / 'empty-auth'
    binaries.mkdir()
    authentication.mkdir()
    monkeypatch.setenv('PATH', str(binaries))
    monkeypatch.setenv('CODEX_HOME', str(authentication))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(authentication))
    configured_port = os.environ.get('CHRIPTMAS_SKILL_TEST_PORT')
    requested_port = 0 if configured_port is None else int(configured_port)
    assert configured_port is None or requested_port == 8018
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/settings.toml').write_bytes((ROOT / 'config/settings.toml.example').read_bytes())
    records, namespace = resolve_recognition_document_store(tmp_path)
    assert namespace == 'default'
    wire = {'calls': 0, 'messages': []}

    def completion(**request):
        wire['calls'] += 1
        wire['messages'] = request['messages']
        assert not request.get('stream')
        return {'choices': [{'message': {'content': json.dumps(draft(), ensure_ascii=False)},
            'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 11, 'completion_tokens': 7}}

    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=completion)
    models.update('generation', {'base_url': 'https://example.invalid/v1', 'model': 'synthetic-skill',
        'api_key': 'synthetic-test-value', 'allow_remote': True, 'expected_revision': 0})
    event_loop = standard_event_loop()
    listener = None
    audit = {'active': True, 'spawns': 0, 'connections': [], 'blocked_connections': []}

    def observe(event, arguments):
        if not audit['active']:
            return
        if event == 'subprocess.Popen':
            audit['spawns'] += 1
            raise RuntimeError('技能消费者验收不启动原生客户端')
        if event == 'socket.connect':
            address = arguments[1]
            if not isinstance(address, tuple) or address[:2] != ('127.0.0.1', port):
                audit['blocked_connections'].append(address[1] if isinstance(address, tuple) else None)
                raise RuntimeError(f'业务连接只允许本测试端口 {port}')
            audit['connections'].append(port)

    try:
        listener = socket.socket()
        listener.bind(('127.0.0.1', requested_port))
        port = listener.getsockname()[1]
        assert port not in {8001, 4173}
        monkeypatch.setenv('CHRIPTMAS_MCP_BACKEND_URL', f'http://127.0.0.1:{port}')
        sys.addaudithook(observe)
        from backend.memory_app.app import create_app
        app = create_app(runtime_root=tmp_path, model_configuration=models)
        assert Path(app.state.container.root_dir).resolve() == tmp_path.resolve()
        assert app.state.recognition_models is models
        assert app.state.recognition_records.database_path == records.database_path
        store = MemoryTurn.store_for(app.state.recognition_records)
        with original_lifespan(app, listener, event_loop) as endpoint:
            assert store._path == app.state.ai_turn_store._path
            with httpx.Client(base_url=endpoint, trust_env=False, timeout=30) as client:
                yield SimpleNamespace(app=app, records=app.state.recognition_records,
                    service=app.state.recognition_service, models=models, store=store,
                    wire=wire, client=client, root=tmp_path, audit=audit)
            assert app.state.ai_turn_runner.active_turn_ids == ()
        assert app.state.ai_turn_runner.shutdown(timeout_seconds=5) == ()
        assert audit['spawns'] == 0 and audit['blocked_connections'] == []
        assert audit['connections'] and set(audit['connections']) == {port}
        assert not tuple(authentication.iterdir()) and not tuple(binaries.iterdir())
    finally:
        audit['active'] = False
        if listener is not None:
            listener.close()
        if not event_loop.is_closed():
            event_loop.close()
        (tmp_path / 'process-memory.json').write_text(json.dumps({**process_memory(),
            'spawn_calls': audit['spawns'], 'allowed_connection_ports': audit['connections'],
            'blocked_connection_ports': audit['blocked_connections']}), encoding='utf-8')


def source(actual, **kwargs):
    return method((actual.records, actual.service, None), **kwargs)


def post(actual, path, body, status=200):
    response = actual.client.post(path, json=body)
    assert response.status_code == status, response.text
    return response


def test_original_factory_manual_off_review_zip_folder_and_source_update(original_factory):
    actual = original_factory
    actual.models.update('generation', {'allow_remote': False, 'expected_revision': 1})
    one = source(actual)
    before = actual.records.read('recognitions', one.id)
    sources = [{'id': one.id, 'revision': one.revision}]
    assert actual.client.get(BASE).json()['generation_available'] is False
    rejected = post(actual, BASE + '/generate', {'sources': sources, 'key': 'synthetic-disabled'}, 403)
    assert rejected.json()['detail'] == 'skill_generation_disabled'
    assert actual.records.list('v2_memory_turn_keys') == () and actual.wire['calls'] == 0
    assert [row['id'] for row in actual.client.get(BASE + '/methods').json()['items']] == [one.id]
    saved = post(actual, BASE, {'sources': sources, 'document': draft()}).json()
    assert saved['reviewed'] is False and saved['needs_update'] is False
    path = BASE + '/' + saved['id']
    post(actual, path + '/download', {'expected_revision': 1}, 409)
    assert actual.client.get('/api/v2/projects/beta/skill-exports/' + saved['id']).status_code == 404
    reviewed = post(actual, path + '/review', {'expected_revision': 1}).json()
    assert reviewed['revision'] == 2 and reviewed['reviewed'] is True
    post(actual, path + '/download', {'expected_revision': 1}, 409)
    archive_response = post(actual, path + '/download', {'expected_revision': 2})
    assert archive_response.headers['content-type'] == 'application/zip'
    with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    assert set(files) == {'choose-gift/SKILL.md', 'choose-gift/references/methods.md'}
    skill = files['choose-gift/SKILL.md'].decode('utf-8')
    header = yaml.safe_load(skill.split('---', 2)[1])
    assert header['name'] == 'choose-gift' and header['description'] == draft()['description']
    assert '1. 询问预算和最近愿望。 [1]' in skill
    assert one.id in files['choose-gift/references/methods.md'].decode('utf-8')
    directory = actual.root / 'selected-folder'
    directory.mkdir()
    folder_body = {'expected_revision': 3, 'directory': str(directory),
        'confirm_first_export': False, 'expected_confirmation_revision': 0}
    post(actual, path + '/folder', folder_body, 409)
    assert not tuple(directory.iterdir())
    post(actual, path + '/folder', {**folder_body,
        'confirm_first_export': True, 'expected_confirmation_revision': 1}, 409)
    assert not tuple(directory.iterdir())
    written = post(actual, path + '/folder', {**folder_body, 'confirm_first_export': True}).json()
    assert written['revision'] == 4 and written['exported_revision'] == 3
    assert written['folder_path'] == str(directory / 'choose-gift')
    assert {p.relative_to(directory).as_posix(): p.read_bytes()
        for p in directory.rglob('*') if p.is_file()} == files
    assert actual.client.get(BASE).json()['local_folder'] == {
        'available': True, 'confirmed': True, 'revision': 1}
    after = actual.records.read('recognitions', one.id)
    assert after.revision == before.revision and after.payload == before.payload
    changed = actual.service.revise(scope=WorkScope('local-user', 'alpha'), recognition_id=one.id,
        expected_revision=one.revision, content='先核实预算，再询问近期愿望')
    assert actual.client.get(path).json()['needs_update'] is True
    post(actual, path + '/download', {'expected_revision': 4}, 409)
    post(actual, path + '/folder', {**folder_body, 'expected_revision': 4,
        'expected_confirmation_revision': 1}, 409)
    regenerated = post(actual, path + '/regenerate', {'expected_revision': 4,
        'sources': [{'id': changed.id, 'revision': changed.revision}], 'document': draft()}).json()
    assert regenerated['reviewed'] is False and regenerated['needs_update'] is False
    post(actual, path + '/download', {'expected_revision': 5}, 409)
    assert actual.wire['calls'] == 0 and actual.records.list('v2_memory_turn_keys') == ()


def test_original_factory_generated_aux_receipt_and_replay_preserve_user_edit(original_factory):
    actual, one = original_factory, source(original_factory)
    original = actual.records.read('recognitions', one.id)
    body = {'sources': [{'id': one.id, 'revision': one.revision}], 'key': 'synthetic-full-factory'}
    saved = post(actual, BASE + '/generate', body).json()
    assert saved['document'] == draft() and saved['reviewed'] is False
    assert actual.wire['calls'] == 1
    assert one.content in json.dumps(actual.wire['messages'], ensure_ascii=False)
    identity = saved['generation_turn_id']
    request = actual.store.get_request(identity)
    assert request == actual.records.read('v2_memory_turn_keys', identity).payload['request']
    assert request['desired_outcome'] == 'memory.skill_export'
    assert request['execution_policy']['purpose'] == 'aux'
    assert request['privacy']['material_refs'] == [{'type': 'recognition', 'id': one.id,
        'revision': one.revision, 'project_id': 'alpha'}]
    events = tuple(actual.store.events_after(identity))
    assert events[-1]['type'] == 'turn.completed'
    assert len([event for event in events if event['type'] == 'model.completed']) == 1
    dispatched = [event for event in events if event['type'] == 'model.attempt.dispatched']
    terminals = [event for event in events if event['type'] == 'model.attempt.terminal']
    assert len(dispatched) == len(terminals) == 1
    receipt = actual.store.get(terminals[0]['data']['receipt_ref'])
    assert receipt['status'] == 'succeeded'
    path = BASE + '/' + saved['id']
    post(actual, path + '/download', {'expected_revision': 1}, 409)
    edited = {**draft(), 'description': '用户审阅后的描述。'}
    response = actual.client.patch(path, json={'expected_revision': 1, 'document': edited})
    assert response.status_code == 200 and response.json()['reviewed'] is False
    repeated = post(actual, BASE + '/generate', body).json()
    assert repeated['id'] == saved['id'] and repeated['document'] == edited
    assert repeated['revision'] == 2 and actual.wire['calls'] == 1
    assert len(actual.records.list('v2_skill_exports')) == 1
    post(actual, path + '/review', {'expected_revision': 2})
    assert post(actual, path + '/download', {'expected_revision': 3}).content.startswith(b'PK')
    after = actual.records.read('recognitions', one.id)
    assert after.revision == original.revision and after.payload == original.payload


def test_original_factory_aux_terminal_failure_refuses_cached_export(original_factory):
    actual, one = original_factory, source(original_factory)
    with sqlite3.connect(actual.store._path) as connection:
        connection.execute("""CREATE TRIGGER reject_skill_terminal BEFORE INSERT ON ai_turn_events
            WHEN json_extract(NEW.event_json, '$.type') = 'turn.completed'
            BEGIN SELECT RAISE(ABORT, 'synthetic terminal failure'); END""")
    body = {'sources': [{'id': one.id, 'revision': one.revision}], 'key': 'synthetic-terminal-failure'}
    first = post(actual, BASE + '/generate', body, 409)
    repeated = post(actual, BASE + '/generate', body, 409)
    assert first.json()['detail'] == repeated.json()['detail'] == 'skill_generation_origin_invalid'
    assert actual.wire['calls'] == 1 and actual.records.list('v2_skill_exports') == ()
    assert len(actual.records.list('v2_memory_turn_keys')) == 1
