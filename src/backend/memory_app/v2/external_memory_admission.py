"""本地记忆探测只产生一次原交付，后续复验不再次请求目录。"""
from collections.abc import Mapping
from copy import deepcopy
from datetime import timedelta
import importlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
from threading import Thread, get_ident

from ..transaction_records import TransactionRecords
from .external_agent_guard import ExternalAgentGuard
from .external_context import DELIVERIES, ExternalContext
from .external_workspace import validate_memory_mcp_config, validate_task_path


_ERROR = 'external_runner_memory_unavailable'
_TOOLS = {'projects', 'recall', 'methods', 'read', 'remember', 'propose_insight', 'report_use'}
_ENVIRONMENT = {'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'HOME', 'USERPROFILE',
    'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY',
    'CHRIPTMAS_DEVICE_KEY', 'DEVICE_KEY', 'APPROVED_DEVICE_KEY'}
_CREDENTIALS = {'CHRIPTMAS_DEVICE_KEY', 'DEVICE_KEY', 'APPROVED_DEVICE_KEY'}
_PROOF = {'turn_id', 'owner_id', 'client', 'immutable_ref', 'outcome_ref'}


class MemoryAdmissionError(ValueError):
    """错误只保留固定码，不携带协议正文、路径或环境。"""


def _deny():
    raise MemoryAdmissionError(_ERROR)


def _environment(environment, aliases):
    """覆盖 SDK 默认继承项，凭据只从宿主已批准的内存映射取得。"""
    if (not isinstance(environment, Mapping) or not isinstance(aliases, Mapping)
            or any(key not in _ENVIRONMENT or not isinstance(value, str) or '\x00' in value
                for key, value in environment.items())):
        _deny()
    from mcp.client.stdio import DEFAULT_INHERITED_ENV_VARS
    result = {key: '' for key in DEFAULT_INHERITED_ENV_VARS}
    result.update(environment)
    for target, reference in aliases.items():
        if (target not in _CREDENTIALS or not isinstance(reference, str)
                or not reference.startswith('${') or not reference.endswith('}')):
            _deny()
        source = reference[2:-1]
        if source not in _CREDENTIALS or not environment.get(source):
            _deny()
        result[target] = environment[source]
    return result


def _preflight(application, config, client, environment):
    if client not in {'claude', 'codex'}:
        _deny()
    canonical = json.loads(validate_memory_mcp_config(config))
    server = canonical['mcpServers']['chriptmas-memory']
    if 'command' not in server:
        _deny()
    # 路由和受控模块都必须真实存在，缺依赖时不导入 SDK 或启动进程。
    from fastapi.routing import APIRoute
    for name in _TOOLS:
        routes = [route for route in application.routes
            if isinstance(route, APIRoute) and route.path == '/api/v2/external-agent/mcp/' + name]
        if len(routes) != 1 or routes[0].methods != {'POST'}:
            _deny()
    expected = Path(__file__).resolve().parent.parent / 'mcp.py'
    specification = importlib.util.find_spec('backend.memory_app.mcp')
    if (specification is None or specification.origin is None
            or Path(specification.origin).resolve() != expected or not expected.is_file()):
        _deny()
    validate_task_path(expected)
    context = _context(application)
    if not callable(getattr(context, 'prepare_projects', None)):
        _deny()
    validate_task_path(Path(server['command']))
    # SDK 在启动后才绑定 Job；Windows venv 启动器可能已创建未绑定的原生子进程。
    if os.name == 'nt' and Path(sys.executable).resolve() != Path(sys._base_executable).resolve():
        _deny()
    native = importlib.import_module('backend.memory_app.mcp')
    tools = native._tools()
    if len(tools) != len(_TOOLS) or {tool.name for tool in tools} != _TOOLS:
        _deny()
    projects = next(tool for tool in tools if tool.name == 'projects')
    if projects.outputSchema['properties']['result']['properties']['version'].get('const') != 'handoff@2':
        _deny()
    from mcp.client.stdio import StdioServerParameters
    return StdioServerParameters(command=server['command'], args=server['args'],
        env=_environment(environment, server.get('env', {})), cwd=str(expected.parent)), tools


class _QuietProbe(logging.Filter):
    def __init__(self, thread_id):
        super().__init__()
        self.thread_id = thread_id

    def filter(self, record):
        return record.thread != self.thread_id


def _exchange(server, client, expected_tools):
    """真实 SDK 交换由短期线程承接，关闭完成后才返回，日志不保存正文。"""
    try:
        import anyio
        from jsonschema import Draft202012Validator
        from mcp import ClientSession, types
        from mcp.client.stdio import stdio_client
    except Exception:
        raise MemoryAdmissionError(_ERROR) from None
    result, failures = [], []

    def run():
        async def probe():
            # stderr 丢弃；SDK 解析异常仅在本线程关闭日志，不影响其他会话。
            with open(os.devnull, 'w', encoding='utf-8') as errlog:
                with anyio.fail_after(3):
                    async with stdio_client(server, errlog=errlog) as (reader, writer):
                        async with ClientSession(reader, writer,
                            read_timeout_seconds=timedelta(seconds=2),
                            client_info=types.Implementation(name=client, version='1.0.0')) as session:
                            initialized = await session.initialize()
                            if (initialized.serverInfo.name != 'chriptmas-memory'
                                    or initialized.serverInfo.version != '1.0.0'
                                    or initialized.capabilities.tools is None):
                                _deny()
                            listing = await session.list_tools()
                            expected = {tool.name: tool.model_dump(by_alias=True, exclude_none=True)
                                for tool in expected_tools}
                            actual = {tool.name: tool.model_dump(by_alias=True, exclude_none=True)
                                for tool in listing.tools}
                            if (listing.nextCursor is not None or len(listing.tools) != len(expected)
                                    or actual != expected):
                                _deny()
                            response = await session.call_tool('projects', {})
                            data = response.structuredContent
                            schema = next(tool for tool in expected_tools if tool.name == 'projects').outputSchema
                            if (response.isError or not isinstance(data, dict)
                                    or not Draft202012Validator(schema).is_valid(data)
                                    or len(response.content) != 1
                                    or not isinstance(response.content[0], types.TextContent)
                                    or json.loads(response.content[0].text) != data):
                                _deny()
                            json.dumps(data, allow_nan=False)
                            result.append(deepcopy(data))

        quiet = _QuietProbe(get_ident())
        loggers = [logging.getLogger(name) for name in ('mcp.client.stdio', 'mcp.client.session',
            'mcp.shared.session', 'client.stdio.win32')]
        for logger in loggers:
            logger.addFilter(quiet)
        try:
            anyio.run(probe)
        except Exception:
            failures.append(True)
        finally:
            for logger in loggers:
                logger.removeFilter(quiet)

    thread = Thread(target=run, name='memory-admission')
    thread.start()
    thread.join()
    if failures or len(result) != 1:
        _deny()
    return result[0]


def _context(application):
    context = getattr(application.state, 'external_context', None)
    if (not isinstance(context, ExternalContext) or context.turns is None
            or context.guard.owner_id != context.owner_id or context.guard.records is not context.records):
        _deny()
    return context


def _qualification(application, turn_id, client):
    context = _context(application)
    if client not in {'claude', 'codex'} or not isinstance(turn_id, str) or not turn_id:
        _deny()
    ref, archive, outcome_ref = context._completed(turn_id)
    frozen = context.turns.get_request(turn_id)
    arguments = frozen['capability_request']['arguments']
    if (frozen != archive['request'] or archive['owner_id'] != context.owner_id
            or archive['turn_id'] != turn_id or arguments['client'] != client
            or arguments['tool'] != 'projects' or arguments['scope']['user_id'] != context.owner_id
            or archive['handoff']['version'] != 'handoff@2'):
        _deny()
    with context.records.begin() as tx:
        ExternalAgentGuard(TransactionRecords(tx), owner_id=context.owner_id,
            now=context.now).validate(turn_id, arguments)
        delivery = tx.read(DELIVERIES, turn_id)
        if (delivery is None or delivery.revision != 1
                or set(delivery.payload) != {'owner_id', 'turn_id', 'immutable_ref', 'outcome_ref', 'at'}
                or delivery.payload['owner_id'] != context.owner_id
                or delivery.payload['turn_id'] != turn_id or delivery.payload['immutable_ref'] != ref
                or delivery.payload['outcome_ref'] != outcome_ref):
            _deny()
    return {'turn_id': turn_id, 'owner_id': context.owner_id, 'client': client,
        'immutable_ref': ref, 'outcome_ref': outcome_ref}, archive['handoff']


def _qualify(application, response, *, client):
    try:
        if not isinstance(response, dict) or set(response) != {'turn_id', 'result'}:
            _deny()
        proof, handoff = _qualification(application, response['turn_id'], client)
        if response['result'] != handoff:
            _deny()
        return deepcopy(proof)
    except Exception:
        raise MemoryAdmissionError(_ERROR) from None


def check_memory_configuration(application, *, config, client, environment):
    """仅检查真实依赖和受控入口，不签交付证明、不派发或请求目录。"""
    try:
        _preflight(application, config, client, environment)
    except Exception:
        raise MemoryAdmissionError(_ERROR) from None


def require_memory_service(application, *, config, client, environment):
    """只请求一次目录，成功返回同一原根可复验的无正文交付证明。"""
    try:
        server, tools = _preflight(application, config, client, environment)
        return _qualify(application, _exchange(server, client, tools), client=client)
    except Exception:
        raise MemoryAdmissionError(_ERROR) from None


def revalidate_memory_service(application, proof, *, client):
    """回读冻结证明和当前原权限，不派发进程、不新增目录交付或配额。"""
    try:
        if (not isinstance(proof, Mapping) or set(proof) != _PROOF
                or any(not isinstance(value, str) or not value for value in proof.values())
                or proof['client'] != client):
            _deny()
        current, _ = _qualification(application, proof['turn_id'], client)
        if dict(proof) != current:
            _deny()
    except Exception:
        raise MemoryAdmissionError(_ERROR) from None
