"""为已交付的外部上下文创建任务目录，外发资格仍由原交接能力核验。"""
from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlsplit

from backend.shared.secret_detection import contains_secret
from core.external_extension_runtime.windows_handle_io import WindowsHandleIoError, WindowsHandleTreeIo


class ExternalWorkspaceError(ValueError):
    """固定错误码不包含材料正文、凭据和本机路径。"""


_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
_ENV = re.compile(r'\$\{[A-Z][A-Z0-9_]*\}\Z')
_RESERVED = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
             *(f'lpt{i}' for i in range(1, 10))}
_CONTROL = {'context.md', 'task.md', 'memory-mcp.json'}


def _invalid():
    raise ExternalWorkspaceError('external_workspace_invalid')


def _name(value):
    if (not isinstance(value, str) or not _NAME.fullmatch(value)
            or value.endswith(('.', ' ')) or value.split('.')[0].lower() in _RESERVED):
        _invalid()
    return value


def _attachment_name(value):
    # 附件保留中文文件名，只拒绝跨目录、设备名和系统不支持的字符。
    if (not isinstance(value, str) or not 1 <= len(value) <= 128
            or value in {'.', '..'} or value.endswith(('.', ' '))
            or any(ord(character) < 32 or character in '<>:"/\\|?*' for character in value)
            or value.split('.')[0].lower() in _RESERVED):
        _invalid()
    return value


def _check_path(path):
    # Windows junction 与符号链接都不能把任务目录引到另一份用户数据。
    for node in (path, *path.parents):
        try:
            information = node.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(information.st_mode)
                or getattr(information, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            _invalid()


def validate_task_path(path):
    """宿主在解析或创建任务路径前复用同一符号链接和 reparse 检查。"""
    if not isinstance(path, Path) or not path.is_absolute() or '..' in path.parts:
        _invalid()
    _check_path(path)


def _context(value):
    if (not isinstance(value, Mapping) or value.get('version') != 'handoff@1'
            or not isinstance(value.get('text'), str)
            or not isinstance(value.get('entries'), list)
            or not isinstance(value.get('profile'), list)):
        _invalid()
    rows = [*value['entries'], *value['profile']]
    try:
        decoded = json.loads(value['text']) if rows else None
    except (ValueError, TypeError):
        _invalid()
    if (rows and decoded != {'entries': value['entries'], 'profile': value['profile']}) or (
            not rows and value['text'] != ''):
        _invalid()
    if (any(not isinstance(row, dict) or not isinstance(row.get('id'), str)
            or not re.fullmatch(r'[MP][1-9][0-9]*', row['id']) for row in rows)
            or len({row['id'] for row in rows}) != len(rows)):
        _invalid()
    return value['text'].encode('utf-8')


def _mcp(value, memory_endpoint):
    if not isinstance(value, Mapping) or set(value) != {'mcpServers'}:
        _invalid()
    servers = value['mcpServers']
    if not isinstance(servers, Mapping) or set(servers) != {'chriptmas-memory'}:
        _invalid()
    server = servers['chriptmas-memory']
    if not isinstance(server, Mapping):
        _invalid()
    if 'command' in server:
        if (not {'command', 'args'} <= set(server) <= {'command', 'args', 'env'}
                or not isinstance(server['command'], str) or not server['command']
                or '\x00' in server['command']
                or server['args'] != ['-I', '-m', 'backend.memory_app.mcp']):
            _invalid()
        if (not Path(server['command']).is_absolute()
                or Path(server['command']).resolve() != Path(sys.executable).resolve()):
            _invalid()
        environment = server.get('env', {})
        if (not isinstance(environment, Mapping)
                or any(not isinstance(key, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key)
                       or not isinstance(item, str) or not _ENV.fullmatch(item)
                       for key, item in environment.items())):
            _invalid()
    elif 'url' in server:
        if (set(server) != {'type', 'url', 'headers'} or server['type'] != 'http'
                or not isinstance(server['url'], str)):
            _invalid()
        try:
            address = urlsplit(server['url'])
            address.port
        except ValueError:
            _invalid()
        if (address.scheme != 'https' or not address.netloc or address.username
                or address.password or address.query or address.fragment
                or memory_endpoint is None or server['url'] != memory_endpoint):
            _invalid()
        headers = server['headers']
        if (not isinstance(headers, Mapping) or set(headers) != {'Authorization'}
                or not isinstance(headers['Authorization'], str)
                or not headers['Authorization'].startswith('Bearer ')
                or not _ENV.fullmatch(headers['Authorization'][7:])):
            _invalid()
    else:
        _invalid()
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (ValueError, TypeError):
        _invalid()
    if '\x00' in encoded or contains_secret(encoded):
        _invalid()
    return encoded.encode('utf-8')


def validate_memory_mcp_config(value, *, memory_endpoint=None):
    """目录写入和 CLI 适配共用原白名单，返回脱离输入的配置字节。"""
    return _mcp(value, memory_endpoint)


def _write_new_tree(root, turn_id, payloads, *, expected_root_identity=None):
    if os.name == 'nt':
        # 复用原句柄相对写树，路径预检之后更换 junction 也不能转移写入。
        try:
            options = {} if expected_root_identity is None else {'expected_root_identity':expected_root_identity}
            made = WindowsHandleTreeIo().write_new_tree(root, ('agent_workspaces', turn_id), payloads, **options)
        except (WindowsHandleIoError, OSError):
            raise ExternalWorkspaceError('external_workspace_unavailable') from None
        if not made:
            raise ExternalWorkspaceError('external_workspace_exists')
        return
    handles = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = os.open(root, flags)
        handles.append(root_fd)
        if expected_root_identity is not None:
            value = os.fstat(root_fd)
            if [value.st_dev, value.st_ino, value.st_mode] != list(expected_root_identity):
                _invalid()
        try:
            os.mkdir('agent_workspaces', mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        parent_fd = os.open('agent_workspaces', flags, dir_fd=root_fd)
        handles.append(parent_fd)
        os.mkdir(turn_id, mode=0o700, dir_fd=parent_fd)
        task_fd = os.open(turn_id, flags, dir_fd=parent_fd)
        handles.append(task_fd)
        for name, content in payloads.items():
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 mode=0o600, dir_fd=task_fd)
            try:
                remaining = memoryview(content)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if not written:
                        raise OSError('external_workspace_short_write')
                    remaining = remaining[written:]
            finally:
                os.close(descriptor)
    except FileExistsError:
        raise ExternalWorkspaceError('external_workspace_exists') from None
    except OSError:
        raise ExternalWorkspaceError('external_workspace_unavailable') from None
    finally:
        for descriptor in reversed(handles):
            os.close(descriptor)


def task_material_payloads(*, task: str, handoff: Mapping, mcp_config: Mapping,
                          attachments: Mapping[str, bytes] | None = None,
                          memory_endpoint: str | None = None) -> dict[str, bytes]:
    """运行器预检与原安全 writer 共用名称、碰撞和 16 MiB 总量限制。"""
    if not isinstance(task, str) or not task.strip():
        _invalid()
    payloads = {'CONTEXT.md': _context(handoff), 'TASK.md': task.encode('utf-8'),
                'memory-mcp.json': validate_memory_mcp_config(mcp_config, memory_endpoint=memory_endpoint)}
    if attachments is None:
        attachments = {}
    if not isinstance(attachments, Mapping):
        _invalid()
    names = set(_CONTROL)
    for name, content in attachments.items():
        _attachment_name(name)
        if name.lower() in names or not isinstance(content, bytes):
            _invalid()
        names.add(name.lower())
        payloads[name] = content
    if sum(map(len, payloads.values())) > 16 * 1024 * 1024:
        _invalid()
    return payloads


def task_path_identity(path: Path, *, directory=False) -> list[int]:
    """保存实际文件身份；目录不含会随新增任务变化的时间和大小。"""
    validate_task_path(path)
    value = path.stat()
    if directory:
        if not stat.S_ISDIR(value.st_mode):
            _invalid()
        return [value.st_dev, value.st_ino, value.st_mode]
    if not stat.S_ISREG(value.st_mode):
        _invalid()
    return [value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns]


def verify_task_material(path: Path, expected: bytes) -> list[int]:
    """有界读取已知材料，同时核路径和实际打开文件的身份。"""
    identity = task_path_identity(path)
    if identity[3] != len(expected):
        _invalid()
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    try:
        def current_identity():
            value = os.fstat(descriptor)
            return [value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns]
        if current_identity() != identity:
            _invalid()
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            if stream.read(len(expected) + 1) != expected:
                _invalid()
        if current_identity() != identity or task_path_identity(path) != identity:
            _invalid()
        return identity
    finally:
        os.close(descriptor)


def create_task_workspace(user_root: Path, turn_id: str, *, task: str, handoff: Mapping,
                          mcp_config: Mapping, attachments: Mapping[str, bytes] | None = None,
                          memory_endpoint: str | None = None, expected_root_identity=None) -> Path:
    """只写新任务目录，不覆盖旧任务，也不代表已获运行或外发授权。"""
    root = Path(user_root)
    _name(turn_id)
    if not root.is_absolute() or not root.is_dir():
        _invalid()
    destination = root / 'agent_workspaces' / turn_id
    _check_path(destination)
    payloads = task_material_payloads(task=task, handoff=handoff, mcp_config=mcp_config,
        attachments=attachments, memory_endpoint=memory_endpoint)
    _write_new_tree(root, turn_id, payloads, expected_root_identity=expected_root_identity)
    return destination
