"""Body-free desktop connection instructions; no client configuration is written."""
import json
from pathlib import Path
import shlex
import sys

from fastapi import HTTPException

from backend.security.device_identity import server_mode
from ..workspace_contracts import _project


_SOURCE_ROOT = Path(__file__).resolve().parents[3]
_VERSION = 'mcp-connection@1'


def _command(client, executable, source, backend, shell):
    quote = (lambda value: "'" + value.replace("'", "''") + "'") if shell == 'powershell' else shlex.quote
    arguments = [client, 'mcp', 'add']
    if client == 'claude':
        arguments += ['--transport', 'stdio']
    arguments += ['chriptmas-memory', '--env', 'PYTHONPATH=' + source, '--env',
                  'CHRIPTMAS_MCP_BACKEND_URL=' + backend, '--', executable,
                  '-m', 'backend.memory_app.mcp']
    return ' '.join(quote(value) if index in (arguments.index('--env') + 1,
        arguments.index('--env') + 3, arguments.index('--') + 1) else value
        for index, value in enumerate(arguments))


def connection_metadata(request, project_id):
    project = _project(project_id)
    if server_mode(request):
        return {'available': False}
    server = request.scope.get('server')
    if (not isinstance(server, (tuple, list)) or len(server) != 2
            or type(server[0]) is not str or server[0] not in {'127.0.0.1', '::1'}
            or type(server[1]) is not int or not 1 <= server[1] <= 65535):
        raise HTTPException(400, 'mcp_connection_unavailable')
    host = '[::1]' if server[0] == '::1' else server[0]
    backend = f'http://{host}:{server[1]}'
    shell = 'powershell' if sys.platform == 'win32' else 'sh'
    project_literal = json.dumps(project, ensure_ascii=False)
    instructions = (f'# {_VERSION}\n'
        f'开始任务先调用 methods(situation, project={project_literal}) 和 recall(query, project={project_literal})。\n'
        '引用交付的编号，保留对应 turn_id；需要原文时调用 read。\n'
        '结束时按各 turn_id 调用 report_use(turn_id, ids)，只回报实际用到的编号。')
    return {'available': True, 'version': _VERSION, 'project_id': project, 'shell': shell,
        'commands': {client: _command(client, sys.executable, str(_SOURCE_ROOT), backend, shell)
                     for client in ('claude', 'codex')}, 'instructions': instructions}
