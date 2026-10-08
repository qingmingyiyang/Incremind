"""启动时发现可信本机 CLI 候选；不执行 CLI，不读取或创建认证正文。"""
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


def _discover(executor, directories, environment, profile, architecture):
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
    authentication = Path(environment[key]) if key in environment else profile / ('.codex' if executor == 'codex' else '.claude')
    _freeze(authentication, stamps)
    for path, identity in stamps.items():
        if _stamp(path, contents=identity[3] is not None) != identity:
            raise ValueError('external_host_discovery_unavailable')
    return ExecutorRegistration(executor, executable, authentication, tuple(stamps.items()))


def discover_local_executors():
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
                registration = _discover(executor, directories, environment, profile, architecture)
                if registration is not None:
                    registrations[executor] = registration
            except (ValueError, TypeError, OSError):
                # 该执行者首个存在候选未知时拒绝，不悄悄降级另一安装或泄漏元数据正文。
                continue
        return registrations
    except (ValueError, TypeError, OSError):
        return {}


def install_local_host(state, *, runtime_root, records, context):
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
        registrations = discover_local_executors()
        if not registrations:
            return None
        host = HostAdmission(deployment=layout, owner_id=context.owner_id, records=records, registrations=registrations)
        state.external_execution_host = host
        return host
    except (AttributeError, TypeError, ValueError, OSError):
        raise ValueError('external_host_bootstrap_invalid') from None
