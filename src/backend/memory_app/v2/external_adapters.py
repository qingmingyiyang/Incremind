"""版本化 CLI 启动请求；LaunchPlan 不是外发授权或沙箱证明。"""
from dataclasses import dataclass, field
import json
from pathlib import Path
import re

from .external_workspace import validate_memory_mcp_config
from .policies import get


@dataclass(frozen=True)
class LaunchPlan:
    """运行器仍须核验版本探测、配置闭包、目录权限和实际 OS 隔离。"""
    executor: str
    adapter_version: str
    cli_version: str
    preset: str
    requested_commands: str
    command: tuple[str, ...]
    cwd: Path
    input_text: str = field(repr=False)
    environment_aliases: tuple[tuple[str, str], ...] = ()
    material_directory: Path | None = None
    input_policy: str | None = None
    stdin_text: str = field(default='', repr=False)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _path(value):
    return (isinstance(value, Path) and value.is_absolute() and '..' not in value.parts
        and '\x00' not in str(value))


def build_launch_plan(executor, *, cli_version, executable, cwd, task, mcp_config,
        preset='workspace', commands='disabled', memory_endpoint=None, material_directory=None):
    """仅构造固定 argv；任务进入 stdin，完整环境和权限准入留在 host。"""
    if (not isinstance(executor, str) or executor not in {'codex', 'claude-code'}
            or not _path(executable) or executable.suffix.lower() in {'.ps1','.cmd','.bat','.sh','.py','.js'}
            or not _path(cwd) or not isinstance(task, str) or not task.strip()
            or not isinstance(preset, str) or preset not in {'research','workspace','folder'}
            or not isinstance(commands, str) or commands not in {'disabled','sandboxed','explicit'}):
        raise ValueError('external_adapter_request_invalid')
    if preset == 'research' and commands != 'disabled':
        raise ValueError('external_adapter_commands_invalid')
    stdin_text, input_policy = task, None
    if material_directory is not None:
        if (preset != 'folder' or not _path(material_directory)
                or material_directory.parent != cwd / 'agent_workspaces'):
            raise ValueError('external_adapter_request_invalid')
        stdin_text = get('external_task_input', version='@1')(task,
            material_directory.relative_to(cwd).as_posix())
        input_policy = 'external_task_input@1'
    version = re.fullmatch(r'([0-9]{1,10})\.([0-9]{1,10})\.([0-9]{1,10})', cli_version) if isinstance(cli_version, str) else None
    if (version is None or (executor == 'codex' and cli_version != '0.156.1')
            or (executor == 'claude-code' and (version.group(1, 2) != ('2', '1')
                or int(version.group(3)) < 257))):
        raise ValueError('external_adapter_version_unsupported')
    # 复用任务目录 owner 的唯一 MCP 白名单，不在适配器复制来源校验。
    encoded = validate_memory_mcp_config(mcp_config, memory_endpoint=memory_endpoint)
    server = json.loads(encoded)['mcpServers']['chriptmas-memory']
    aliases = tuple(sorted((key, value[2:-1]) for key, value in server.get('env', {}).items()))
    enabled = commands != 'disabled'
    if executor == 'codex':
        config = [('approval_policy', 'never'), ('web_search', 'live'), ('notify', []),
            ('features.shell_tool', enabled), ('features.unified_exec', enabled)]
        config.extend(('features.' + feature, False) for feature in
            ('hooks','plugins','apps','multi_agent','shell_snapshot',
                'skill_mcp_dependency_install','code_mode_host'))
        if 'command' in server:
            mcp = {'command':server['command'], 'args':server['args'],
                'env_vars':[key for key, _ in aliases]}
        else:
            reference = server['headers']['Authorization'][7:]
            mcp = {'url':server['url'], 'bearer_token_env_var':reference[2:-1]}
        config.extend(('mcp_servers.chriptmas-memory.' + key, value) for key, value in mcp.items())
        command = [str(executable), 'exec', '--json', '--ephemeral', '--ignore-user-config',
            '--ignore-rules', '--strict-config', '--skip-git-repo-check', '-C', str(cwd),
            '--sandbox', 'read-only' if preset == 'research' else 'workspace-write']
        for key, value in config:
            command.extend(('-c', key + '=' + _json(value)))
        command.append('-')
    else:
        tools = ['Read','Glob','Grep','WebSearch']
        if preset != 'research':
            tools.extend(('Edit','Write'))
        if enabled:
            tools.append('Bash')
        denied = ['PowerShell','Monitor','NotebookEdit','Agent','Task','WebFetch']
        if not enabled:
            denied.insert(0, 'Bash')
        settings = {'disableAllHooks':True,
            'permissions':{'blockReadsOutsideWorkingDirectories':True}}
        allowed = tools + ['mcp__chriptmas-memory__' + name for name in
            ('projects','recall','methods','read','remember','propose_insight','report_use')]
        # --restricted 保留 managed 设置；host 必须额外拒绝未受控 managed 能力。
        command = [str(executable), '-p', '--restricted', '--no-session-persistence',
            '--no-chrome', '--output-format', 'stream-json',
            '--input-format', 'text', '--verbose', '--include-partial-messages',
            '--permission-mode', 'dontAsk', '--tools', ','.join(tools),
            '--allowedTools', ','.join(allowed), '--disallowedTools', ','.join(denied),
            '--strict-mcp-config', '--mcp-config', encoded.decode('utf-8'),
            '--settings', _json(settings)]
    return LaunchPlan(executor, executor + '@1', cli_version, preset, commands,
        tuple(command), cwd, task, aliases, material_directory, input_policy, stdin_text)
