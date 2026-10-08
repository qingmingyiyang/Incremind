"""Real MCP SDK stdio sessions against a synthetic loopback HTTP channel.

These tests verify transport/contracts, not Kernel delivery authority. Backend
integration uses the separately implemented real v2 owner.
"""
import asyncio
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sys
from threading import Thread

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[3]
TOOLS = ['projects', 'recall', 'methods', 'read', 'remember', 'propose_insight', 'report_use']
ARGUMENTS = {
    'projects': {},
    'recall': {'query': '合成问题', 'project': 'alpha', 'scene': 'reading', 'budget': 3000},
    'methods': {'situation': '挑礼物', 'project': 'alpha'},
    'read': {'id': {'turn_id': 'turn-origin', 'id': 'M1'}, 'window': {'start': 0, 'end': 4}},
    'remember': {'text': '合成原件', 'project': 'alpha'},
    'propose_insight': {'text': '合成待确认认识', 'conditions': ['挑礼物时'], 'project': 'alpha',
        'evidence_ids': [{'turn_id': 'turn-origin', 'id': 'M1'}]},
    'report_use': {'turn_id': 'turn-origin', 'ids': ['M1']},
}


def tool_result(tool, client):
    if tool in {'projects', 'recall', 'methods', 'read'}:
        value = {'version': 'handoff@2' if tool == 'projects' else 'handoff@1',
            'budget': 3000, 'entries': [], 'profile': [], 'tokens': 0, 'text': ''}
        if tool == 'projects':
            value['projects'] = []
        return value
    if tool == 'report_use':
        return {'turn_id': 'turn-origin', 'ids': ['M1']}
    value = {'receipt_id': 'synthetic-receipt', 'revision': 1, 'project_id': 'alpha',
        'client': client, 'state': 'staged' if tool == 'remember' else 'pending'}
    value['item_id' if tool == 'remember' else 'candidate_id'] = 'synthetic-object'
    if tool == 'remember':
        value['verified'] = False
    return value


@pytest.fixture
def channel():
    calls, replies = [], {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, payload))
            tool = self.path.rsplit('/', 1)[-1]
            status, data = replies.get(tool, (200, {
                'turn_id': None if tool in {'remember', 'propose_insight'} else 'turn-origin',
                'result': tool_result(tool, payload['client'])}))
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', calls, replies
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


async def session_call(endpoint, client_name, tmp_path, operation):
    environment = {**os.environ, 'PYTHONPATH': str(ROOT / 'src'),
        'CHRIPTMAS_APP_ROOT': str(tmp_path / 'isolated-runtime'), 'CHRIPTMAS_MCP_BACKEND_URL': endpoint}
    parameters = StdioServerParameters(command=sys.executable,
        args=['-m', 'backend.memory_app.mcp'], env=environment, cwd=str(ROOT))
    with (tmp_path / 'stdio-stderr.log').open('w', encoding='utf-8') as errors:
        async with stdio_client(parameters, errlog=errors) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=12),
                client_info=types.Implementation(name=client_name, version='synthetic-1')) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == 'chriptmas-memory'
                return await operation(session)


@pytest.mark.parametrize(('name', 'client'), [('claude', 'claude'), ('claude-code', 'claude'),
    ('Claude Code', 'claude'), ('codex', 'codex'), ('Codex', 'codex')])
def test_actual_sdk_lists_and_calls_exact_seven_tools(channel, tmp_path, name, client):
    endpoint, calls, _ = channel

    async def operation(session):
        listed = await session.list_tools()
        assert [tool.name for tool in listed.tools] == TOOLS
        for index, tool in enumerate(listed.tools):
            assert tool.annotations.readOnlyHint is (index < 4)
            assert tool.inputSchema['additionalProperties'] is False
            assert 'client' not in tool.inputSchema['properties']
            assert tool.outputSchema['additionalProperties'] is False
            result = await session.call_tool(tool.name, ARGUMENTS[tool.name])
            assert result.isError is False
            assert result.structuredContent['result'] == tool_result(tool.name, client)
            assert json.loads(result.content[0].text) == result.structuredContent

    asyncio.run(session_call(endpoint, name, tmp_path, operation))
    assert calls == [(f'/api/v2/external-agent/mcp/{tool}', {'client': client, 'arguments': ARGUMENTS[tool]})
        for tool in TOOLS]


def test_unknown_client_and_argument_override_never_reach_backend(channel, tmp_path):
    endpoint, calls, _ = channel

    async def unknown(session):
        result = await session.call_tool('projects', {})
        assert result.isError is True
        assert result.content[0].text == 'external_agent_client_unknown'

    asyncio.run(session_call(endpoint, 'unrecognised', tmp_path, unknown))

    async def invalid(session):
        for tool, arguments in [
            ('projects', {'client': 'claude'}),
            ('read', {'id': {'turn_id': 'turn-origin', 'id': 'M1'}, 'window': {'start': True, 'end': 4}}),
            ('read', {'id': 'M1'}),
            ('remember', {'text': 'sensitive-fixture', 'url': 'https://example.invalid'}),
            ('remember', {}),
            ('recall', {'query': 'sensitive-fixture', 'budget': True}),
        ]:
            result = await session.call_tool(tool, arguments)
            assert result.isError is True
            assert result.content[0].text == 'external_agent_arguments_invalid'
            assert 'sensitive-fixture' not in str(result)

    asyncio.run(session_call(endpoint, 'codex', tmp_path, invalid))
    assert calls == []


@pytest.mark.parametrize('before_first_tool', [True, False])
def test_initialize_client_name_is_immutable_before_and_after_first_tool(channel, tmp_path, before_first_tool):
    endpoint, calls, _ = channel

    async def operation(session):
        if not before_first_tool:
            assert (await session.call_tool('projects', {})).isError is False
        await session.send_request(types.ClientRequest(types.InitializeRequest(params=types.InitializeRequestParams(
            protocolVersion=types.LATEST_PROTOCOL_VERSION, capabilities=types.ClientCapabilities(),
            clientInfo=types.Implementation(name='claude', version='synthetic-2')))), types.InitializeResult)
        result = await session.call_tool('projects', {})
        assert result.isError is True
        assert result.content[0].text == 'external_agent_client_changed'

    asyncio.run(session_call(endpoint, 'codex', tmp_path, operation))
    assert len(calls) == (0 if before_first_tool else 1)
    assert all(payload['client'] == 'codex' for _, payload in calls)


def test_backend_unavailable_is_explicit_chinese_error(tmp_path):
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        endpoint = f'http://127.0.0.1:{reserved.getsockname()[1]}'

        async def operation(session):
            result = await session.call_tool('projects', {})
            assert result.isError is True
            assert result.content[0].text == '第二大脑未启动'

        asyncio.run(session_call(endpoint, 'codex', tmp_path, operation))


@pytest.mark.parametrize('endpoint', ['https://127.0.0.1:8001', 'http://localhost:8001',
    'http://example.invalid:8001', 'http://user:pass@127.0.0.1:8001', 'http://127.0.0.1:8001/?key=hidden'])
def test_non_numeric_loopback_or_credential_endpoint_is_rejected(tmp_path, endpoint):
    async def operation(session):
        result = await session.call_tool('projects', {})
        assert result.isError is True
        assert result.content[0].text == 'external_agent_backend_invalid'
        assert 'hidden' not in str(result)

    asyncio.run(session_call(endpoint, 'codex', tmp_path, operation))


def test_backend_errors_and_bad_envelope_are_safely_rejected(channel, tmp_path):
    endpoint, calls, replies = channel

    async def operation(session):
        replies['projects'] = (409, {'detail': 'external_agent_remote_blocked', 'private': 'sensitive-fixture'})
        result = await session.call_tool('projects', {})
        assert result.isError is True
        assert result.content[0].text == 'external_agent_remote_blocked'
        replies['projects'] = (500, {'detail': 'sensitive-fixture'})
        result = await session.call_tool('projects', {})
        assert result.isError is True
        assert result.content[0].text == 'external_agent_backend_failed'
        replies['projects'] = (200, {'turn_id': None, 'result': {'private': 'sensitive-fixture'}})
        result = await session.call_tool('projects', {})
        assert result.isError is True
        assert result.content[0].text == 'external_agent_backend_failed'
        assert 'sensitive-fixture' not in str(result)

    asyncio.run(session_call(endpoint, 'codex', tmp_path, operation))
    assert len(calls) == 3


def test_sdk_dependency_is_fixed_to_actual_v1():
    assert 'mcp==1.27.1' in (ROOT / 'requirements.txt').read_text().splitlines()


@pytest.mark.parametrize('tool', TOOLS)
def test_partial_backend_result_is_rejected_by_each_declared_output_schema(channel, tmp_path, tool):
    endpoint, calls, replies = channel
    replies[tool] = (200, {'turn_id': None if tool in {'remember', 'propose_insight'} else 'turn-origin',
        'result': {'unexpected': 'synthetic_unqualified_material'}})
    async def operation(session):
        result = await session.call_tool(tool, ARGUMENTS[tool])
        assert result.isError is True
        assert result.content[0].text == 'external_agent_backend_failed'
        assert 'synthetic_unqualified_material' not in str(result)
    asyncio.run(session_call(endpoint, 'codex', tmp_path, operation))
    assert len(calls) == 1
