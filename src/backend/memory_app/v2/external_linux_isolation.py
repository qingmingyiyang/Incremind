"""固定Linux文件系统隔离与真实PID域资源；不给请求提供launcher或挂载选项。"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import select
import signal
import stat
import sys
import sysconfig
import time
import weakref


RUNTIME_ROOT = Path('/opt/chriptmas/external_executor_runtimes/deepseek-harness-0.1.5rc1')
BWRAP = Path('/usr/bin/bwrap')
_GATE = Path(__file__).with_name('_external_linux_gate.py')
_SNAPSHOT_LIMIT = 150000


def _deny():
    raise ValueError('external_linux_isolation_unavailable')


def trusted_identity(path):
    """共享依赖只接受root维护且当前服务身份无法写入的真实对象。"""
    value = path.lstat()
    if value.st_uid != 0 or (not stat.S_ISLNK(value.st_mode) and value.st_mode & 0o022):
        _deny()
    if not stat.S_ISLNK(value.st_mode) and os.access(path, os.W_OK, effective_ids=True):
        _deny()
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
        value.st_size if not stat.S_ISDIR(value.st_mode) else None,
        value.st_mtime_ns if not stat.S_ISDIR(value.st_mode) else None,
        os.readlink(path) if stat.S_ISLNK(value.st_mode) else None)


def trusted_path(path):
    if not isinstance(path, Path) or not path.is_absolute() or '..' in path.parts:
        _deny()
    for node in (*reversed(path.parents), path):
        identity = trusted_identity(node)
        if stat.S_ISLNK(identity[2]):
            _deny()
    return trusted_identity(path)


def _tree(path):
    trusted_path(path)
    result = {}
    pending = [path]
    while pending:
        node = pending.pop()
        result[str(node)] = identity = trusted_identity(node)
        if len(result) > _SNAPSHOT_LIMIT:
            _deny()
        if stat.S_ISDIR(identity[2]):
            pending.extend(Path(entry.path) for entry in os.scandir(node))
        elif not (stat.S_ISREG(identity[2]) or stat.S_ISLNK(identity[2])):
            _deny()
    return result


def read_trusted_file(path, maximum):
    identity = trusted_path(path)
    if not stat.S_ISREG(identity[2]) or identity[5] > maximum:
        _deny()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid, opened.st_gid,
                opened.st_size, opened.st_mtime_ns, None) != identity:
            _deny()
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum or trusted_path(path) != identity:
            _deny()
        return raw
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class LinuxLaunch:
    policy: object = field(repr=False)
    cwd: Path
    directory_identity: tuple

    def validate(self):
        if not isinstance(self.policy, LinuxIsolation) or self.policy._issued.get(id(self)) is not self:
            _deny()
        self.policy.validate()
        from .external_workspace import task_path_identity
        if tuple(task_path_identity(self.cwd, directory=True)) != self.directory_identity:
            _deny()

    def process_spec(self):
        self.validate()
        return {'version': 'linux-bwrap@1', 'mounts': self.policy.mounts,
            'links': self.policy.links, 'gate': str(_GATE), 'python': str(self.policy.python), 'cwd': str(self.cwd)}


class LinuxIsolation:
    """由可信应用装配建立；可用状态必须包含同策略原生探测的物理关闭事实。"""
    def __init__(self, *, user_root, executable):
        if not sys.platform.startswith('linux') or os.getuid() == 0 or os.geteuid() == 0:
            _deny()
        if executable.parent != RUNTIME_ROOT / 'deepseek_harness_runtime/runtime':
            _deny()
        from .external_workspace import validate_task_path
        validate_task_path(user_root)
        self.user_root, self.executable = user_root, executable
        self.uid, self.gid = os.geteuid(), os.getegid()
        self.python = Path(sys.executable).absolute()
        trusted_path(self.python)
        if self.python.is_symlink() or not os.access(self.python, os.X_OK):
            _deny()
        trusted_path(BWRAP)
        self.bwrap_identity = trusted_identity(BWRAP)
        if not stat.S_ISREG(self.bwrap_identity[2]) or self.bwrap_identity[2] & (stat.S_ISUID | stat.S_ISGID):
            _deny()
        roots = {RUNTIME_ROOT, Path(sys.prefix), Path(sys.base_prefix)}
        if Path('/usr') in roots:
            roots.remove(Path('/usr'))
            roots.update((Path(sysconfig.get_path('stdlib')), Path(sys._base_executable).resolve()))
        roots.update(Path(value) for value in sys.path if value and '/site-packages' in value
            and Path(value).is_absolute() and Path(value).exists())
        architecture = {'x86_64': 'x86_64-linux-gnu', 'aarch64': 'aarch64-linux-gnu'}.get(os.uname().machine)
        if architecture is None:
            _deny()
        library = Path('/usr/lib') / architecture
        if not library.is_dir():
            _deny()
        roots.add(library)
        if Path('/usr/lib64').is_dir():
            roots.add(Path('/usr/lib64').resolve())
        roots.update((_GATE, Path(__file__).with_name('_external_linux_bootstrap.py'),
            Path(__file__).resolve().parent.parent / 'mcp.py',
            Path(__file__).resolve().parents[4] / 'core-contracts/ai/external-context-result.schema.json'))
        mounts, links = [], []
        for target in ('/lib', '/lib64'):
            alias = Path(target)
            if alias.is_symlink():
                if alias.lstat().st_uid != 0 or not os.readlink(alias).startswith('usr/lib'):
                    _deny()
                links.append((os.readlink(alias), target))
            elif alias.exists():
                roots.add(alias)
        for name in ('/etc/resolv.conf', '/etc/hosts', '/etc/nsswitch.conf', '/etc/ld.so.cache',
                '/etc/passwd', '/etc/group', '/etc/ssl/certs/ca-certificates.crt'):
            path = Path(name)
            if path.exists():
                mounts.append((str(path.resolve()), name))
                roots.add(path.resolve())
        # 最小源文件挂载；服务数据根、完整源码和systemd凭据目录不会进入此集合。
        selected = sorted(roots, key=lambda value: (len(value.parts), str(value)))
        selected = [node for index, node in enumerate(selected)
            if not any(node.is_relative_to(parent) for parent in selected[:index])]
        if any(user_root.is_relative_to(node) or node.is_relative_to(user_root) for node in selected):
            _deny()
        self._snapshots = {path: _tree(path) for path in selected}
        mounts.extend((str(path), str(path)) for path in selected
            if str(path) not in {source for source, target in mounts if source == target})
        self.mounts, self.links = tuple(mounts), tuple(links)
        self._mount_binding = (self.mounts, self.links)
        self._issued = weakref.WeakValueDictionary()
        self.verified = False

    def validate(self):
        if os.geteuid() != self.uid or self.uid == 0 or os.getegid() != self.gid:
            _deny()
        if trusted_identity(BWRAP) != self.bwrap_identity:
            _deny()
        if (self.mounts, self.links) != self._mount_binding:
            _deny()
        for path, expected in self._snapshots.items():
            if _tree(path) != expected:
                _deny()

    def launch(self, cwd):
        from .external_workspace import task_path_identity
        if not cwd.is_relative_to(self.user_root) or cwd == self.user_root:
            _deny()
        launch = LinuxLaunch(self, cwd, tuple(task_path_identity(cwd, directory=True)))
        self._issued[id(launch)] = launch
        return launch

    def available(self):
        try:
            self.validate()
            return self.verified
        except (ValueError, OSError):
            return False


def _status(pid):
    with open('/proc/' + str(pid) + '/status', encoding='ascii') as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        _deny()
    return dict(line.split(':', 1) for line in raw.splitlines() if ':' in line)


def _start(pid):
    with open('/proc/' + str(pid) + '/stat', encoding='ascii') as stream:
        raw = stream.read(8193)
    if len(raw) > 8192:
        _deny()
    return int(raw.rsplit(') ', 1)[1].split()[19])


def _exited(descriptor):
    ready = select.poll()
    ready.register(descriptor, select.POLLIN)
    events = ready.poll(0)
    if any(mask & select.POLLNVAL for descriptor, mask in events):
        raise OSError('external_linux_process_identity_changed')
    return bool(events)


class LinuxNamespaceOwner:
    """原PID1与后代均持pidfd；PID重用、setsid或父进程退出不能替换身份。"""
    def __init__(self, launch):
        self.launch = launch
        self.pid = self.pidfd = self.namespace_fd = self.namespace = None
        self.members = {}
        self.retired = []
        self._descriptors = set()
        self.observed_members = set()
        self.tree_exited = False
        self.closed = False
        self.armed = False

    def bind(self, pid, launcher, bootstrap, deadline):
        if self.pid is not None or type(pid) is not int or pid <= 1 or type(launcher) is not int:
            _deny()
        descriptor = os.pidfd_open(pid, 0)
        self._descriptors.add(descriptor)
        self.pid, self.pidfd = pid, descriptor
        while True:
            try:
                status = _status(pid)
                if (int(status['PPid']) != launcher or int(_status(launcher)['PPid']) != bootstrap
                        or [int(value) for value in status['Uid'].split()] != [self.launch.policy.uid] * 4
                        or int(status['NSpid'].split()[-1]) != 1):
                    _deny()
                names = {name: os.stat('/proc/' + str(pid) + '/ns/' + name).st_ino
                    for name in ('pid', 'mnt', 'user', 'ipc', 'uts')}
                if any(value == os.stat('/proc/self/ns/' + name).st_ino for name, value in names.items()):
                    _deny()
                if (int(status['NoNewPrivs']) != 1
                        or any(int(status[name], 16) != 0 for name in ('CapEff', 'CapPrm', 'CapInh', 'CapAmb'))
                        or int(status['Seccomp']) != 2):
                    raise PermissionError
                self.namespace_fd = os.open('/proc/' + str(pid) + '/ns/pid', os.O_RDONLY | os.O_CLOEXEC)
                self._descriptors.add(self.namespace_fd)
                self.namespace = names['pid']
                self.members[pid] = (descriptor, _start(pid))
                self.observed_members.add((pid, self.members[pid][1]))
                self._validate_mounts(pid)
                self._capture()
                return
            except (PermissionError, FileNotFoundError):
                if time.monotonic() >= deadline or _exited(descriptor):
                    _deny()
                time.sleep(.01)

    def _validate_mounts(self, pid):
        prefix = '/proc/' + str(pid) + '/root'
        expected = {target: source for source, target in self.launch.policy.mounts}
        expected[str(self.launch.cwd)] = str(self.launch.cwd)
        for target, source in expected.items():
            actual, original = os.stat(prefix + target), os.stat(source)
            if (actual.st_dev, actual.st_ino) != (original.st_dev, original.st_ino):
                _deny()
        with open('/proc/' + str(pid) + '/mountinfo', encoding='utf-8') as stream:
            lines = stream.read(1048577)
        if len(lines) > 1048576:
            _deny()
        seen = set()
        for line in lines.splitlines():
            parts = line.split()
            target = parts[4]
            for escape, character in (('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\')):
                target = target.replace(escape, character)
            if target in expected:
                if target != str(self.launch.cwd) and 'ro' not in parts[5].split(','):
                    _deny()
                seen.add(target)
            elif target in {'/', '/proc'} or target.startswith('/proc/'):
                if 'ro' not in parts[5].split(','):
                    _deny()
            elif target not in {'/tmp', '/dev'} and not target.startswith('/dev/'):
                _deny()
        if seen != set(expected):
            _deny()

    def _capture(self):
        if self.namespace is None:
            _deny()
        active = []
        count = 0
        for entry in os.scandir('/proc'):
            if not entry.name.isdecimal():
                continue
            count += 1
            if count > 65536:
                _deny()
            pid = int(entry.name)
            try:
                if entry.stat(follow_symlinks=False).st_uid != self.launch.policy.uid:
                    continue
                status = _status(pid)
                if int(status['Uid'].split()[0]) != self.launch.policy.uid:
                    continue
                if os.stat(entry.path + '/ns/pid').st_ino != self.namespace:
                    continue
                generation = _start(pid)
                if pid not in self.members or self.members[pid][1] != generation:
                    descriptor = os.pidfd_open(pid, 0)
                    try:
                        if _start(pid) != generation:
                            os.close(descriptor)
                            continue
                    except Exception:
                        os.close(descriptor)
                        raise
                    if pid in self.members:
                        self.retired.append(self.members[pid][0])
                    self.members[pid] = (descriptor, generation)
                    self._descriptors.add(descriptor)
                    self.observed_members.add((pid, generation))
                if not _exited(self.members[pid][0]):
                    active.append(pid)
                if len(self.members) + len(self.retired) > 4096:
                    _deny()
            except (FileNotFoundError, ProcessLookupError):
                continue
        return active

    def terminate(self, deadline):
        if self.closed or self.tree_exited:
            return
        if not self.armed:
            return
        if self.pidfd is None or self.namespace is None:
            _deny()
        self._capture()
        if not _exited(self.pidfd):
            signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)
        while True:
            active = self._capture()
            if (not active and all(_exited(descriptor) for descriptor, start in self.members.values())
                    and all(_exited(descriptor) for descriptor in self.retired)):
                self.tree_exited = True
                return
            if time.monotonic() >= deadline:
                _deny()
            time.sleep(.005)

    def observe(self):
        if self.armed and self.namespace is not None and not self.closed and not self.tree_exited:
            self._capture()

    def close(self, deadline):
        if self.closed:
            return
        self.terminate(deadline)
        if not self.armed:
            self.tree_exited = True
        failed = False
        for descriptor in tuple(self._descriptors):
            try:
                os.close(descriptor)
            except OSError:
                failed = True
            else:
                self._descriptors.remove(descriptor)
        if failed or self._descriptors:
            _deny()
        self.members.clear()
        self.retired.clear()
        self.pidfd = self.namespace_fd = None
        self.closed = True
