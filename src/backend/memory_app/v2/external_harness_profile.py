"""发布版唯一可信 profile；不加载 dsh-base、用户插件或本机指令。"""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from core.external_extension_runtime.windows_handle_io import WindowsHandleTreeIo
from .external_workspace import validate_memory_mcp_config, validate_task_path, task_path_identity, verify_task_material


RUNTIME_VERSION = '0.1.5-rc.1'
PACKAGE_VERSION = '0.1.5rc1'
PROFILE_NAME = 'chriptmas-task'


class _EnvironmentExpression(str):
    """仅由封闭server profile生成官方Cordis内存环境引用。"""


class _ServerProfileDumper(yaml.SafeDumper):
    """只为固定server引用注册标签，不修改全局YAML序列化器。"""


_ServerProfileDumper.add_representer(_EnvironmentExpression,
    lambda dumper, value: dumper.represent_scalar('tag:yaml.org,2002:js', str(value)))


def _credential(models):
    row = models.records.read('recognition_model_config', 'generation')
    if row is None or row.payload.get('provider') != 'openai':
        raise ValueError('external_executor_credentials_unavailable')
    parsed = urlsplit(row.payload.get('base_url', ''))
    if (parsed.scheme != 'https' or parsed.hostname != 'api.deepseek.com' or parsed.port not in {None, 443}
            or parsed.path.rstrip('/') not in {'', '/v1'} or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError('external_executor_credentials_unavailable')
    reference = row.payload.get('secret_ref')
    if not isinstance(reference, str) or not reference:
        raise ValueError('external_executor_credentials_unavailable')
    snapshot = models.secrets.get_snapshot(reference)
    if not snapshot.value or '\x00' in snapshot.value:
        raise ValueError('external_executor_credentials_unavailable')
    return row.revision, reference, snapshot


def harness_credential_status(models):
    try:
        _credential(models)
        return {'has_key': True}
    except (ValueError, TypeError):
        return {'has_key': False}


def acquire_harness_credential(models):
    revision, reference, snapshot = _credential(models)
    def current():
        fresh_revision, fresh_reference, fresh = _credential(models)
        if (fresh_revision, fresh_reference, fresh.generation) != (revision, reference, snapshot.generation):
            raise ValueError('external_executor_credentials_changed')
    return snapshot.value, current


def memory_patch(config, *, remote_backend=None):
    server = json.loads(validate_memory_mcp_config(config))['mcpServers']['chriptmas-memory']
    if 'command' not in server:
        raise ValueError('external_executor_memory_invalid')
    environment = {'DEEPSEEK_API_KEY': '', 'OPENAI_API_KEY': '', 'ANTHROPIC_API_KEY': '', 'CODEX_API_KEY': ''}
    # 设备凭据不写明文；server封闭profile另用官方固定!!js引用读取Host内存环境。
    environment.update({key: value for key, value in server.get('env', {}).items()
        if key not in {'CHRIPTMAS_DEVICE_KEY', 'DEVICE_KEY', 'APPROVED_DEVICE_KEY', 'CHRIPTMAS_MCP_TARGET_USER'}})
    if remote_backend is not None:
        from ..mcp import _backend_url
        if _backend_url(remote_backend) != remote_backend or not remote_backend.startswith('https://'):
            raise ValueError('external_executor_memory_invalid')
        environment['CHRIPTMAS_MCP_BACKEND_URL'] = remote_backend
    return [{'insert': [{'id': 'memory-mcp', 'name': '@deepseek-ai/dsh-mcp-client',
        'config': {'serverName': 'chriptmas-memory', 'transport': 'stdio',
            'command': server['command'], 'args': server['args'], 'env': environment,
            'failOnStartupError': True, 'reconnect': {'enabled': False}}}]}]


def profile_files(home, config, *, server=False):
    names = [
        ('sdk-app-startup', '@deepseek-ai/dsh-sdk-app', {'profile': 'sdk-minimal'}),
        ('sdk-jsonrpc-server', '@deepseek-ai/dsh-sdk-jsonrpc-server', {'maxTokensAsSuccess': False}),
        ('deepseek-llm-api-extensions', '@deepseek-ai/dsh-deepseek-llm-api-extensions', None),
        ('llm-deepseek', '@deepseek-ai/dsh-llm-deepseek', {'apiKeyEnv': 'DEEPSEEK_API_KEY',
            'defaultContextWindow': 200000, 'streamIdleTimeoutMs': 120000}),
        ('session-projection', '@deepseek-ai/dsh-session-projection', None),
        ('timer', '@deepseek-ai/cordis-plugin-timer', None),
        ('llm', '@deepseek-ai/dsh-llm', None), ('session', '@deepseek-ai/dsh-session', None),
        ('session-title', '@deepseek-ai/dsh-session-title', {'fallbackMaxWords': 5, 'fallbackMaxBytes': 40, 'maxTitleBytes': 80}),
        ('system-prompt', '@deepseek-ai/dsh-system-prompt', {'includeHarnessIdentity': False,
            'includeRuntimeContext': False, 'personaPrefix': 'Use only the supplied task and authorized memory tools. Return the final work as text.'}),
        ('tools', '@deepseek-ai/dsh-tools', None), ('agent', '@deepseek-ai/dsh-agent', None),
        ('llm-retry', '@deepseek-ai/dsh-llm-retry', None), ('jobs', '@deepseek-ai/dsh-jobs-local', None),
        ('invariants', '@deepseek-ai/dsh-invariants', None),
        ('session-invariant', '@deepseek-ai/dsh-session/invariant', None),
        ('agent-invariant', '@deepseek-ai/dsh-agent/invariant', None),
        ('scope-invariant', '@deepseek-ai/dsh-scope/invariant', None),
        ('agent-loop-invariant', '@deepseek-ai/dsh-agent-loop/invariant', None),
        ('agent-loop', '@deepseek-ai/dsh-agent-loop', {'agents': []}),
        ('sessions', '@deepseek-ai/dsh-session-persistence-jsonl', {'root': str(home / 'sessions'), 'compression': 'none'}),
    ]
    rows = [{'id': identity, 'name': name, **({'config': value} if value is not None else {})}
        for identity, name, value in names]
    if server:
        rows[3]['config']['retryPolicy'] = {'mode': 'normal', 'maxRetries': 0}
    rows[1]['inject'] = ['sdkAppStartup', 'loader']
    rows.extend(memory_patch(config)[0]['insert'])
    if server:
        # 发布版MCP子进程会清除含KEY的父环境；固定!!js引用在内存显式转交设备凭据。
        rows[-1]['config']['env'].update({name: _EnvironmentExpression('process.env.' + name)
            for name in ('CHRIPTMAS_DEVICE_KEY', 'CHRIPTMAS_MCP_TARGET_USER')})
    # 明确采用封闭允许列表：不含 session-log-deepseek/遥测/原生文件或 shell 插件。
    manifest = {'name': PROFILE_NAME, 'private': True, 'dependencies': {},
        'dsh': {'profile': {'bundles': [], 'patchReload': 'startup'}}}
    prefix = 'profiles/' + PROFILE_NAME + '/'
    patch = (yaml.dump([{'insert': rows}], Dumper=_ServerProfileDumper, allow_unicode=True, sort_keys=True)
        if server else json.dumps([{'insert': rows}], ensure_ascii=False, sort_keys=True))
    return {prefix + 'package.json': json.dumps(manifest, sort_keys=True).encode('utf-8'),
        prefix + 'cordis.patch.yml': patch.encode('utf-8'),
        prefix + 'cordis.yml': b'[]\n'}


def install_profile(cwd, config, *, server=False):
    home = cwd / 'harness-home'
    validate_task_path(home)
    files = profile_files(home, config, server=server)
    if os.name == 'nt':
        made = WindowsHandleTreeIo().write_new_tree(cwd, ('harness-home',), files,
            expected_root_identity=task_path_identity(cwd, directory=True))
        if not made:
            raise ValueError('external_executor_profile_exists')
    else:
        handles = []
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            descriptor = os.open(cwd, flags); handles.append(descriptor)
            os.mkdir('harness-home', mode=0o700, dir_fd=descriptor)
            descriptor = os.open('harness-home', flags, dir_fd=descriptor); handles.append(descriptor)
            os.mkdir('profiles', mode=0o700, dir_fd=descriptor)
            descriptor = os.open('profiles', flags, dir_fd=descriptor); handles.append(descriptor)
            os.mkdir(PROFILE_NAME, mode=0o700, dir_fd=descriptor)
            descriptor = os.open(PROFILE_NAME, flags, dir_fd=descriptor); handles.append(descriptor)
            for name, content in files.items():
                target = os.open(Path(name).name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    mode=0o600, dir_fd=descriptor)
                try:
                    remaining = memoryview(content)
                    while remaining:
                        written = os.write(target, remaining)
                        if written <= 0:
                            raise OSError('external_executor_profile_write_failed')
                        remaining = remaining[written:]
                finally:
                    os.close(target)
        finally:
            for descriptor in reversed(handles):
                os.close(descriptor)
    validate_profile(cwd, config, server=server)
    return home


def validate_profile(cwd, config, *, server=False):
    home = cwd / 'harness-home'
    validate_task_path(home)
    # 发布版会读取调用目录的.env；任务材料不允许另行改变模型、密钥或配置入口。
    if os.path.lexists(cwd / '.env'):
        raise ValueError('external_executor_profile_changed')
    # 发布版实际还叠加home patch；仅允许原运行时的无配置状态目录。
    if set(path.name for path in home.iterdir()) - {'profiles', 'sessions', '.anonymous-user-id'}:
        raise ValueError('external_executor_profile_changed')
    if set(path.name for path in (home / 'profiles').iterdir()) != {PROFILE_NAME}:
        raise ValueError('external_executor_profile_changed')
    for name, expected in profile_files(home, config, server=server).items():
        verify_task_material(home / name, expected)
    profile = home / 'profiles' / PROFILE_NAME
    for forbidden in ('node_modules', '.env', 'plugins', 'skills', '.dsh-module-fallback'):
        if os.path.lexists(profile / forbidden):
            raise ValueError('external_executor_profile_changed')
