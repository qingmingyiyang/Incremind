"""原完整工厂的记忆准入环境边界，不代签 portable 或真实 CLI。"""
import asyncio
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys
from threading import Thread
import time
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.storage_authority import resolve_recognition_document_store
from backend.memory_app.v2.external_agent_settings import (
    external_agent_settings, replace_external_agent_settings,
)
from backend.memory_app.v2.external_host import HostAdmission
from backend.memory_app.v2.external_memory_admission import MemoryAdmissionError, check_memory_configuration
from backend.memory_app.v2.external_runner import ExternalRunner, ExternalRunnerError
from backend.security.secrets import InMemorySecretStore
from tests.memory_app.v2.test_external_host import native_cli


ROOT = Path(__file__).resolve().parents[3]
STARTUP_ENVIRONMENT = (
    'CHRIPTMAS_DESKTOP_SESSION_MODE', 'CHRIPTMAS_DESKTOP_SESSION_SECRET',
    'CHRIPTMAS_DESKTOP_INSTANCE_ID', 'CHRIPTMAS_DESKTOP_NONCE',
    'CHRIPTMAS_DESKTOP_PROTOCOL_VERSION', 'CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT',
    'CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN', 'CHRIPTMAS_WORKER_SECRET',
    'CHRIPTMAS_WORKER_INSTANCE_ID', 'CHRIPTMAS_WORKER_PORT',
    'CHRIPTMAS_RUNTIME_ROOT_VERSION', 'CHRIPTMAS_RUNTIME_ROOT_REVISION',
    'CHRIPTMAS_RUNTIME_VAULT_ROOT', 'CHRIPTMAS_RUNTIME_MODEL_ROOT', 'CHRIPTMAS_RUNTIME_MEDIA_ROOT',
)


def memory_metadata():
    """只观测当前测试进程的 RSS，不读取其他进程、环境正文或凭据。"""
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


def event_loop_for_driver():
    """先在辅助线程准备标准事件循环的自唤醒通道，不放宽业务网络观察器。"""
    loops, failures = [], []

    def prepare():
        try:
            loops.append(asyncio.new_event_loop())
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=prepare, name='external-test-event-loop')
    thread.start()
    thread.join()
    if failures:
        raise failures[0]
    return loops[0]


@contextmanager
def original_lifespan(app, port, event_loop):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', port))
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, loop=lambda: event_loop,
            lifespan='on', access_log=False, log_level='error'))
        failures = []

        def run():
            try:
                server.run(sockets=[listener])
            except BaseException as error:
                failures.append(error)

        thread = Thread(target=run, name='external-full-factory')
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
    binaries, authentication = tmp_path / 'synthetic-bin', tmp_path / 'synthetic-auth'
    binaries.mkdir()
    authentication.mkdir()
    # 只有合成原生版本程序与空认证根，原发现、Host 和 installer 不替换。
    executable = native_cli(binaries / 'codex.exe', 'codex')
    monkeypatch.setenv('PATH', str(binaries))
    monkeypatch.setenv('CODEX_HOME', str(authentication))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(authentication))
    port = int(os.environ['CHRIPTMAS_EXTERNAL_TEST_PORT'])
    assert port == 8017
    endpoint = f'http://127.0.0.1:{port}'
    monkeypatch.setenv('CHRIPTMAS_MCP_BACKEND_URL', endpoint)
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/settings.toml').write_bytes((ROOT / 'config/settings.toml.example').read_bytes())
    records, namespace = resolve_recognition_document_store(tmp_path)
    assert namespace == 'default'
    provider_calls = []

    def no_paid_wire(**request):
        provider_calls.append(True)
        raise AssertionError('此准入环境控制不应调用模型')

    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=no_paid_wire)
    settings = external_agent_settings(records)
    replace_external_agent_settings(records,
        {key: value for key, value in settings.items() if key != 'revision'}
        | {'allow_remote': True, 'include_profile': False}, expected_revision=settings['revision'])
    event_loop = event_loop_for_driver()
    audit = {'active': True, 'spawns': 0, 'connections': [], 'blocked_connections': []}

    def observe(event, arguments):
        if not audit['active']:
            return
        if event == 'subprocess.Popen':
            audit['spawns'] += 1
        elif event == 'socket.connect':
            address = arguments[1]
            allowed = isinstance(address, tuple) and address[:2] == ('127.0.0.1', port)
            if not allowed:
                audit['blocked_connections'].append(address[1] if isinstance(address, tuple) else None)
                raise RuntimeError('测试只允许本任务 8017，不连接其他服务')
            audit['connections'].append(port)

    sys.addaudithook(observe)
    try:
        from backend.memory_app.app import create_app
        app = create_app(runtime_root=tmp_path, model_configuration=models)
        assert Path(app.state.container.root_dir).resolve() == tmp_path.resolve()
        host = app.state.external_execution_host
        assert isinstance(host, HostAdmission)
        assert host.registrations['codex'].executable == executable
        assert host.registrations['codex'].authentication_root == authentication
        assert not tuple(authentication.iterdir())
        with original_lifespan(app, port, event_loop) as actual_endpoint:
            state = app.state
            owner = state.external_runner
            assert isinstance(owner, ExternalRunner) and owner.host is host
            assert owner.context is state.external_context and owner.records is state.recognition_records
            assert owner.turns is state.ai_turn_store and owner.runtime is state.ai_runtime
            assert owner.frozen_authorization is not None
            yield SimpleNamespace(root=tmp_path, app=app, owner=owner, host=host,
                endpoint=actual_endpoint, records=state.recognition_records,
                turns=state.ai_turn_store, runner=state.ai_turn_runner, audit=audit,
                provider_calls=provider_calls)
            assert state.ai_turn_runner.active_turn_ids == ()
        assert state.ai_turn_runner.shutdown(timeout_seconds=5) == ()
    finally:
        audit['active'] = False
        if not event_loop.is_closed():
            event_loop.close()
        observed = {**memory_metadata(), 'spawn_calls': audit['spawns'],
            'allowed_connection_ports': audit['connections'],
            'blocked_connection_ports': audit['blocked_connections']}
        (tmp_path / 'process-memory.json').write_text(json.dumps(observed), encoding='utf-8')


def test_original_full_factory_shared_venv_refuses_memory_before_task_spawn(original_factory):
    actual = original_factory
    # 此节点明确使用当前共享 venv，固定拒绝不是 portable Python 产品失败。
    assert Path(sys.executable).resolve() != Path(sys._base_executable).resolve()
    with httpx.Client(base_url=actual.endpoint, trust_env=False, timeout=30) as client:
        response = client.post('/api/v2/external-agent/mcp/projects',
            json={'client': 'codex', 'arguments': {}})
        assert response.status_code == 200, response.text
    delivery = response.json()
    assert delivery['result']['version'] == 'handoff@2'
    qualified = actual.owner.context.qualified_delivery(delivery['turn_id'])
    assert qualified['owner_id'] == 'local-user' and qualified['client'] == 'codex'
    config = {'mcpServers': {'chriptmas-memory': {'command': sys.executable,
        'args': ['-I', '-m', 'backend.memory_app.mcp']}}}
    original_spawns = actual.audit['spawns']
    original_connections = list(actual.audit['connections'])
    before = actual.records.list_all()
    with pytest.raises(MemoryAdmissionError, match='^external_runner_memory_unavailable$'):
        check_memory_configuration(actual.app, config=config, client='codex', environment={})
    identity = 'turn-external-environment-control'
    with pytest.raises(ExternalRunnerError, match='^external_runner_memory_unavailable$'):
        actual.owner.prepare(identity, delivery_turn_id=delivery['turn_id'], executor='codex',
            cli_version='0.156.1', task='核查合成目录', mcp_config=config,
            session_id='session-external-environment', operation_id='external-environment',
            idempotency_key=identity, created_at=datetime.now(timezone.utc).isoformat())
    assert actual.audit['spawns'] == original_spawns == 0
    assert actual.audit['connections'] == original_connections
    assert actual.audit['blocked_connections'] == []
    assert actual.records.list_all() == before
    assert actual.turns.get_request(identity) is None
    assert actual.records.list('v2_external_task_preparations') == ()
    assert actual.records.list('v2_external_runs') == ()
    assert not (actual.root / 'agent_workspaces').exists()
    assert not (actual.host.registrations['codex'].executable.parent / 'version-called').exists()
    assert actual.provider_calls == []
