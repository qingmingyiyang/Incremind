"""固定stdio记忆适配器：连接本机loopback或既有设备鉴权的HTTPS服务器。"""
from __future__ import annotations

import os
import json
import re
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import httpx
from jsonschema import Draft202012Validator
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage


_CLIENTS = {'claude': 'claude', 'claude-code': 'claude', 'Claude Code': 'claude',
            'codex': 'codex', 'Codex': 'codex', 'codex-mcp-client': 'codex',
            'dsh-mcp-client': 'deepseek-harness'}
_READ = ('projects', 'recall', 'methods', 'read')
_SAFE_ERRORS = frozenset({
    'external_agent_remote_blocked', 'external_agent_binding_invalid', 'external_agent_client_disabled',
    'external_agent_clock_invalid', 'external_agent_disabled', 'external_agent_material_changed',
    'external_agent_material_invalid', 'external_agent_private', 'external_agent_profile_disabled',
    'external_agent_quota_exhausted', 'external_agent_request_invalid', 'external_context_binding_invalid',
    'external_context_citations_invalid', 'external_context_material_changed', 'external_context_not_accepted',
    'external_context_not_completed', 'external_context_not_delivered', 'external_context_owner_changed',
    'external_context_selection_invalid', 'external_context_unavailable',
    'external_context_evidence_invalid', 'shared_forbidden', 'shared_project_unavailable',
})
_STRING = {'type': 'string', 'minLength': 1}
_REFERENCE = {'type': 'object', 'additionalProperties': False, 'required': ['turn_id', 'id'],
    'properties': {'turn_id': _STRING, 'id': {'type': 'string', 'pattern': '^[MP][1-9][0-9]*$'}}}
_SCOPE = {'project': _STRING, 'scene': _STRING}
_INPUTS = {
    'projects': ({}, []),
    'recall': ({'query': _STRING, **_SCOPE, 'budget': {'type': 'integer', 'minimum': 1, 'maximum': 12000}}, ['query']),
    'methods': ({'situation': _STRING, **_SCOPE}, ['situation']),
    'read': ({'id': _REFERENCE, 'window': {'type': 'object', 'additionalProperties': False,
        'required': ['start', 'end'], 'properties': {'start': {'type': 'integer', 'minimum': 0},
            'end': {'type': 'integer', 'minimum': 1}}}}, ['id']),
    'remember': ({'text': _STRING, 'url': _STRING, **_SCOPE}, []),
    'propose_insight': ({'text': _STRING, 'conditions': {'type': 'array', 'items': _STRING},
        **_SCOPE, 'evidence_ids': {'type': 'array', 'items': _REFERENCE, 'uniqueItems': True}}, ['text', 'project']),
    'report_use': ({'turn_id': _STRING, 'ids': {'type': 'array', 'items': {
        'type': 'string', 'pattern': '^[MP][1-9][0-9]*$'}, 'uniqueItems': True}}, ['turn_id', 'ids']),
}
_DESCRIPTIONS = {'projects': '列出可用项目和场景', 'recall': '召回带编号的记忆',
    'methods': '按情境补充方法', 'read': '下钻原交付编号的证据', 'remember': '记住原件，保持未核对；text与url二选一',
    'propose_insight': '提一条待确认认识', 'report_use': '回报原交付中使用的编号'}


def _output_schema(name, handoff, *, shared=False):
    if name in _READ:
        personal = deepcopy(handoff['oneOf'][1 if name == 'projects' else 0])
        result = {'oneOf': [personal, deepcopy(handoff['oneOf'][2 if name == 'projects' else 3])]} if shared else personal
    elif name == 'report_use':
        result = {'type': 'object', 'additionalProperties': False, 'required': ['turn_id', 'ids'],
            'properties': {'turn_id': _STRING, 'ids': {'type': 'array', 'uniqueItems': True,
                'items': {'type': 'string', 'pattern': '^[MP][1-9][0-9]*$'}}}}
    else:
        properties = {'receipt_id': _STRING, 'revision': {'type': 'integer', 'minimum': 1},
            'project_id': _STRING, 'client': {'enum': ['claude', 'codex', 'deepseek-harness', 'chatgpt']},
            'state': {'const': 'staged' if name == 'remember' else 'pending'}}
        properties['item_id' if name == 'remember' else 'candidate_id'] = _STRING
        if name == 'remember':
            properties['verified'] = {'const': False}
        result = {'type': 'object', 'additionalProperties': False,
            'required': list(properties), 'properties': properties}
        if shared:
            qualified = {**properties, 'scope_kind': {'const': 'shared'}, 'namespace': _STRING}
            result = {'oneOf': [result, {'type': 'object', 'additionalProperties': False,
                'required': list(qualified), 'properties': qualified}]}
    return {'$schema': handoff['$schema'], 'type': 'object', 'additionalProperties': False,
        'required': ['turn_id', 'result'], '$defs': deepcopy(handoff['$defs']),
        'properties': {'turn_id': _STRING if name in (*_READ, 'report_use') else {'type': 'null'},
            'result': result}}


def _input_schema(name, *, advertised=False, shared=False):
    properties, required = _INPUTS[name]
    scoped = shared and name in {'projects', 'recall', 'methods', 'remember', 'propose_insight'}
    writes = name in {'remember', 'propose_insight'}
    if scoped:
        properties = {**properties, 'scope_kind': {'enum': ['personal', 'shared']}}
        if writes:
            properties['idempotency_key'] = {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'}
    schema = {'type': 'object', 'additionalProperties': False,
        'properties': properties, 'required': required}
    # Claude 接口不接纳根级组合，真实调用仍由原 Schema 严格校验二选一。
    if name == 'remember' and not advertised:
        schema['oneOf'] = [{'required': ['text']}, {'required': ['url']}]
    if scoped and not advertised and name != 'projects':
        schema['if'] = {'required': ['scope_kind'], 'properties': {'scope_kind': {'const': 'shared'}}}
        schema['then'] = {'required': ['project', 'idempotency_key'] if writes else ['project']}
        if writes:
            schema['else'] = {'not': {'required': ['idempotency_key']}}
    return schema


def _tools(*, shared=False):
    result = []
    # Reuse the product's declared handoff contract; this opens no database.
    handoff = json.loads((Path(__file__).resolve().parents[3] /
        'core-contracts/ai/external-context-result.schema.json').read_text(encoding='utf-8'))
    for name in _INPUTS:
        schema = _input_schema(name, advertised=True, shared=shared)
        output = _output_schema(name, handoff, shared=shared)
        description = _DESCRIPTIONS[name]
        if shared and name in {'projects', 'recall', 'methods', 'remember', 'propose_insight'}:
            description += '。scope_kind=shared 选择共享项目'
            if name != 'projects':
                description += '，必须指定 project'
            if name in {'remember', 'propose_insight'}:
                description += ' 和 idempotency_key，重试沿用原键'
        result.append(types.Tool(name=name, description=description, inputSchema=schema,
            outputSchema=output, annotations=types.ToolAnnotations(readOnlyHint=name in _READ,
                destructiveHint=False, openWorldHint=True)))
    return result


def _backend_url(value):
    try:
        if not isinstance(value, str) or len(value) > 2048 or any(ord(character) < 33 or ord(character) == 127 for character in value):
            return None
        parsed = urlsplit(value)
        if (not ((parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', '::1'})
                or (parsed.scheme == 'https' and bool(parsed.hostname)))
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
            return None
        # Accessing port checks its syntax and range without resolving any host.
        parsed.port
        return value.rstrip('/')
    except (TypeError, ValueError):
        return None


def _error(code):
    return types.CallToolResult(content=[types.TextContent(type='text', text=code)], isError=True)


async def _serve(reader, writer, client, backend):
    server = Server('chriptmas-memory', version='1.0.0')
    tools = {tool.name: tool for tool in _tools()}
    binding = {'initialized': False, 'name': None, 'changed': False}

    @server.list_tools()
    async def list_tools():
        return list(tools.values())

    # Run the exact declared schemas ourselves so validation errors cannot echo
    # rejected material. The SDK still owns protocol and result validation.
    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        parameters = server.request_context.session.client_params
        actual = parameters.clientInfo.name if parameters else None
        if binding['changed'] or actual != binding['name']:
            return _error('external_agent_client_changed')
        client_id = _CLIENTS.get(binding['name'])
        if client_id is None:
            return _error('external_agent_client_unknown')
        tool = tools.get(name)
        if tool is None or not Draft202012Validator(_input_schema(name)).is_valid(arguments):
            return _error('external_agent_arguments_invalid')
        if name == 'read' and 'window' in arguments and arguments['window']['start'] >= arguments['window']['end']:
            return _error('external_agent_arguments_invalid')
        if backend is None:
            return _error('external_agent_backend_invalid')
        try:
            response = await client.post(backend + '/api/v2/external-agent/mcp/' + name,
                json={'client': client_id, 'arguments': arguments})
        except httpx.RequestError:
            return _error('第二大脑未启动')
        try:
            data = response.json()
        except ValueError:
            return _error('external_agent_backend_failed')
        if not 200 <= response.status_code < 300:
            detail = data.get('detail') if isinstance(data, dict) else None
            return _error(detail if isinstance(detail, str) and detail in _SAFE_ERRORS
                else 'external_agent_backend_failed')
        if not Draft202012Validator(tool.outputSchema).is_valid(data):
            return _error('external_agent_backend_failed')
        return data

    async def forward(destination):
        async with reader, destination:
            async for message in reader:
                request = message.message.root if isinstance(message, SessionMessage) else None
                if isinstance(request, types.JSONRPCRequest) and request.method == 'initialize':
                    parameters = request.params if isinstance(request.params, dict) else {}
                    info = parameters.get('clientInfo')
                    name = info.get('name') if isinstance(info, dict) else None
                    if not binding['initialized']:
                        binding.update(initialized=True, name=name)
                    elif name != binding['name']:
                        binding['changed'] = True
                # Forward the SDK-parsed message unchanged. SDK ServerSession
                # performs the complete initialization and protocol validation.
                await destination.send(message)

    sender, incoming = anyio.create_memory_object_stream(0)
    async with incoming, anyio.create_task_group() as group:
        group.start_soon(forward, sender)
        await server.run(incoming, writer, server.create_initialization_options())
        group.cancel_scope.cancel()


async def _main():
    backend = _backend_url(os.environ.get('CHRIPTMAS_MCP_BACKEND_URL', 'http://127.0.0.1:8001'))
    headers = {}
    key = os.environ.get('CHRIPTMAS_DEVICE_KEY', '')
    if backend is not None and (urlsplit(backend).scheme == 'https' or key):
        if not key or len(key) > 4096 or any(not 33 <= ord(character) <= 126 for character in key):
            backend = None
        else:
            headers['Authorization'] = 'Bearer ' + key
    target = os.environ.get('CHRIPTMAS_MCP_TARGET_USER')
    if target is not None:
        if not key or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._~-]{0,127}', target):
            backend = None
        else:
            headers['x-chriptmas-target-user'] = target
    async with httpx.AsyncClient(headers=headers, trust_env=False, follow_redirects=False,
        timeout=httpx.Timeout(150, connect=5)) as client, stdio_server() as (reader, writer):
        await _serve(reader, writer, client, backend)


if __name__ == '__main__':
    anyio.run(_main)
