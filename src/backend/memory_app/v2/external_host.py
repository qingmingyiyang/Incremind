"""真实宿主输入、版本与内存租约；未知托管配置或 OS 隔离不准入。"""
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from functools import wraps
import os
from pathlib import Path
import re
import sys
import threading
import time
import weakref
from types import MappingProxyType

from backend.shared.deployment import DeploymentLayout
from backend.shared.secret_detection import REDACTED_SECRET, contains_secret, redact_secrets
from core.ai_kernel import validate_turn_request

from .external_adapters import LaunchPlan, build_launch_plan
from .external_process import ProcessCleanupError, _Owner, run_process
from .external_workspace import task_path_identity, validate_task_path
from .external_permissions import HostPermissions, PERMISSIONS


_IDENTITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z')
_ENV_NAME = re.compile(r'[A-Z][A-Z0-9_]{0,127}\Z')
_BASE_ENV = frozenset({'SystemRoot','WINDIR','TEMP','TMP'})
_CREDENTIAL_ENV = frozenset({'OPENAI_API_KEY','CODEX_API_KEY','ANTHROPIC_API_KEY','CHRIPTMAS_DEVICE_KEY',
    'DEVICE_KEY','APPROVED_DEVICE_KEY'})
_RESERVED_ENV = frozenset({'PATH','HOME','USERPROFILE','APPDATA','LOCALAPPDATA',
    'CODEX_HOME','CLAUDE_CONFIG_DIR','PYTHONPATH','PYTHONHOME','NODE_OPTIONS',
    'LD_PRELOAD','LD_LIBRARY_PATH','DYLD_INSERT_LIBRARIES','DYLD_LIBRARY_PATH',
    'COMSPEC','PATHEXT','BASH_ENV','ENV','SHELLOPTS','CLAUDE_CODE_SHELL',
    'CLAUDE_CODE_SHELL_PREFIX','ANTHROPIC_BASE_URL','OPENAI_BASE_URL'})
_PROJECT_CONFIG = ('.codex','.claude','.mcp.json','CLAUDE.md','CLAUDE.local.md')


class ExternalHostError(ValueError):
    """仅返回固定错误码，不含路径、环境、认证或进程输出。"""


def _safe(method):
    @wraps(method)
    def call(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except ExternalHostError:
            raise
        except Exception:
            raise ExternalHostError('external_host_unavailable') from None
    return call


def _reject(code):
    raise ExternalHostError('external_host_' + code)


def _identity(value):
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value) or contains_secret(value):
        _reject('request_invalid')
    return value


def _path(value):
    if (not isinstance(value, Path) or not value.is_absolute() or '..' in value.parts
            or '\x00' in str(value) or contains_secret(str(value))):
        _reject('request_invalid')
    validate_task_path(value)
    return value


def _local_drive_kind(anchor):
    import ctypes
    function = ctypes.WinDLL('kernel32', use_last_error=True).GetDriveTypeW
    function.argtypes, function.restype = [ctypes.c_wchar_p], ctypes.c_uint
    return function(anchor)


def _local_candidate_path(path, *, drive_kind=None):
    """发现候选及其复验先核本机路径，避免隐式读取远程共享元数据。"""
    if (not isinstance(path, Path) or not path.is_absolute() or '..' in path.parts
            or str(path).startswith('\\\\') or '\x00' in str(path)):
        raise ValueError('external_host_discovery_unavailable')
    if os.name == 'nt':
        if (len(path.drive) != 2 or path.drive[0] not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
                or path.drive[1] != ':' or (drive_kind or _local_drive_kind)(path.anchor) not in {2,3,5,6}):
            raise ValueError('external_host_discovery_unavailable')
    return path


def _local_candidate_metadata(path):
    # 原校验从子路径起步；可信发现接点先从盘根逐级核祖先，避免触及既存 junction 后代。
    for node in (*reversed(path.parents), path):
        validate_task_path(node)


def _stamp(path, *, contents=False):
    _path(path)
    value = path.stat()
    return (value.st_dev, value.st_ino, value.st_mode,
        value.st_size if contents else None, value.st_mtime_ns if contents else None)


def _present(path):
    # lexists 也检查断开的链接；不读取文件正文或认证数据。
    return os.path.lexists(path)


def _system_configuration(executor):
    """由运行平台推导已知本机来源，不接受调用方给空目录清单。"""
    if os.name == 'nt':
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        # 读取 OS 已知目录，不使用可能被传入环境覆盖的 ProgramData。
        if ctypes.windll.shell32.SHGetFolderPathW(None, 35, None, 0, buffer) != 0:
            _reject('configuration_unknown')
        if executor == 'codex':
            root = Path(buffer.value) / 'OpenAI' / 'Codex'
            return (root,)
        # 官方 Windows 路径固定为 Program Files；两个注册表视图都拒绝托管来源。
        import winreg
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        r'SOFTWARE\Policies\ClaudeCode', 0, winreg.KEY_READ | view):
                    _reject('configuration_unknown')
            except FileNotFoundError:
                pass
        return (Path('C:/Program Files/ClaudeCode'),)
    if sys.platform == 'darwin':
        # MDM 的有效配置来源不能以文件不存在作证明，等待真实部署 owner。
        _reject('configuration_unknown')
    if sys.platform.startswith('linux'):
        return (Path('/etc/codex') if executor == 'codex' else Path('/etc/claude-code'),)
    _reject('configuration_unknown')


@dataclass(frozen=True)
class ExecutorRegistration:
    """仅由应用装配固定原生 CLI 与认证根；用户请求不能注册入口。"""
    executor: str
    executable: Path = field(repr=False)
    authentication_root: Path = field(repr=False)
    discovery_stamps: tuple = field(default=(), repr=False)
    cli_version: str | None = None
    controlled_api: bool = False


def create_executor_home(cwd):
    """每次任务独占空配置目录，运行时产物永不成为下个任务的配置。"""
    home = cwd / 'executor-home'
    validate_task_path(home)
    if os.name == 'nt':
        from core.external_extension_runtime.windows_handle_io import WindowsHandleTreeIo
        if not WindowsHandleTreeIo().write_new_tree(cwd, ('executor-home',), {},
                expected_root_identity=task_path_identity(cwd, directory=True)):
            _reject('configuration_unknown')
    else:
        descriptor = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.mkdir('executor-home', mode=0o700, dir_fd=descriptor)
        finally:
            os.close(descriptor)
    return home


class HostLease:
    """租约只持有本次内存环境、秘密和复验闭包，不写授权事实。"""
    def __init__(self, host, turn, plan, environment, secrets, validate, *, validate_current=None):
        self.owner_id, self.plan = host.owner_id, plan
        self._host = host
        self.concurrency_limit = host.concurrency_limit
        self._accepted_turn = deepcopy(turn)
        self.isolation = 'linux-bwrap@1' if host.deployment.mode == 'server' else 'desktop-cli-permissions'
        self.linux_launch = host.linux_isolation.launch(plan.cwd) if host.deployment.mode == 'server' else None
        self.harness_model = None
        if host.deployment.mode == 'server':
            model = host.models.records.read('recognition_model_config', 'generation').payload.get('model')
            if (not isinstance(model, str) or contains_secret(model)
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}', model)):
                _reject('credentials_unavailable')
            self.harness_model = model
        self.commands = plan.requested_commands
        self._environment = environment
        self._secrets = secrets
        self._validate = validate
        self._validate_current = validate if validate_current is None else validate_current
        self._closed = False
        self._process_owner = None
        self._lifecycle_lock = threading.RLock()
        self._executing = False
        self._completing = False

    def __repr__(self):
        return 'HostLease(closed=' + repr(self._closed) + ',isolation=' + self.isolation + ')'

    @property
    def accepted_turn(self):
        return deepcopy(self._accepted_turn)

    @property
    def environment(self):
        return MappingProxyType(self._environment)

    @property
    def secret_values(self):
        return self._secrets

    def redact(self, text):
        if not isinstance(text, str):
            _reject('request_invalid')
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED_SECRET)
        return redact_secrets(text)

    @_safe
    def validate(self, reader=None):
        if self._closed:
            _reject('lease_closed')
        self._binding()
        self._validate(reader)

    @_safe
    def _validate_native_current(self):
        """完整复验及产品交付检查之后，只重读同一租约的紧前身份与凭据。"""
        if self._closed:
            _reject('lease_closed')
        self._binding()
        self._validate_current()

    def _binding(self):
        if not isinstance(self._host, HostAdmission):
            _reject('lease_invalid')
        snapshot = self._host._issued_leases.get(self)
        if self._host.deployment.mode == 'server':
            runtime = self._host._linux_leases.get(self)
            if runtime is None or runtime[0] is not self.linux_launch or runtime[1] != self.harness_model:
                _reject('lease_invalid')
        if (snapshot is None or self._host.records is not snapshot[0]
                or self._host.owner_id != snapshot[1] or self.owner_id != snapshot[1]
                or type(self.concurrency_limit) is not int or self.concurrency_limit != snapshot[2]
                or self.plan is not snapshot[3]
                or self._host._validated_limit() != snapshot[2]
                or snapshot[4] is not True or self._process_owner is not snapshot[5]):
            _reject('lease_invalid')
        return snapshot

    @_safe
    def execution_limit(self, *, records, owner_id):
        """内部执行接点借用真实签发身份，不接受其他存储或请求传入的限额。"""
        self.validate()
        snapshot = self._binding()
        if records is not snapshot[0] or owner_id != snapshot[1]:
            _reject('lease_invalid')
        return snapshot[2]

    def _adopt_owner(self, owner):
        with self._lifecycle_lock:
            self._adopt_locked(owner)

    def _adopt_locked(self, owner):
        snapshot = self._host._issued_leases.get(self) if isinstance(self._host, HostAdmission) else None
        if (snapshot is None or self._closed and not self._executing or not isinstance(owner, _Owner)
                or self.plan is not snapshot[3] or self._process_owner is not snapshot[5]):
            _reject('lease_invalid')
        if snapshot[5] is not None and not snapshot[5].closed:
            raise ProcessCleanupError(snapshot[5])
        self._process_owner = owner
        self._host._issued_leases[self] = (*snapshot[:5], owner, snapshot[6])
        if self._closed:
            owner.finished.set()

    def _retain_cleanup(self, owner, complete):
        with self._lifecycle_lock:
            self._retain_locked(owner, complete)

    def _retain_locked(self, owner, complete):
        snapshot = self._host._issued_leases.get(self) if isinstance(self._host, HostAdmission) else None
        if (snapshot is None or snapshot[4] is not True or owner is not snapshot[5]
                or not callable(complete) or snapshot[6] is not None):
            _reject('lease_invalid')
        # 只持原 caller 的领域收口，不在 Host 复制回执或并发槽的写入逻辑。
        self._host._issued_leases[self] = (*snapshot[:6], complete)

    def _begin_execution(self):
        with self._lifecycle_lock:
            self._binding()
            if self._closed or self._executing:
                _reject('lease_invalid')
            self._executing = True

    def _end_execution(self):
        with self._lifecycle_lock:
            self._executing = False
            snapshot = self._host._issued_leases.get(self)
            if (snapshot is not None and self._closed and snapshot[6] is None
                    and snapshot[5] is not None and snapshot[5].closed):
                self._host._issued_leases.pop(self, None)

    def close(self):
        deadline = time.monotonic() + 1
        if not self._lifecycle_lock.acquire(timeout=1):
            _reject('cleanup_incomplete')
        try:
            owner = self._close_identity()
        finally:
            self._lifecycle_lock.release()
        if owner is not None:
            owner.close(deadline=deadline)
        with self._lifecycle_lock:
            snapshot = self._host._issued_leases.get(self) if isinstance(self._host, HostAdmission) else None
            # 原 caller 未结束时保同 map，物理关闭不能抢在原完成回调登记之前。
            if snapshot is None or self._executing:
                return
            if self._completing:
                _reject('cleanup_incomplete')
            complete = snapshot[6]
            self._completing = complete is not None
        if complete is not None:
            try:
                complete()
            except Exception:
                with self._lifecycle_lock:
                    self._completing = False
                _reject('cleanup_incomplete')
        with self._lifecycle_lock:
            self._completing = False
            self._host._issued_leases.pop(self, None)

    def _close_identity(self):
        snapshot = self._host._issued_leases.get(self) if isinstance(self._host, HostAdmission) else None
        # 未签发副本可能共享环境引用，只脱离自身，不能清空真实 owner 的资源。
        if snapshot is None: self._environment = {}
        else: self._environment.clear()
        self._secrets = ()
        self._validate = None
        self._validate_current = None
        self._accepted_turn = None
        self._closed = True
        if snapshot is None:
            self._process_owner = None
            return None
        # 资源从可信签发快照取得；复制租约和可变字段不能夺走原 owner。
        return snapshot[5]

    def __enter__(self):
        self.validate()
        return self

    def __exit__(self, *args):
        self.close()


class HostAdmission:
    """应用固定的宿主机械准入；材料与真实 Turn 身份仍由原运行器核验。"""
    def __init__(self, *, deployment: DeploymentLayout, owner_id, registrations,
            records, environment=None, secret_environment=None, memory_endpoint=None, concurrency_limit=1, models=None,
            linux_isolation=None, server_state=None):
        if type(concurrency_limit) is not int or concurrency_limit < 1:
            _reject('concurrency_invalid')
        self.deployment, self.owner_id = deployment, owner_id
        self.concurrency_limit = self._configured_limit = concurrency_limit
        # 原映射强持待准入/待清理租约；只有快照的 issued=True 才授予执行身份。
        self._issued_leases = {}
        self.registrations = dict(registrations)
        self.records = records
        self._environment = dict(environment or {})
        self._secret_environment = dict(secret_environment or {})
        self.memory_endpoint = memory_endpoint
        self.models = models
        self.linux_isolation, self.server_state = linux_isolation, server_state
        self._server_memory = {}
        self._server_memory_lock = threading.RLock()
        self._bootstrap_owners = []
        self._linux_leases = weakref.WeakKeyDictionary()

    def _adopt_bootstrap_owner(self, owner):
        if not isinstance(owner, _Owner):
            _reject('request_invalid')
        self._bootstrap_owners.append(owner)

    def server_available(self, executor):
        from .external_linux_isolation import LinuxIsolation
        return (executor == 'deepseek-harness' and self.deployment.mode == 'server'
            and self.server_state is not None and self.server_state.external_execution_host is self
            and self.server_state.deployment is self.deployment
            and self.server_state.external_context.owner_id == self.owner_id
            and self.server_state.recognition_records is self.records
            and self.server_state.recognition_models is self.models
            and self.server_state.recognition_runtime_root == self.deployment.user_root
            and isinstance(self.linux_isolation, LinuxIsolation)
            and self.linux_isolation.user_root == self.deployment.user_root and self.linux_isolation.available())

    def bind_server_memory(self, turn_id):
        if self.deployment.mode != 'server':
            return
        from .external_server_identity import current_memory_identity
        binding = current_memory_identity(self.server_state)
        with self._server_memory_lock:
            if turn_id in self._server_memory or len(self._server_memory) >= 64:
                _reject('request_invalid')
            binding._adopted = True
            self._server_memory[turn_id] = binding

    def release_server_memory(self, turn_id):
        with self._server_memory_lock:
            binding = self._server_memory.pop(turn_id, None)
        if binding is not None:
            binding.close()

    def _server_binding(self, turn_id):
        if self.deployment.mode != 'server':
            return None
        with self._server_memory_lock:
            binding = self._server_memory.get(turn_id)
        if binding is None or binding.state is not self.server_state:
            _reject('identity_unavailable')
        binding.validate()
        return binding

    def close(self):
        failed = False
        for owner in tuple(self._bootstrap_owners):
            try:
                owner.close()
            except ProcessCleanupError:
                failed = True
            else:
                self._bootstrap_owners.remove(owner)
        for lease in tuple(self._issued_leases):
            try:
                lease.close()
            except (ProcessCleanupError, ExternalHostError):
                failed = True
        if failed:
            _reject('cleanup_incomplete')
        for identity in tuple(self._server_memory):
            self.release_server_memory(identity)

    def _validated_limit(self):
        if (type(self.concurrency_limit) is not int or self.concurrency_limit < 1
                or self.concurrency_limit != self._configured_limit):
            _reject('concurrency_changed')
        return self.concurrency_limit

    @property
    def permissions(self):
        return HostPermissions(self.records, owner_id=self.owner_id, deployment=self.deployment)

    def material_text_is_safe(self, text):
        """写材料前核已知内存秘密，不向调用方交出环境或秘密值。"""
        if not isinstance(text, str) or redact_secrets(text) != text:
            return False
        with self._server_memory_lock:
            device_keys = tuple(binding.environment()['CHRIPTMAS_DEVICE_KEY'] for binding in self._server_memory.values())
        return all(isinstance(value, str) and bool(value) and value not in text
            for value in (*self._secret_environment.values(), *device_keys))

    def _approved_credentials(self):
        for key, value in self._secret_environment.items():
            if (not isinstance(key, str) or key not in _CREDENTIAL_ENV
                    or not isinstance(value, str) or not value or '\x00' in value):
                _reject('environment_invalid')
        return dict(self._secret_environment)

    def credential_status(self, executor):
        if executor == 'deepseek-harness':
            from .external_harness_profile import harness_credential_status
            return harness_credential_status(self.models) if self.models is not None else {'has_key': False}
        key = {'codex': 'CODEX_API_KEY', 'claude-code': 'ANTHROPIC_API_KEY'}.get(executor)
        try:
            return {'has_key': bool(self._approved_credentials().get(key))}
        except ExternalHostError:
            return {'has_key': False}

    @_safe
    def _memory_environment(self, config=None, *, turn_id=None):
        """MCP 仅消费原批准设备钥匙，不交模型钥匙或继承进程环境。"""
        from .external_memory_admission import _CREDENTIALS
        result = {key:value for key, value in self._approved_credentials().items() if key in _CREDENTIALS}
        if self.deployment.mode == 'server':
            result.update(self._server_binding(turn_id).environment())
        if os.name == 'nt':
            import ctypes
            buffer = ctypes.create_unicode_buffer(32768)
            if not ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer)):
                _reject('environment_invalid')
            result.update({'SystemRoot': buffer.value, 'WINDIR': buffer.value})
        if config is not None:
            from .external_workspace import validate_memory_mcp_config
            import json
            server = json.loads(validate_memory_mcp_config(config))['mcpServers']['chriptmas-memory']
            if 'CHRIPTMAS_MCP_BACKEND_URL' in server.get('env', {}):
                result['CHRIPTMAS_MCP_BACKEND_URL'] = server['env']['CHRIPTMAS_MCP_BACKEND_URL']
        return result

    def _discovered_paths_are_local(self, registration):
        if not registration.discovery_stamps:
            return
        try:
            # 全部驱动类型先于原 _path、_stamp 和认证目录枚举，显式旧装配保持原行为。
            paths = (self.deployment.user_root, *(path for path, _ in registration.discovery_stamps))
            for path in paths:
                _local_candidate_path(path)
            for path in paths:
                _local_candidate_metadata(path)
        except (ValueError, TypeError, OSError):
            _reject('resource_changed')

    def _environment_for(self, registration, plan, *, server_binding=None):
        if any(key not in _BASE_ENV for key in self._environment):
            _reject('environment_invalid')
        result = dict(self._environment)
        for key, value in result.items():
            if not isinstance(value, str) or '\x00' in value or contains_secret(value):
                _reject('environment_invalid')
            if key in {'TEMP','TMP'}:
                path = _path(Path(value))
                if not path.is_dir() or not path.is_relative_to(self.deployment.user_root):
                    _reject('environment_invalid')
        if os.name == 'nt':
            import ctypes
            buffer = ctypes.create_unicode_buffer(32768)
            if not ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer)):
                _reject('environment_invalid')
            for key in ('SystemRoot','WINDIR'):
                if key in result and Path(result[key]) != Path(buffer.value):
                    _reject('environment_invalid')
                result[key] = buffer.value
        if plan.executor == 'deepseek-harness':
            from .external_harness_profile import acquire_harness_credential
            key, _ = acquire_harness_credential(self.models)
            home = plan.cwd / 'harness-home'
            result.update({'HOME':str(home), 'USERPROFILE':str(home), 'DSH_HOME':str(home),
                'DEEPSEEK_API_KEY':key, **dict(plan.public_environment)})
            if server_binding is not None:
                result.update(server_binding.environment())
            return result, (key, *([server_binding.environment()['CHRIPTMAS_DEVICE_KEY']]
                if server_binding is not None else []))
        authentication = plan.cwd / 'executor-home' if registration.controlled_api else registration.authentication_root
        home = authentication if registration.controlled_api else authentication.parent
        result.update({'HOME':str(home), 'USERPROFILE':str(home)})
        result['CODEX_HOME' if plan.executor == 'codex' else 'CLAUDE_CONFIG_DIR'] = str(authentication)
        # 仅显式可信装配传入秘密，绝不继承进程的完整环境。
        approved = self._approved_credentials()
        key = 'CODEX_API_KEY' if plan.executor == 'codex' else 'ANTHROPIC_API_KEY'
        if registration.controlled_api and key not in approved:
            _reject('credentials_unavailable')
        from .external_memory_admission import _CREDENTIALS
        result.update({name: value for name, value in approved.items()
            if not registration.controlled_api or name == key or name in _CREDENTIALS})
        if plan.executor == 'claude-code':
            # 官方>=2.1.251对子进程移除模型秘密；当前适配器最低2.1.257。
            result['CLAUDE_CODE_SUBPROCESS_ENV_SCRUB'] = '1'
        result.update(plan.public_environment)
        for target, source in plan.environment_aliases:
            if (target not in _CREDENTIAL_ENV
                    or source not in self._secret_environment
                    or (target in result and result[target] != result[source])):
                _reject('environment_invalid')
            result[target] = result[source]
        return result, tuple(dict.fromkeys(value for name, value in result.items() if name in _CREDENTIAL_ENV))

    def _permission_check(self, turn, plan, refs, reader):
        needed = {'folder':plan.preset == 'folder', 'commands':plan.requested_commands == 'explicit'}
        if not isinstance(refs, Mapping) or set(refs) != {'folder','commands'}:
            _reject('permission_unavailable')
        for kind, required in needed.items():
            ref = refs[kind]
            if not required:
                if ref is not None:
                    _reject('permission_unavailable')
                continue
            if (not isinstance(ref, Mapping) or set(ref) != {'id','revision'}
                    or not isinstance(ref['id'], str) or not _IDENTITY.fullmatch(ref['id'])
                    or contains_secret(ref['id']) or type(ref['revision']) is not int or ref['revision'] < 1):
                _reject('permission_unavailable')
            row = reader.read(PERMISSIONS, ref['id'])
            expected = {'owner_id':self.owner_id, 'scope':kind, 'path':str(plan.cwd),
                'turn_id':turn['turn_id'] if kind == 'commands' else None}
            # 新生产 writer 固定目录身份；旧底座记录仍保持原精确字段校验。
            if row is not None and 'directory_identity' in row.payload:
                expected['directory_identity'] = task_path_identity(plan.cwd, directory=True)
            if row is None or row.revision != ref['revision'] or row.payload != expected:
                _reject('permission_unavailable')

    def _configuration_paths(self, registration, plan):
        auth = plan.cwd / 'executor-home' if registration.controlled_api else registration.authentication_root
        if not auth.is_dir():
            _reject('configuration_unknown')
        # 已有登录可能加载云端策略或系统凭据。本叶不读内容，也不把本机缺文件当云端证明。
        if any(auth.iterdir()):
            _reject('configuration_unknown')
        absent = list(_system_configuration(plan.executor))
        absent.append(auth.parent / '.codex' / 'managed_config.toml')
        # 已核0.156.1固定ignore flags和doc预算跳过目录来源；旧Host保留原闭包。
        ignored = registration.controlled_api and plan.executor == 'codex' and plan.cli_version == '0.156.1'
        for parent in (plan.cwd, *plan.cwd.parents):
            absent.extend(parent / name for name in (() if ignored else _PROJECT_CONFIG))
        if any(_present(path) for path in absent):
            _reject('configuration_unknown')
        return tuple(absent)

    @_safe
    def prepare(self, accepted_turn, plan, *, mcp_config, host_permission_refs=None):
        """返回原生版本已核的租约；调用方必须在派发前再次 validate 并在 finally close。"""
        limit = self._validated_limit()
        if (not isinstance(self.deployment, DeploymentLayout)
                or self.deployment.mode not in {'desktop','server'}
                or not isinstance(plan, LaunchPlan)):
            _reject('request_invalid')
        try:
            turn = deepcopy(validate_turn_request(deepcopy(accepted_turn)))
        except Exception:
            _reject('request_invalid')
        if (turn['desired_outcome'] != 'project.task'
                or turn.get('execution_policy', {}).get('template_version') != 2
                or turn['privacy']['mode'] != 'remote_allowed' or turn['privacy']['allow_remote'] is not True
                or plan.input_text != turn['input']['text']):
            _reject('request_invalid')
        registration = self.registrations.get(plan.executor)
        if isinstance(registration, ExecutorRegistration):
            self._discovered_paths_are_local(registration)
        root = _path(self.deployment.user_root)
        if not root.is_dir():
            _reject('request_invalid')
        if (not isinstance(registration, ExecutorRegistration) or registration.executor != plan.executor
                or not _path(registration.executable).is_file()
                or not _path(registration.authentication_root).is_dir()):
            _reject('request_invalid')
        for path, expected in registration.discovery_stamps:
            if _stamp(path, contents=expected[3] is not None) != expected:
                _reject('resource_changed')
        cwd = _path(plan.cwd)
        if not cwd.is_dir():
            _reject('request_invalid')
        if plan.preset != 'folder' and cwd != root / 'agent_workspaces' / _identity(turn['turn_id']):
            _reject('request_invalid')
        canonical = build_launch_plan(plan.executor, cli_version=plan.cli_version,
            executable=registration.executable, cwd=cwd, task=turn['input']['text'],
            mcp_config=mcp_config, preset=plan.preset, commands=plan.requested_commands,
            memory_endpoint=self.memory_endpoint, material_directory=plan.material_directory)
        if plan != canonical:
            _reject('request_invalid')
        if plan.material_directory is not None:
            _path(plan.material_directory)
            if plan.material_directory != cwd / 'agent_workspaces' / _identity(turn['turn_id']):
                _reject('request_invalid')
        if plan.executor == 'deepseek-harness':
            from .external_harness_profile import acquire_harness_credential, validate_profile
            _, credential_current = acquire_harness_credential(self.models)
            validate_profile(cwd, mcp_config, server=self.deployment.mode == 'server')
        elif registration.controlled_api:
            key = 'CODEX_API_KEY' if plan.executor == 'codex' else 'ANTHROPIC_API_KEY'
            original_key = self._approved_credentials().get(key)
            def credential_current():
                if not original_key or self._approved_credentials().get(key) != original_key:
                    _reject('credentials_changed')
        else:
            credential_current = None
        server_binding = self._server_binding(turn['turn_id'])
        environment, secrets = self._environment_for(registration, plan,
            **({'server_binding': server_binding} if server_binding is not None else {}))
        # 进程 Job 不限制文件权限；本仓尚无可委托的隔离域，不能由 preset 或平台名代替。
        if (self.deployment.mode == 'server' and not self.server_available(plan.executor) or plan.preset == 'research'
                or plan.requested_commands == 'sandboxed'):
            _reject('isolation_unavailable')
        refs = {'folder':None,'commands':None} if host_permission_refs is None else deepcopy(dict(host_permission_refs))
        self._permission_check(turn, plan, refs, self.records)
        absent = () if plan.executor == 'deepseek-harness' else self._configuration_paths(registration, plan)
        stamps = {path:_stamp(path, contents=path == registration.executable)
            for path in (root, cwd, registration.executable, registration.authentication_root)}
        if registration.controlled_api:
            stamps[cwd / 'executor-home'] = _stamp(cwd / 'executor-home')
        if plan.material_directory is not None:
            stamps[plan.material_directory] = _stamp(plan.material_directory)

        def validate_current():
            if self.deployment.mode == 'server':
                if self._server_binding(turn['turn_id']) is not server_binding:
                    _reject('identity_unavailable')
            if credential_current is not None:
                credential_current()

        def validate(reader=None):
            if self._validated_limit() != limit:
                _reject('concurrency_changed')
            self._discovered_paths_are_local(registration)
            if credential_current is not None:
                credential_current()
            if self.deployment.mode == 'server':
                if not self.server_available(plan.executor) or self._server_binding(turn['turn_id']) is not server_binding:
                    _reject('isolation_unavailable')
            if plan.executor == 'deepseek-harness':
                validate_profile(cwd, mcp_config, server=self.deployment.mode == 'server')
            if (any(_present(path) for path in absent)
                    or plan.executor != 'deepseek-harness' and any((cwd / 'executor-home'
                        if registration.controlled_api else registration.authentication_root).iterdir())):
                _reject('configuration_changed')
            if plan.executor != 'deepseek-harness':
                _system_configuration(plan.executor)
            for path, expected in registration.discovery_stamps:
                if _stamp(path, contents=expected[3] is not None) != expected:
                    _reject('resource_changed')
            for path, expected in stamps.items():
                if _stamp(path, contents=path == registration.executable) != expected:
                    _reject('resource_changed')
            self._permission_check(turn, plan, refs, self.records if reader is None else reader)
            if self.deployment.mode == 'server':
                # 隔离树与材料复核可能较慢；末尾紧前重读设备身份和模型密钥修订。
                validate_current()

        validate()
        lease = HostLease(self, turn, plan, environment, secrets, validate, validate_current=validate_current)
        if self.deployment.mode == 'server':
            self._linux_leases[lease] = (lease.linux_launch, lease.harness_model)
        lease._executing = True
        self._issued_leases[lease] = (self.records, self.owner_id, limit, plan, False, None, None)
        def before_version_native():
            if lease._closed:
                _reject('lease_closed')
            # 版本探测尚未签发执行资格，复用同一复验闭包而不提前认可lease。
            validate()
            if lease._closed:
                _reject('lease_closed')
        try:
            lines = []
            result = run_process((str(registration.executable), '--version'), cwd=cwd,
                environment=environment, timeout=10 if plan.executor == 'deepseek-harness' else 3,
                output_limit=4096, on_line=lines.append,
                secret_values=secrets, on_owner=lease._adopt_owner,
                **({'linux_isolation': lease.linux_launch, 'before_native': before_version_native}
                    if lease.linux_launch is not None else {}))
            expected = ('codex-cli ' + plan.cli_version if plan.executor == 'codex'
                else plan.cli_version if plan.executor == 'deepseek-harness'
                else plan.cli_version + ' (Claude Code)')
            visible = [line.strip() for line in lines if line.strip() and line.strip() != REDACTED_SECRET]
            if result.status != 'completed' or result.exit_code != 0 or visible != [expected]:
                _reject('version_unavailable')
            validate()
            with lease._lifecycle_lock:
                snapshot = self._issued_leases.get(lease)
                if snapshot is None or lease._closed:
                    _reject('lease_closed')
                self._issued_leases[lease] = (*snapshot[:4], True, *snapshot[5:])
        except Exception:
            try:
                lease.close()
            except ProcessCleanupError:
                _reject('cleanup_incomplete')
            raise
        finally:
            lease._end_execution()
        return lease
