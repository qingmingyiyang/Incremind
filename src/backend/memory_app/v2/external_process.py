"""受控命令的进程树生命周期、限额双管道和脱敏输出；不提供文件沙箱。"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
import codecs
import ctypes
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time

from backend.shared.secret_detection import REDACTED_SECRET, SECRET_PATTERNS, redact_secrets


@dataclass(frozen=True)
class ProcessResult:
    status: str
    exit_code: int | None
    output_bytes: int
    tail: tuple[str, ...]
    # 内部副作用证据独立于原四个输出结果字段，不改变其等值和展示契约。
    may_have_started: bool = field(default=False, compare=False, repr=False)


class ProcessCleanupError(OSError):
    """内部清理通道只保原 owner 与固定码，不包含启动资料。"""
    def __init__(self, owner):
        super().__init__('external_process_cleanup_incomplete')
        self._cleanup_owner = owner


class _WindowsJob:
    """Job 只负责禁止 breakaway 和关闭时结束全部后代，不限制文件访问。"""

    def __init__(self):
        from ctypes import wintypes as w
        self.tree_exited = False
        self.assigned = False
        self.members = {}
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                ('flags', w.DWORD), ('min_working', ctypes.c_size_t), ('max_working', ctypes.c_size_t),
                ('active', w.DWORD), ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('schedule', w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ('read', 'write', 'other', 'read_bytes', 'write_bytes', 'other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t),
                ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        api.CreateJobObjectW.restype = w.HANDLE
        api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        api.SetInformationJobObject.restype = w.BOOL
        api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        api.AssignProcessToJobObject.restype = w.BOOL
        api.CloseHandle.argtypes = [w.HANDLE]
        api.CloseHandle.restype = w.BOOL
        api.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        api.TerminateJobObject.restype = w.BOOL
        api.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
        api.QueryInformationJobObject.restype = w.BOOL
        api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        api.OpenProcess.restype = w.HANDLE
        api.IsProcessInJob.argtypes = [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)]
        api.IsProcessInJob.restype = w.BOOL
        api.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        api.WaitForSingleObject.restype = w.DWORD
        self.api, self.handle = api, api.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError('external_process_owner_failed')
        limits = Extended()
        # 只启用 KILL_ON_JOB_CLOSE，两个允许 breakaway 的标志均保持关闭。
        limits.basic.flags = 0x2000
        if not api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = OSError('external_process_owner_failed')
            try:
                self.close()
            except OSError:
                error._cleanup_job = self
            raise error from None

    def assign(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise OSError('external_process_owner_failed')
        self.assigned = True

    def close(self):
        failed = False
        if self.handle:
            if not self.api.CloseHandle(self.handle):
                failed = True
            else:
                self.handle = None
                if not self.assigned:
                    self.tree_exited = True
        for pid, (handle, _) in tuple(self.members.items()):
            if self.api.CloseHandle(handle):
                del self.members[pid]
            else:
                failed = True
        if failed:
            raise OSError('external_process_cleanup_incomplete')

    def _capture_members(self, deadline):
        from ctypes import wintypes as w
        def resolve(pid, handle):
            belongs = w.BOOL()
            if not self.api.IsProcessInJob(handle, self.handle, ctypes.byref(belongs)):
                raise OSError('external_process_cleanup_incomplete')
            self.members[pid] = (handle, bool(belongs.value))
            if not belongs.value:
                if not self.api.CloseHandle(handle):
                    raise OSError('external_process_cleanup_incomplete')
                del self.members[pid]
        for pid, (handle, belongs) in tuple(self.members.items()):
            if belongs is not True:
                resolve(pid, handle)
        capacity = 16
        while True:
            class ProcessIds(ctypes.Structure):
                _fields_ = [('assigned', w.DWORD), ('count', w.DWORD), ('ids', ctypes.c_size_t * capacity)]
            ids = ProcessIds()
            success = self.api.QueryInformationJobObject(self.handle, 3, ctypes.byref(ids), ctypes.sizeof(ids), None)
            if success and ids.count == ids.assigned:
                break
            if (not success and ctypes.get_last_error() != 234 or ids.assigned <= capacity
                    or time.monotonic() >= deadline):
                raise OSError('external_process_cleanup_incomplete')
            capacity = max(capacity * 2, ids.assigned)
        for pid in ids.ids[:ids.count]:
            if pid in self.members:
                continue
            if time.monotonic() >= deadline:
                raise OSError('external_process_cleanup_incomplete')
            handle = self.api.OpenProcess(0x00101000, False, pid)
            if not handle:
                if ctypes.get_last_error() == 87:
                    continue
                raise OSError('external_process_cleanup_incomplete')
            # 先接住原 HANDLE 再核 Job 身份，失败关闭仍能在原 Job 上重试。
            self.members[pid] = (handle, None)
            resolve(pid, handle)

    def terminate(self, deadline):
        if not self.handle or self.tree_exited:
            return
        from ctypes import wintypes as w
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in ('user', 'kernel', 'period_user', 'period_kernel')]
            _fields_ += [(name, w.DWORD) for name in ('page_faults', 'total', 'active', 'terminated')]
        self._capture_members(deadline)
        if not self.api.TerminateJobObject(self.handle, 1):
            raise OSError('external_process_cleanup_incomplete')
        while True:
            accounting = Accounting()
            if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                raise OSError('external_process_cleanup_incomplete')
            if accounting.active == 0:
                # Job 活动计数先于原进程对象的退出信号，必须核真实代次。
                for handle, belongs in self.members.values():
                    if belongs and self.api.WaitForSingleObject(handle,
                            max(0, math.ceil((deadline - time.monotonic()) * 1000))) != 0:
                        raise OSError('external_process_cleanup_incomplete')
                self.tree_exited = True
                return
            if time.monotonic() >= deadline:
                raise OSError('external_process_cleanup_incomplete')
            time.sleep(min(.01, max(0, deadline - time.monotonic())))


class _Owner:
    def __init__(self):
        self.lock = threading.Lock()
        self.cleanup_lock = threading.Lock()
        self.job = None
        self.process = None
        self.closed = False
        self.streams, self.threads, self.descriptors = [], [], set()
        self.finished = threading.Event()
        self.process_handle_closed = False
        try:
            self.job = _WindowsJob() if os.name == 'nt' else None
        except OSError as error:
            self.job = getattr(error, '_cleanup_job', None)
            if self.job is not None:
                error._cleanup_owner = self
            raise

    def bind(self, process):
        self.process = process
        if self.job is not None:
            self.job.assign(process)

    def terminate(self, deadline):
        if not self.lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise ProcessCleanupError(self) from None
        try:
            if self.closed:
                return
            failed = False
            if self.job is not None:
                try:
                    self.job.terminate(deadline)
                except OSError:
                    failed = True
            elif self.process is not None:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    failed = True
            # 绑定失败时 bootstrap 尚未开闸，也需明确终止它。
            if self.process is not None and self.process.poll() is None:
                try:
                    self.process.kill()
                except OSError:
                    failed = True
            if self.process is not None:
                try:
                    self.process.wait(timeout=max(0, deadline - time.monotonic()))
                except (OSError, subprocess.TimeoutExpired):
                    failed = True
            if failed:
                raise ProcessCleanupError(self) from None
        finally:
            self.lock.release()

    def close(self, *, deadline=None):
        deadline = time.monotonic() + 1 if deadline is None else deadline
        self.finished.set()
        if not self.cleanup_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise ProcessCleanupError(self) from None
        try:
            if self.closed:
                return
            failed = False
            try:
                self.terminate(deadline)
            except OSError:
                failed = True
            # 先核退出再释放 Job 的进程引用，之后才收流与原 process HANDLE。
            if self.job is not None:
                if not self.job.tree_exited:
                    failed = True
                else:
                    try:
                        self.job.close()
                    except OSError:
                        failed = True
            self.finished.set()
            # 读取/watch 线程只终止进程，不持此清理锁或负责 join 自身。
            for thread in self.threads:
                if thread is threading.current_thread() or thread.ident is None and not thread.is_alive():
                    continue
                try:
                    thread.join(timeout=max(0, deadline - time.monotonic()))
                except RuntimeError:
                    failed = True
            if any(thread.is_alive() for thread in self.threads):
                failed = True
            else:
                for stream in self.streams:
                    if not stream.closed:
                        try:
                            stream.close()
                        except (OSError, ValueError):
                            failed = True
            for descriptor in tuple(self.descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    failed = True
                else:
                    self.descriptors.remove(descriptor)
            if self.process is not None and self.process.poll() is not None and not self.process_handle_closed:
                try:
                    if os.name == 'nt':
                        # 原 Popen Handle.Close 会先置 closed，失败后无法重试原 HANDLE。
                        if not self.job.api.CloseHandle(int(self.process._handle)):
                            raise OSError('external_process_cleanup_incomplete')
                        self.process._handle.closed = True
                    self.process_handle_closed = True
                except OSError:
                    failed = True
            if (failed or self.descriptors or any(not stream.closed for stream in self.streams)
                    or self.job is not None and (self.job.handle is not None or self.job.members)
                    or self.process is not None and not self.process_handle_closed):
                raise ProcessCleanupError(self) from None
            self.closed = True
        finally:
            self.cleanup_lock.release()


class _Lines:
    def __init__(self, secrets, emit):
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.pending = ''
        self.secrets = sorted(set(value for value in secrets if value), key=len, reverse=True)
        self.lookahead = max((len(value) - 1 for value in self.secrets), default=0)
        # 从原检测器取得字段前缀，只用于等待尚未收齐的跨行匹配。
        self.credential_key = re.compile(SECRET_PATTERNS[2].pattern.split(r'\s*[:=]', 1)[0])
        self.pem_end = None
        self.emit = emit

    def _line(self, line):
        if self.pem_end is not None:
            ending = line.find(self.pem_end)
            if ending < 0:
                self.emit(REDACTED_SECRET)
                return
            line = REDACTED_SECRET + line[ending + len(self.pem_end):]
            self.pem_end = None
        header = SECRET_PATTERNS[1].search(line)
        if header is not None:
            closing = header.group().replace('BEGIN ', 'END ', 1)
            if closing not in line[header.end():]:
                self.pem_end = closing
        self.emit(redact_secrets(line.rstrip('\r')))

    def feed(self, data=b'', *, final=False):
        self.pending += self.decoder.decode(data, final=final)
        for secret in self.secrets:
            self.pending = self.pending.replace(secret, REDACTED_SECRET)
        self.pending = SECRET_PATTERNS[2].sub(REDACTED_SECRET, self.pending)
        hold = len(self.pending)
        if not final:
            for match in self.credential_key.finditer(self.pending):
                suffix = self.pending[match.end():].lstrip()
                if suffix and suffix[0] in ':=':
                    suffix = suffix[1:].lstrip()
                    incomplete = not suffix or (suffix[0] in '\"\'' and suffix[0] not in suffix[1:])
                else:
                    incomplete = not suffix
                if incomplete:
                    hold = match.start()
                    break
        # 留足前瞻，避免显式凭据跨行或跨块时先把前半段交给回调。
        while '\n' in self.pending:
            ending = self.pending.index('\n')
            if not final and (ending + 1 > len(self.pending) - self.lookahead or ending + 1 > hold):
                break
            line, self.pending = self.pending[:ending], self.pending[ending + 1:]
            hold -= ending + 1
            self._line(line)
        if final and self.pending:
            self._line(self.pending)
            self.pending = ''


def run_process(command: Sequence[str], *, cwd: Path, environment: Mapping[str, str],
    input_text: str = '', timeout: float = 1200, output_limit: int = 1048576,
    cancel: threading.Event | None = None, on_line: Callable[[str], None] | None = None,
    on_started: Callable[[], None] | None = None,
    secret_values: Sequence[str] = (), on_owner: Callable[[_Owner], None] | None = None) -> ProcessResult:
    """回调仅供有界的本地解析；不记录命令、环境或异常详情。"""
    if cancel is not None and cancel.is_set():
        return ProcessResult('cancelled', None, 0, ())
    if (isinstance(command, (str, bytes)) or not command or any(not isinstance(arg, str) or '\0' in arg for arg in command)
            or not isinstance(input_text, str) or not isinstance(environment, Mapping)
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in environment.items())
            or type(output_limit) is not int or output_limit <= 0 or isinstance(timeout, bool)
            or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0
            or (on_line is not None and not callable(on_line))
            or (on_started is not None and not callable(on_started))
            or (on_owner is not None and not callable(on_owner))
            or any(not isinstance(value, str) for value in secret_values)):
        return ProcessResult('failed', None, 0, ('external_process_start_failed',))
    owner = None
    process = None
    streams, threads = [], []
    read_fd = write_fd = None
    events = queue.Queue(maxsize=32)
    finished = threading.Event()
    state_lock = threading.Lock()
    state = {'bytes': 0, 'reason': None, 'cleanup_deadline': None, 'launch_possible': False}
    tail = deque(maxlen=128)
    control, exit_code, closed = {}, None, set()
    deadline = time.monotonic() + timeout

    def stop(reason):
        with state_lock:
            if state['reason'] is None:
                state['reason'] = reason
            if state['cleanup_deadline'] is None:
                state['cleanup_deadline'] = time.monotonic() + 1
        if owner is not None:
            try:
                owner.terminate(state['cleanup_deadline'])
            except OSError:
                pass

    def enqueue(name, data):
        while not finished.is_set():
            try:
                events.put((name, data), timeout=.05)
                return
            except queue.Full:
                continue

    def read(name, stream):
        try:
            while not finished.is_set():
                data = stream.read(8192)
                if not data:
                    break
                if name != 'control':
                    with state_lock:
                        state['bytes'] += len(data)
                        exceeded = state['bytes'] > output_limit
                    if exceeded:
                        stop('output_limit')
                enqueue(name, data)
        except (OSError, ValueError):
            stop('external_process_failed')
        finally:
            enqueue(name, None)

    def send(payload):
        try:
            remaining = memoryview(payload)
            while remaining:
                written = process.stdin.write(remaining)
                if not written:
                    raise OSError('external_process_failed')
                remaining = remaining[written:]
            # 完整启动包交给原 bootstrap 后，EOF 可开闸；未观察回报不能证明零副作用。
            with state_lock:
                state['launch_possible'] = True
        except (OSError, ValueError):
            if state['reason'] is None and process.poll() is None:
                stop('external_process_failed')
        finally:
            process.stdin.close()

    def watch():
        while not finished.wait(.02):
            if cancel is not None and cancel.is_set():
                stop('cancelled')
                return
            if time.monotonic() >= deadline:
                stop('timed_out')
                return
            if process.poll() is not None:
                # CLI 的父进程已退也不能等待仍持有管道的孙进程。
                with state_lock:
                    if state['cleanup_deadline'] is None:
                        state['cleanup_deadline'] = time.monotonic() + 1
                try:
                    owner.terminate(state['cleanup_deadline'])
                except OSError:
                    pass
                return

    def emit(name, line):
        tail.append(line[-4096:])
        if name == 'stdout' and on_line is not None and state['reason'] != 'external_process_callback_failed':
            try:
                on_line(line)
            except Exception:
                stop('external_process_callback_failed')

    def may_have_started():
        return control.get('started') is True or (state['launch_possible']
            and control.get('error') != 'external_process_start_failed')

    try:
        owner = _Owner()
        owner.finished = finished
        if on_owner is not None:
            on_owner(owner)
        if not owner.cleanup_lock.acquire(timeout=min(1, max(0, deadline - time.monotonic()))):
            raise ProcessCleanupError(owner)
        try:
            # 启动分配、纳入原 owner 和关闭共用原清理锁；关闭后不再分配。
            if owner.closed or owner.finished.is_set():
                raise OSError('external_process_start_failed')
            read_fd, write_fd = os.pipe()
            owner.descriptors.update((read_fd, write_fd))
            options = {}
            if os.name == 'nt':
                import msvcrt
                os.set_inheritable(write_fd, True)
                handle = msvcrt.get_osfhandle(write_fd)
                startup = subprocess.STARTUPINFO()
                startup.lpAttributeList = {'handle_list': [handle]}
                options.update(startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW)
                descriptor = handle
            else:
                options.update(pass_fds=(write_fd,), start_new_session=True)
                descriptor = write_fd
            bootstrap = Path(__file__).with_name('_external_process_bootstrap.py')
            # Windows 虚拟环境 redirector 会先派生 native 子进程，绑定后才开闸不足以约束它。
            executable = sys._base_executable if os.name == 'nt' else sys.executable
            process = subprocess.Popen([executable, '-I', '-S', '-u', str(bootstrap), str(descriptor)],
                cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, close_fds=True, **options)
            owner.process = process
            owner.streams = [stream for stream in (process.stdin, process.stdout, process.stderr) if stream is not None]
            os.close(write_fd)
            owner.descriptors.remove(write_fd)
            write_fd = None
            owner.bind(process)
            control_stream = os.fdopen(read_fd, 'rb', buffering=0)
            owner.descriptors.remove(read_fd)
            owner.streams.append(control_stream)
            read_fd = None
            streams = [process.stdout, process.stderr, control_stream]
            owner.threads = threads
            for name, stream in zip(('stdout', 'stderr', 'control'), streams):
                thread = threading.Thread(target=read, args=(name, stream), name='external-process-' + name)
                threads.append(thread)
                thread.start()
            payload = json.dumps({'command': list(command), 'cwd': str(cwd), 'environment': dict(environment),
                'input_text': input_text}, ensure_ascii=False).encode('utf-8')
            for name, target, args in [('stdin', send, (payload,)), ('watch', watch, ())]:
                thread = threading.Thread(target=target, args=args, name='external-process-' + name)
                threads.append(thread)
                thread.start()
        finally:
            owner.cleanup_lock.release()
        lines = {name: _Lines(secret_values, lambda line, name=name: emit(name, line)) for name in ('stdout', 'stderr')}
        control_bytes = b''
        while len(closed) < 3:
            if finished.is_set():
                stop('external_process_failed')
                break
            if state['cleanup_deadline'] is not None and time.monotonic() >= state['cleanup_deadline']:
                break
            try:
                name, data = events.get(timeout=.05)
            except queue.Empty:
                continue
            if data is None:
                closed.add(name)
                if name in lines:
                    lines[name].feed(final=True)
            elif name in lines:
                lines[name].feed(data)
            else:
                control_bytes += data
                if len(control_bytes) > 512:
                    stop('external_process_failed')
                    control_bytes = b''
                while b'\n' in control_bytes:
                    raw, control_bytes = control_bytes.split(b'\n', 1)
                    packet = json.loads(raw)
                    first_start = packet.get('started') is True and not control.get('started')
                    control.update(packet)
                    # 只有私有管道报告实际 CLI 已创建，才记录开始；stdout 不能伪造此事实。
                    if first_start and on_started is not None:
                        try:
                            on_started()
                        except Exception:
                            stop('external_process_callback_failed')
        process.wait(timeout=max(0, (state['cleanup_deadline'] or deadline) - time.monotonic()))
        exit_code = control.get('exit_code')
        if control.get('error'):
            stop(control['error'])
        elif exit_code is None and state['reason'] is None:
            stop('external_process_failed')
    except Exception as error:
        pending = getattr(error, '_cleanup_owner', None)
        if owner is None and isinstance(pending, _Owner):
            owner = pending
            owner.finished = finished
            if on_owner is not None:
                on_owner(owner)
        stop('external_process_start_failed')
    finally:
        if owner is not None:
            try:
                owner.close(deadline=state['cleanup_deadline'])
            except ProcessCleanupError as error:
                error._cleanup_result = ProcessResult('failed', exit_code, state['bytes'],
                    ('external_process_cleanup_incomplete',), may_have_started())
                raise
        else:
            finished.set()
    reason = state['reason']
    if reason and reason.startswith('external_process_'):
        return ProcessResult('failed', None, state['bytes'], (reason,), may_have_started())
    return ProcessResult(reason or ('completed' if exit_code == 0 else 'failed'),
        exit_code, state['bytes'], tuple(tail), may_have_started())
