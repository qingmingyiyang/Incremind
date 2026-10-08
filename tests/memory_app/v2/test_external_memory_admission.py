"""本地记忆准入保留真实依赖缺口，协议负控只替代外部服务。"""
import importlib
import json
import os
import sys
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI


def admission():
    return importlib.import_module('backend.memory_app.v2.external_memory_admission')


def config():
    return {'mcpServers': {'chriptmas-memory': {
        'command': sys.executable, 'args': ['-I', '-m', 'backend.memory_app.mcp']}}}


@pytest.mark.parametrize('routes', [False, True])
@pytest.mark.parametrize('entry', ['require_memory_service', 'check_memory_configuration'])
def test_current_missing_dependency_rejects_without_spawning(routes, entry):
    module = admission()
    app = FastAPI()
    if routes:
        for name in ('projects', 'recall', 'methods', 'read', 'remember', 'propose_insight', 'report_use'):
            app.add_api_route('/api/v2/external-agent/mcp/' + name, lambda: {}, methods=['POST'])
    spawned = []
    active = [True]

    def audit(event, args):
        if active[0] and event == 'subprocess.Popen':
            spawned.append(args[0])

    sys.addaudithook(audit)
    try:
        with pytest.raises(module.MemoryAdmissionError, match='^external_runner_memory_unavailable$'):
            getattr(module, entry)(app, config=config(), client='codex', environment={})
    finally:
        active[0] = False
    assert spawned == []


@pytest.mark.parametrize('proof', [True, {}, {'turn_id': 'foreign'}, {
    'turn_id': 'foreign', 'owner_id': 'local-user', 'client': 'codex',
    'immutable_ref': 'foreign-input', 'outcome_ref': 'foreign-output'}])
def test_caller_proof_cannot_replace_original_context(proof):
    module = admission()
    app = FastAPI()
    app.state.external_context = SimpleNamespace(owner_id='local-user')
    with pytest.raises(module.MemoryAdmissionError, match='^external_runner_memory_unavailable$'):
        module.revalidate_memory_service(app, proof, client='codex')


@pytest.mark.parametrize('environment,aliases', [
    ({'PYTHONPATH': 'injected'}, {}),
    ({'PATH': 'injected'}, {}),
    ({}, {'CHRIPTMAS_DEVICE_KEY': '${APPROVED_DEVICE_KEY}'}),
    ({'APPROVED_DEVICE_KEY': 'synthetic-value'}, {'PYTHONPATH': '${APPROVED_DEVICE_KEY}'}),
    ({'HOME': 1}, {}),
])
def test_environment_rejects_injection_or_missing_approved_alias(environment, aliases):
    module = admission()
    with pytest.raises(module.MemoryAdmissionError, match='^external_runner_memory_unavailable$'):
        module._environment(environment, aliases)


def test_sdk_inherited_environment_is_masked_and_aliases_use_only_supplied_memory():
    module = admission()
    from mcp.client.stdio import DEFAULT_INHERITED_ENV_VARS
    result = module._environment({'HOME': 'synthetic-home', 'APPROVED_DEVICE_KEY': 'synthetic-value'},
        {'CHRIPTMAS_DEVICE_KEY': '${APPROVED_DEVICE_KEY}'})
    assert result['HOME'] == 'synthetic-home'
    assert result['CHRIPTMAS_DEVICE_KEY'] == 'synthetic-value'
    assert all(result[key] == '' for key in DEFAULT_INHERITED_ENV_VARS if key != 'HOME')


def expected_tools():
    from mcp import types
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['turn_id', 'result'],
        'properties': {'turn_id': {'type': 'string', 'minLength': 1}, 'result': {'type': 'object'}}}
    return [types.Tool(name=name, inputSchema={'type': 'object'}, outputSchema=schema,
        annotations=types.ToolAnnotations(readOnlyHint=name in {'projects', 'recall', 'methods', 'read'},
            destructiveHint=False, openWorldHint=True))
        for name in ('projects', 'recall', 'methods', 'read', 'remember', 'propose_insight', 'report_use')]


def fake_server(tmp_path, change):
    """临时外部进程提供协议输入，原 SDK 和被测交换过程保持真实。"""
    tools = [tool.model_dump(by_alias=True, exclude_none=True) for tool in expected_tools()]
    if change == 'missing_tool':
        tools.pop()
    elif change == 'duplicate_tool':
        tools.append(deepcopy(tools[0]))
    elif change == 'changed_schema':
        tools[0]['inputSchema'] = {'type': 'object', 'additionalProperties': True}
    elif change == 'changed_permission':
        tools[0]['annotations']['readOnlyHint'] = False
    payload = {'turn_id': 'synthetic-turn', 'result': {}}
    if change == 'unknown_field':
        payload['unexpected'] = True
    if change == 'wrong_type':
        payload['turn_id'] = 1
    source = tmp_path / 'server.py'
    source.write_text('import json,sys,time,os\nPID_PATH=' + repr(str(tmp_path / 'server.pid')) +
        '\nopen(PID_PATH,"w").write(str(os.getpid()))\nTOOLS=' + repr(tools) + '\nPAYLOAD=' + repr(payload) +
        '\nCHANGE=' + repr(change) + '\n' + '''for line in sys.stdin:
    request=json.loads(line)
    if 'id' not in request: continue
    if CHANGE == 'timeout': time.sleep(30)
    method=request['method']
    if method == 'initialize':
        result={'protocolVersion':request['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'chriptmas-memory','version':'1.0.0'}}
    elif method == 'tools/list': result={'tools':TOOLS}
    else: result={'content':[{'type':'text','text':json.dumps(PAYLOAD)}],'structuredContent':PAYLOAD,'isError':False}
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
''', encoding='utf-8')
    from mcp.client.stdio import StdioServerParameters
    return StdioServerParameters(command=sys._base_executable, args=['-I', str(source)],
        env=admission()._environment({}, {}), cwd=str(tmp_path))


def assert_process_ended(tmp_path):
    """持有同步句柄核实际退出；失败时只清理本测试启动的临时服务。"""
    pid = int((tmp_path / 'server.pid').read_text())
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x100000 | 1, False, pid)
        if not handle:
            assert ctypes.get_last_error() == 87
            return
        try:
            ended = api.WaitForSingleObject(handle, 0) == 0
            if not ended:
                api.TerminateProcess(handle, 1)
                api.WaitForSingleObject(handle, 1000)
            assert ended
        finally:
            api.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        os.kill(pid, 9)
        pytest.fail('temporary MCP process remained alive')


@pytest.mark.parametrize('change', ['missing_tool', 'duplicate_tool', 'changed_schema',
    'changed_permission', 'unknown_field', 'wrong_type', 'timeout'])
def test_real_sdk_protocol_negative_and_bounded_close(tmp_path, change):
    module = admission()
    before = time.monotonic()
    with pytest.raises(module.MemoryAdmissionError, match='^external_runner_memory_unavailable$'):
        module._exchange(fake_server(tmp_path, change), 'codex', expected_tools())
    assert time.monotonic() - before < 10
    assert_process_ended(tmp_path)


def test_real_sdk_exchange_is_not_an_owner_proof(tmp_path):
    module = admission()
    response = module._exchange(fake_server(tmp_path, 'valid'), 'codex', expected_tools())
    assert_process_ended(tmp_path)
    assert response == {'turn_id': 'synthetic-turn', 'result': {}}
    app = FastAPI()
    with pytest.raises(module.MemoryAdmissionError, match='^external_runner_memory_unavailable$'):
        module._qualify(app, response, client='codex')


def test_foreign_turn_rejected_by_real_original_store_without_writes(tmp_path):
    module = admission()
    from backend.memory_app.v2.external_context import DELIVERIES, ExternalContext
    from core.ai_kernel import SQLiteAITurnStore, ScopedCapabilityRegistry
    from core.document_engine.sqlite_runtime import SQLiteDocumentRepository
    from core.storage_provider import SQLiteStructuredRecordStore
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    turns = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    context = ExternalContext(records, owner_id='local-user', documents=SQLiteDocumentRepository(records))
    context.install(ScopedCapabilityRegistry(), turns)
    app = FastAPI()
    app.state.external_context = context
    proof = {'turn_id': 'foreign-turn', 'owner_id': 'local-user', 'client': 'codex',
        'immutable_ref': 'foreign-input', 'outcome_ref': 'foreign-output'}
    with pytest.raises(module.MemoryAdmissionError, match='^external_runner_memory_unavailable$'):
        module.revalidate_memory_service(app, proof, client='codex')
    assert records.list(DELIVERIES) == ()
    assert turns.events_after('foreign-turn') == ()


def test_missing_sdk_dependency_uses_fixed_error_without_thread_trace(monkeypatch, tmp_path, capsys):
    module = admission()
    server = fake_server(tmp_path, 'valid')
    tools = expected_tools()
    monkeypatch.setitem(sys.modules, 'jsonschema', None)
    with pytest.raises(module.MemoryAdmissionError, match='^external_runner_memory_unavailable$'):
        module._exchange(server, 'codex', tools)
    output = capsys.readouterr()
    assert output.out == '' and output.err == ''
