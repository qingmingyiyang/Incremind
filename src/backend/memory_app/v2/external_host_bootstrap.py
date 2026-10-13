"""启动时发现并探测可信本机 CLI 版本；不读取或创建登录文件。"""
from collections.abc import Mapping
import json
import os
from pathlib import Path
import platform

from backend.shared.deployment import DeploymentLayout
from .external_context import ExternalContext
from .external_host import ExecutorRegistration, HostAdmission, _local_candidate_metadata, _local_candidate_path, _local_drive_kind, _stamp


def _startup_environment():
    # 仅消费可信启动位置，不继承密钥、代理、Node 选项或完整进程环境。
    return {key:os.environ[key] for key in ('PATH','CODEX_HOME','CLAUDE_CONFIG_DIR') if key in os.environ}


def _profile_directory():
    if os.name != 'nt':
        raise ValueError('external_host_discovery_unavailable')
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 40, None, 0, buffer) != 0:
        raise ValueError('external_host_discovery_unavailable')
    return Path(buffer.value)


def _architecture():
    value = platform.machine().lower()
    return {'amd64':'x64', 'x86_64':'x64', 'arm64':'arm64', 'aarch64':'arm64'}.get(value)


def _drive_kind(anchor):
    return _local_drive_kind(anchor)


def _checked_path(path):
    _local_candidate_path(path, drive_kind=_drive_kind)
    _local_candidate_metadata(path)
    return path


def _freeze(path, stamps, *, file=False):
    _checked_path(path)
    if not (path.is_file() if file else path.is_dir()):
        raise ValueError('external_host_discovery_unavailable')
    stamps[path] = _stamp(path, contents=file)
    for parent in path.parents:
        _checked_path(parent)
        stamps.setdefault(parent, _stamp(parent))


def _read_metadata_file(path, expected):
    import msvcrt
    from core.external_extension_runtime.windows_handle_io import _Nt, _component, _directory, WindowsHandleIoError
    _checked_path(path)
    if path.name != 'package.json':
        raise ValueError('external_host_discovery_unavailable')
    api, handles, descriptor = _Nt(), [], None
    def identity():
        value = os.fstat(descriptor)
        return value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns
    try:
        handle = api.open_root(Path(path.anchor)); handles.append(handle)
        _directory(api, handle)
        for name in path.parts[1:-1]:
            handle = api.open_dir(handle, _component(name)); handles.append(handle)
            _directory(api, handle)
        handle = api.open_file(handle, _component(path.name)); handles.append(handle)
        if not handle or api.is_reparse(handle) or api.is_dir(handle):
            raise ValueError('external_host_discovery_unavailable')
        # 将自有文件 HANDLE 的关闭责任交给 fd；目录 HANDLE 仍由原 API 逆序关闭。
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        handles.pop()
        # 先核实际打开文件的身份与大小，之后才读取固定单文件，不枚举目录或重开正文。
        if identity() != expected or api.size(handle) > 65536:
            raise ValueError('external_host_discovery_unavailable')
        raw = api.read(handle, maximum=65537)
        if identity() != expected:
            raise ValueError('external_host_discovery_unavailable')
        _checked_path(path)
        if _stamp(path, contents=True) != expected:
            raise ValueError('external_host_discovery_unavailable')
        return raw
    except WindowsHandleIoError:
        raise ValueError('external_host_discovery_unavailable') from None
    finally:
        if descriptor is not None: os.close(descriptor)
        for handle in reversed(handles): api.close(handle)


def _metadata(path, stamps):
    _freeze(path, stamps, file=True)
    raw = _read_metadata_file(path, stamps[path])
    if len(raw) > 65536:
        raise ValueError('external_host_discovery_unavailable')
    value = json.loads(raw)
    if not isinstance(value, Mapping):
        raise ValueError('external_host_discovery_unavailable')
    return value


def _npm_codex(directory, architecture, stamps):
    root = directory / 'node_modules' / '@openai' / 'codex'
    value = _metadata(root / 'package.json', stamps)
    package = '@openai/codex-win32-' + architecture
    if (value.get('name') != '@openai/codex' or value.get('version') != '0.156.1'
            or value.get('bin') != {'codex':'bin/codex.js'}
            or not isinstance(value.get('optionalDependencies'), Mapping)
            or value['optionalDependencies'].get(package) != 'npm:@openai/codex@0.156.1-win32-' + architecture):
        raise ValueError('external_host_discovery_unavailable')
    vendor = root / 'vendor'
    # 对照已核 npm 原解析链，先包内 nested，再同级 hoisted；只认固定名称和版本。
    for parent in (root / 'node_modules', directory / 'node_modules'):
        candidate = parent / '@openai' / ('codex-win32-' + architecture)
        metadata = candidate / 'package.json'
        _checked_path(metadata)
        if os.path.lexists(metadata):
            platform_package = _metadata(metadata, stamps)
            if (platform_package.get('name') != '@openai/codex'
                    or platform_package.get('version') != '0.156.1-win32-' + architecture
                    or platform_package.get('os') != ['win32'] or platform_package.get('cpu') != [architecture]):
                raise ValueError('external_host_discovery_unavailable')
            vendor = candidate / 'vendor'
            break
    target = 'x86_64-pc-windows-msvc' if architecture == 'x64' else 'aarch64-pc-windows-msvc'
    return vendor / target / 'bin' / 'codex.exe'


def _discover(executor, directories, environment, profile, architecture, runtime_root=None):
    name = 'codex' if executor == 'codex' else 'claude'
    stamps = {}
    executable = None
    for directory in directories:
        _checked_path(directory)
        candidate = directory / (name + '.exe')
        _checked_path(candidate)
        if os.path.lexists(candidate):
            executable = candidate
            break
        # wrapper 仅为标准安装定位锚点，其内容永不读取或执行。
        wrappers = tuple(directory / (name + suffix) for suffix in ('.ps1','.cmd','.bat','.js',''))
        if any(os.path.lexists(path) for path in wrappers):
            if executor != 'codex':
                raise ValueError('external_host_discovery_unavailable')
            for wrapper in wrappers:
                if os.path.lexists(wrapper):
                    _freeze(wrapper, stamps, file=True)
            executable = _npm_codex(directory, architecture, stamps)
            break
    if executable is None:
        return None
    _freeze(executable, stamps, file=True)
    key = 'CODEX_HOME' if executor == 'codex' else 'CLAUDE_CONFIG_DIR'
    if runtime_root is not None:
        from core.external_extension_runtime.windows_handle_io import WindowsHandleTreeIo
        from .external_workspace import task_path_identity
        authentication = runtime_root / 'external_executor_auth' / executor
        _checked_path(authentication)
        if not authentication.exists():
            if not WindowsHandleTreeIo().write_new_tree(runtime_root, ('external_executor_auth', executor), {},
                    expected_root_identity=task_path_identity(runtime_root, directory=True)):
                raise ValueError('external_host_discovery_unavailable')
        if any(authentication.iterdir()):
            raise ValueError('external_host_discovery_unavailable')
    else:
        authentication = Path(environment[key]) if key in environment else profile / ('.codex' if executor == 'codex' else '.claude')
    _freeze(authentication, stamps)
    for path, identity in stamps.items():
        if _stamp(path, contents=identity[3] is not None) != identity:
            raise ValueError('external_host_discovery_unavailable')
    return ExecutorRegistration(executor, executable, authentication, tuple(stamps.items()),
        _probe_version(executor, executable, authentication, controlled_api=runtime_root is not None),
        controlled_api=runtime_root is not None)


def discover_local_executors(*, runtime_root=None):
    """返回元数据固定的 Windows 候选，不把候选身份当登录、隔离或运行许可。"""
    try:
        if os.name != 'nt':
            return {}
        environment, profile, architecture = _startup_environment(), _profile_directory(), _architecture()
        if not isinstance(environment, Mapping) or architecture not in {'x64','arm64'}:
            return {}
        _checked_path(profile)
        raw = environment.get('PATH', '')
        if not isinstance(raw, str):
            return {}
        directories = [Path(value) for value in raw.split(';')] if raw else []
        for directory in directories:
            _checked_path(directory)
        directories.append(profile / '.local' / 'bin')
        registrations = {}
        for executor in ('codex', 'claude-code'):
            try:
                registration = _discover(executor, directories, environment, profile, architecture, runtime_root)
                if registration is not None:
                    registrations[executor] = registration
            except (ValueError, TypeError, OSError):
                # 该执行者首个存在候选未知时拒绝，不悄悄降级另一安装或泄漏元数据正文。
                continue
        return registrations
    except (ValueError, TypeError, OSError):
        return {}


def install_local_host(state, *, runtime_root, records, context, models=None):
    """只在可信部署的原 routes 装配阶段固定 Host，首次 Kernel 以后不重新发现。"""
    layout = getattr(state, 'deployment', None)
    if layout is None:
        return None
    try:
        if (not isinstance(layout, DeploymentLayout) or layout.mode not in {'desktop','server'}
                or not isinstance(runtime_root, Path) or layout.user_root != runtime_root
                or not isinstance(context, ExternalContext) or context.records is not records
                or context.owner_id != 'local-user' or getattr(state, 'external_context', None) is not context):
            raise ValueError
        _checked_path(runtime_root)
        _checked_path(records.database_path)
        if not records.database_path.is_relative_to(runtime_root):
            raise ValueError
        existing = getattr(state, 'external_execution_host', None)
        if existing is not None:
            if (not isinstance(existing, HostAdmission) or existing.deployment != layout
                    or existing.records is not records or existing.owner_id != context.owner_id):
                raise ValueError
            return existing
        if layout.mode == 'server':
            return None
        if layout.server_root is not None or getattr(state, 'ai_runtime', None) is not None:
            raise ValueError
        registrations = discover_local_executors(runtime_root=runtime_root)
        harness = _discover_harness(runtime_root)
        if harness is not None:
            registrations['deepseek-harness'] = harness
        if not registrations:
            return None
        # 仅可信启动装配消费官方非交互API key，不继承其它模型密钥或完整环境。
        credentials = {key: os.environ[key] for key in ('CODEX_API_KEY', 'ANTHROPIC_API_KEY')
            if os.environ.get(key)}
        host = HostAdmission(deployment=layout, owner_id=context.owner_id, records=records,
            registrations=registrations, models=models, secret_environment=credentials)
        state.external_execution_host = host
        return host
    except (AttributeError, TypeError, ValueError, OSError):
        raise ValueError('external_host_bootstrap_invalid') from None


def _probe_version(executor, executable, directory, *, controlled_api=False):
    """只在无用户配置的候选中探测版本；未知身份保留不可执行状态。"""
    from .external_process import run_process
    from .external_host import _system_configuration, _PROJECT_CONFIG, _present
    import re
    if executor != 'deepseek-harness':
        if any(directory.iterdir()) or any(_present(path) for path in _system_configuration(executor)):
            return None
        # 固定Codex版本探测不消费项目配置；执行另由canonical flags封闭。
        names = () if controlled_api and executor == 'codex' else _PROJECT_CONFIG
        if any(_present(parent / name) for parent in (directory, *directory.parents) for name in names
                if parent / name != directory):
            return None
    environment = {}
    if os.name == 'nt':
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer)):
            return None
        environment = {'SystemRoot': buffer.value, 'WINDIR': buffer.value}
    lines = []
    result = run_process((str(executable), '--version'), cwd=directory, environment=environment,
        timeout=10 if executor == 'deepseek-harness' else 3, output_limit=4096, on_line=lines.append)
    if result.status != 'completed' or result.exit_code != 0 or len(lines) != 1:
        return None
    expression = {'codex':r'codex-cli (0\.156\.1)', 'claude-code':r'(2\.1\.[0-9]+) \(Claude Code\)',
        'deepseek-harness':r'(0\.1\.5-rc\.1)'}[executor]
    found = re.fullmatch(expression, lines[0].strip())
    if found is None or executor == 'claude-code' and int(found[1].split('.')[-1]) < 257:
        return None
    return found[1]


def _discover_harness(runtime_root):
    """独立可选wheel安装目录，不导入其中Python代码或SDK，不自动安装。"""
    try:
        from .external_harness_profile import PACKAGE_VERSION
        root = runtime_root / 'external_executor_runtimes' / ('deepseek-harness-' + PACKAGE_VERSION)
        package = root / 'deepseek_harness_runtime'
        architecture = _architecture()
        if os.name != 'nt' or architecture not in {'x64', 'arm64'}:
            return None
        metadata = root / ('deepseek_harness_runtime_bin-' + PACKAGE_VERSION + '.dist-info') / 'METADATA'
        stamps = {}
        _freeze(metadata, stamps, file=True)
        if metadata.stat().st_size > 131072:
            return None
        # 元数据只核发行名与版本，不执行wheel中的入口脚本。
        fields = metadata.read_text(encoding='utf-8').split('\n\n', 1)[0].splitlines()
        if fields.count('Name: deepseek-harness-runtime-bin') != 1 or fields.count('Version: ' + PACKAGE_VERSION) != 1:
            return None
        executable = package / 'runtime' / ('deepseek-harness-sdk-runtime-win-' + architecture + '.exe')
        _freeze(executable, stamps, file=True)
        _freeze(executable.with_name(executable.stem + '-rg.exe'), stamps, file=True)
        version = _probe_version('deepseek-harness', executable, root)
        if any(_stamp(path, contents=value[3] is not None) != value for path, value in stamps.items()):
            return None
        return ExecutorRegistration('deepseek-harness', executable, root, tuple(stamps.items()), version)
    except (ValueError, TypeError, OSError):
        return None
