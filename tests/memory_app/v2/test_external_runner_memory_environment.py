"""原批准环境的 caller 接线；原准入仍拒绝缺失依赖，不代签 MCP 资格。"""
from copy import deepcopy
import os
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI

from backend.memory_app.v2 import external_memory_admission as memory
from backend.memory_app.v2.external_context import ExternalContext
from backend.memory_app.v2.external_host import ExternalHostError, HostAdmission
from backend.memory_app.v2.external_process import run_process
from backend.memory_app.v2.external_runner import ExternalRunner, ExternalRunnerError
from backend.shared.deployment import DeploymentLayout
from core.ai_kernel import SQLiteAITurnStore, ScopedCapabilityRegistry
from core.document_engine.sqlite_runtime import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore


DEVICE = 'approved-synthetic-device-key'
MODEL = 'sk-' + 'M' * 24


@pytest.fixture
def owned(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    turns = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    context = ExternalContext(records, owner_id='local-user', documents=SQLiteDocumentRepository(records))
    context.install(ScopedCapabilityRegistry(), turns)
    host = HostAdmission(deployment=DeploymentLayout('desktop', tmp_path), owner_id='local-user',
        registrations={}, records=records, secret_environment={
            'APPROVED_DEVICE_KEY': DEVICE, 'OPENAI_API_KEY': MODEL, 'ANTHROPIC_API_KEY': MODEL})
    runner = ExternalRunner(records, owner_id='local-user', context=context, host=host, turns=turns)
    app = FastAPI()
    app.state.external_context = context
    app.state.external_execution_host = host
    app.state.external_runner = runner
    runner.application = app
    yield runner, host, records, turns, tmp_path
    host.close()


def config(target='DEVICE_KEY', source='APPROVED_DEVICE_KEY'):
    return {'mcpServers': {'chriptmas-memory': {'command': sys.executable,
        'args': ['-I', '-m', 'backend.memory_app.mcp'], 'env': {target: '${' + source + '}'}}}}


def captured_input(runner, entry, value, executor='codex'):
    """只观察真实原函数实参；不替换 Host、Runner、准入函数或准入结果。"""
    seen = []
    previous = sys.getprofile()

    def observe(frame, event, arg):
        if event == 'call' and frame.f_code is memory._preflight.__code__:
            seen.append(deepcopy(frame.f_locals['environment']))
        if previous is not None:
            previous(frame, event, arg)

    sys.setprofile(observe)
    try:
        # 空路由 FastAPI 夹具只提供原 _preflight 读取的接口库存；固定拒绝，不代签产品应用或服务资格。
        with pytest.raises(ExternalRunnerError, match='^external_runner_memory_unavailable$'):
            getattr(runner, entry)(value, executor)
    finally:
        sys.setprofile(previous)
    assert len(seen) == 1
    return seen[0]


@pytest.mark.parametrize('entry', ['_check_memory_configuration', '_require_memory'])
@pytest.mark.parametrize('executor', ['codex', 'claude-code'])
def test_approved_device_alias_reaches_two_original_admission_callers(owned, entry, executor):
    runner, host, records, turns, root = owned
    value = config()
    received = captured_input(runner, entry, value, executor)
    assert received == {'APPROVED_DEVICE_KEY': DEVICE}
    assert 'OPENAI_API_KEY' not in received and 'ANTHROPIC_API_KEY' not in received
    projected = memory._environment(received, value['mcpServers']['chriptmas-memory']['env'])
    assert projected['DEVICE_KEY'] == projected['APPROVED_DEVICE_KEY'] == DEVICE
    assert MODEL not in projected.values()

    from mcp.client.stdio import StdioServerParameters
    # SDK 参数与真实合成 CLI 只核环境消费，未运行握手或记忆交付。
    source = """import os,sys
assert os.environ['DEVICE_KEY']==os.environ['APPROVED_DEVICE_KEY']
assert 'OPENAI_API_KEY' not in os.environ and 'ANTHROPIC_API_KEY' not in os.environ
print('secret_received=True')
print(os.environ['DEVICE_KEY'])
print(os.environ['DEVICE_KEY'],file=sys.stderr)
"""
    parameters = StdioServerParameters(command=sys._base_executable, args=['-I', '-S', '-c', source],
        env=projected, cwd=str(root))
    result = run_process((parameters.command, *parameters.args), cwd=Path(parameters.cwd),
        environment=parameters.env, timeout=3, secret_values=(DEVICE, MODEL))
    assert result.status == 'completed' and result.exit_code == 0
    assert 'secret_received=True' in result.tail
    assert '[REDACTED_SECRET]' in result.tail
    assert DEVICE not in repr(result) and MODEL not in repr(result)
    assert host._secret_environment['APPROVED_DEVICE_KEY'] == DEVICE
    assert records.list('v2_external_runs') == () and turns.events_after('unstarted') == ()


@pytest.mark.parametrize('entry', ['_check_memory_configuration', '_require_memory'])
def test_process_environment_cannot_supply_unapproved_device_alias(owned, monkeypatch, entry):
    runner, host, _, _, _ = owned
    host._secret_environment = {}
    monkeypatch.setenv('APPROVED_DEVICE_KEY', DEVICE)
    received = captured_input(runner, entry, config())
    assert received == {}
    with pytest.raises(memory.MemoryAdmissionError, match='^external_runner_memory_unavailable$'):
        memory._environment(received, config()['mcpServers']['chriptmas-memory']['env'])


@pytest.mark.parametrize('entry', ['_check_memory_configuration', '_require_memory'])
@pytest.mark.parametrize('target,source', [('OPENAI_API_KEY', 'APPROVED_DEVICE_KEY'),
    ('DEVICE_KEY', 'OPENAI_API_KEY'), ('PYTHONPATH', 'APPROVED_DEVICE_KEY')])
def test_original_alias_whitelist_rejects_model_and_process_environment(owned, entry, target, source):
    runner, _, _, _, _ = owned
    value = config(target, source)
    received = captured_input(runner, entry, value)
    with pytest.raises(memory.MemoryAdmissionError, match='^external_runner_memory_unavailable$'):
        memory._environment(received, value['mcpServers']['chriptmas-memory']['env'])


@pytest.mark.parametrize('name', ['PATH', 'PYTHONPATH', 'NODE_OPTIONS'])
def test_host_approved_source_cannot_expand_to_complete_process_environment(owned, name):
    _, host, _, _, _ = owned
    host._secret_environment[name] = 'synthetic-injection'
    with pytest.raises(ExternalHostError, match='^external_host_environment_invalid$'):
        host._memory_environment()


def test_memory_environment_is_detached_from_original_approved_source(owned):
    _, host, _, _, _ = owned
    received = host._memory_environment()
    received['APPROVED_DEVICE_KEY'] = 'changed-caller-copy'
    received['PATH'] = 'changed-caller-copy'
    assert host._secret_environment['APPROVED_DEVICE_KEY'] == DEVICE
    assert 'PATH' not in host._secret_environment
    assert host._memory_environment() == {'APPROVED_DEVICE_KEY': DEVICE}
