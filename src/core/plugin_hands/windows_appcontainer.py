"""Fail-closed Windows AppContainer launcher for the disabled Plugin Hands host.

This module deliberately has no connection to Plugin package activation.  It is
an OS-boundary primitive: callers must create a lease workspace and its ACLs
before asking it to start a managed artifact.  It never falls back to the
ordinary user token when an AppContainer operation fails.
"""
from __future__ import annotations

import ctypes
import io
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final


_S_OK: Final = 0
_CREATE_SUSPENDED: Final = 0x00000004
_CREATE_UNICODE_ENVIRONMENT: Final = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT: Final = 0x00080000
# ProcThreadAttributeValue(9, FALSE, TRUE, FALSE).  The ``9`` is important:
# 0x2000B addresses a different process-thread attribute and must fail closed.
_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES: Final = 0x00020009
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST: Final = 0x00020002
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: Final = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x00002000
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS: Final = 0x00000008
_JOB_OBJECT_LIMIT_PROCESS_MEMORY: Final = 0x00000100
_JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION: Final = 15
_JOB_OBJECT_CPU_RATE_CONTROL_ENABLE: Final = 0x00000001
_JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP: Final = 0x00000004
_INVALID_DWORD: Final = 0xFFFFFFFF
_STARTF_USESTDHANDLES: Final = 0x00000100
_HANDLE_FLAG_INHERIT: Final = 0x00000001
_TOKEN_QUERY: Final = 0x0008
_TOKEN_IS_APP_CONTAINER: Final = 29


class AppContainerError(RuntimeError):
    """A containment operation failed; callers must treat the launch as none."""


class AppContainerUnavailable(AppContainerError):
    """The required Windows APIs cannot establish an AppContainer boundary."""


@dataclass(frozen=True)
class AppContainerCapability:
    """A deliberately narrow capability probe result, not a sandbox attestation."""

    available: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AppContainerResourceLimits:
    """OS-owned hard limits applied before a suspended worker is resumed."""

    process_memory_bytes: int
    cpu_rate: int

    def __post_init__(self) -> None:
        if not isinstance(self.process_memory_bytes, int) or isinstance(self.process_memory_bytes, bool) or self.process_memory_bytes < 64 * 1024 * 1024:
            raise ValueError("AppContainer process memory limit is invalid")
        if not isinstance(self.cpu_rate, int) or isinstance(self.cpu_rate, bool) or not 1 <= self.cpu_rate <= 10_000:
            raise ValueError("AppContainer CPU rate limit is invalid")


@dataclass(frozen=True)
class AppContainerLaunchSpec:
    """Host-derived launch data; do not construct this from Plugin package data."""

    executable: Path
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.executable.is_absolute() or not self.cwd.is_absolute():
            raise ValueError("AppContainer launch paths must be absolute")
        if not self.executable.is_file() or not self.cwd.is_dir():
            raise ValueError("AppContainer launch target or workspace is missing")
        if any("\x00" in value for value in (*self.argv, *self.environment.keys(), *self.environment.values())):
            raise ValueError("AppContainer launch data must not contain NUL")


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_ulong)]


class _SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.POINTER(_SID_AND_ATTRIBUTES)),
        ("CapabilityCount", ctypes.c_ulong),
        ("Reserved", ctypes.c_ulong),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_ulong),
        ("dwY", ctypes.c_ulong),
        ("dwXSize", ctypes.c_ulong),
        ("dwYSize", ctypes.c_ulong),
        ("dwXCountChars", ctypes.c_ulong),
        ("dwYCountChars", ctypes.c_ulong),
        ("dwFillAttribute", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("wShowWindow", ctypes.c_ushort),
        ("cbReserved2", ctypes.c_ushort),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_ulong),
        ("dwThreadId", ctypes.c_ulong),
    ]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", ctypes.c_ulong), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", ctypes.c_int)]


@dataclass
class _AttributeList:
    pointer: int
    buffer: ctypes.Array[ctypes.c_char]
    capabilities: _SECURITY_CAPABILITIES
    child_handles: ctypes.Array[ctypes.c_void_p]


@dataclass
class _AnonymousStdio:
    """Raw pipe ends until ownership moves to the child or a parent file object."""

    child_stdin: int
    parent_stdin: int
    parent_stdout: int
    child_stdout: int
    parent_stderr: int
    child_stderr: int
    _parent_descriptors: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _parent_streams: dict[str, io.IOBase] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def create(cls) -> "_AnonymousStdio":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure_kernel32(kernel32)
        security = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), None, True)
        # 创建期间同样保留原管道身份，不能等六个端点全成功才建立所有者。
        stdio = cls(0, 0, 0, 0, 0, 0)
        try:
            for label, read_name, write_name in (("stdin", "child_stdin", "parent_stdin"), ("stdout", "parent_stdout", "child_stdout"), ("stderr", "parent_stderr", "child_stderr")):
                read, write = ctypes.c_void_p(), ctypes.c_void_p()
                created = kernel32.CreatePipe(ctypes.byref(read), ctypes.byref(write), ctypes.byref(security), 0)
                error = ctypes.get_last_error()
                setattr(stdio, read_name, int(read.value or 0))
                setattr(stdio, write_name, int(write.value or 0))
                if not created:
                    raise _last_error("CreatePipe(" + label + ")", error)
            # 只有子端可继承，后面的显式 handle-list 继续限制继承范围。
            for name in ("parent_stdin", "parent_stdout", "parent_stderr"):
                if not kernel32.SetHandleInformation(ctypes.c_void_p(getattr(stdio, name)), _HANDLE_FLAG_INHERIT, 0):
                    raise _last_error("SetHandleInformation(" + name + ")")
            return stdio
        except BaseException as error:
            errors: list[tuple[str, BaseException]] = []
            _cleanup_attempt(errors, "partial pipes", stdio.close)
            if stdio._has_cleanup_resources():
                error._cleanup_stdio = stdio
            _add_cleanup_notes(error, errors)
            raise

    def child_handle_list(self) -> tuple[int, int, int]:
        return self.child_stdin, self.child_stdout, self.child_stderr

    def close_child(self) -> None:
        self._close_raw(("child_stdin", "child_stdout", "child_stderr"))

    def close_parent(self) -> None:
        errors: list[tuple[str, BaseException]] = []
        for name in ("parent_stdin", "parent_stdout", "parent_stderr"):
            _cleanup_attempt(errors, name + " stream", lambda name=name: self._close_parent_stream(name))
            _cleanup_attempt(errors, name + " descriptor", lambda name=name: self._close_parent_descriptor(name))
            _cleanup_attempt(errors, name, lambda name=name: _close_owned_handle(self, name))
        _raise_cleanup_failures(errors)

    def close(self) -> None:
        errors: list[tuple[str, BaseException]] = []
        _cleanup_attempt(errors, "child pipes", self.close_child)
        _cleanup_attempt(errors, "parent pipes", self.close_parent)
        _raise_cleanup_failures(errors)

    def parent_files(self) -> tuple[io.BufferedWriter, io.BufferedReader, io.BufferedReader]:
        for name, mode in (("parent_stdin", "wb"), ("parent_stdout", "rb"), ("parent_stderr", "rb")):
            descriptor = _descriptor_from_handle(getattr(self, name), write=mode == "wb")
            # 原生句柄先交给 CRT，再交给 FileIO；每一步成功后才清空上一代。
            self._parent_descriptors[name] = descriptor
            setattr(self, name, 0)
            stream = os.fdopen(descriptor, mode, buffering=0)
            self._parent_streams[name] = stream
            del self._parent_descriptors[name]
        return tuple(self._parent_streams[name] for name in ("parent_stdin", "parent_stdout", "parent_stderr"))

    def release_parent_files(self) -> None:
        # 调用方已把三条原流交给同一个进程所有者，此处仅移交引用。
        self._parent_streams.clear()

    def _close_parent_stream(self, name: str) -> None:
        stream = self._parent_streams.get(name)
        if stream is not None:
            try:
                stream.close()
            finally:
                if stream.closed:
                    del self._parent_streams[name]

    def _close_parent_descriptor(self, name: str) -> None:
        descriptor = self._parent_descriptors.get(name)
        if descriptor is not None:
            os.close(descriptor)
            del self._parent_descriptors[name]

    def _close_raw(self, names: tuple[str, ...]) -> None:
        errors: list[tuple[str, BaseException]] = []
        for name in names:
            _cleanup_attempt(errors, name, lambda name=name: _close_owned_handle(self, name))
        _raise_cleanup_failures(errors)

    def _has_cleanup_resources(self) -> bool:
        return bool(self._parent_descriptors or self._parent_streams or any(getattr(self, name) for name in ("child_stdin", "parent_stdin", "parent_stdout", "child_stdout", "parent_stderr", "child_stderr")))


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    _fields_ = [("ControlFlags", ctypes.c_ulong), ("CpuRate", ctypes.c_ulong)]


def probe_appcontainer() -> AppContainerCapability:
    """Report whether the native APIs needed for a fail-closed launch are present."""

    if os.name != "nt":
        return AppContainerCapability(False, "windows-required")
    try:
        userenv = ctypes.WinDLL("userenv", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        for library, names in ((userenv, ("CreateAppContainerProfile", "DeleteAppContainerProfile")), (kernel32, ("InitializeProcThreadAttributeList", "UpdateProcThreadAttribute", "CreateProcessW", "AssignProcessToJobObject", "ResumeThread"))):
            for name in names:
                getattr(library, name)
    except (AttributeError, OSError) as error:
        return AppContainerCapability(False, f"native-api-unavailable:{type(error).__name__}")
    return AppContainerCapability(True)


class AppContainerProfile:
    """An ephemeral zero-capability AppContainer identity."""

    def __init__(self, name: str, sid: int) -> None:
        self.name = name
        self._sid = sid
        self._deleted = False

    @property
    def sid(self) -> int:
        if not self._sid:
            raise AppContainerError("AppContainer SID has been released")
        return self._sid

    @classmethod
    def create(cls, *, prefix: str = "ChriptmasOS.PluginHands") -> "AppContainerProfile":
        _require_windows()
        name = f"{prefix}.{uuid.uuid4().hex}"
        if len(name) > 64:
            raise ValueError("AppContainer profile name exceeds the Windows 64-character limit")
        userenv = ctypes.WinDLL("userenv", use_last_error=True)
        userenv.CreateAppContainerProfile.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
        userenv.CreateAppContainerProfile.restype = ctypes.c_long
        sid = ctypes.c_void_p()
        result = int(userenv.CreateAppContainerProfile(name, name, "Chriptmas OS Plugin Hands ephemeral worker", None, 0, ctypes.byref(sid)))
        if result != _S_OK or not sid.value:
            raise AppContainerError(f"CreateAppContainerProfile failed: 0x{result & _INVALID_DWORD:08x}")
        return cls(name, int(sid.value))

    def close(self) -> None:
        if self._deleted:
            return
        _require_windows()
        result = int(ctypes.WinDLL("userenv", use_last_error=True).DeleteAppContainerProfile(self.name))
        if result != _S_OK:
            # Keep the SID and profile name so the caller can terminate child
            # handles and retry cleanup; losing either would orphan a profile.
            raise AppContainerError(f"DeleteAppContainerProfile failed: 0x{result & _INVALID_DWORD:08x}")
        if self._sid:
            ctypes.WinDLL("kernel32", use_last_error=True).LocalFree(ctypes.c_void_p(self._sid))
            self._sid = 0
        self._deleted = True

    def __enter__(self) -> "AppContainerProfile":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class _NoBreakawayJob:
    """A dedicated kill-on-close, one-process Job.  It is not a sandbox itself."""

    def __init__(self, handle: int) -> None:
        self._handle = handle

    @classmethod
    def create(cls, resource_limits: AppContainerResourceLimits) -> "_NoBreakawayJob":
        _require_windows()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure_kernel32(kernel32)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise _last_error("CreateJobObjectW")
        # Job 创建后立即由原包装器持有，配置失败也不能丢失未关闭的句柄。
        job = cls(int(handle))
        try:
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            if not isinstance(resource_limits, AppContainerResourceLimits):
                raise ValueError("AppContainer resource limits are required")
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS | _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info.BasicLimitInformation.ActiveProcessLimit = 1
            info.ProcessMemoryLimit = resource_limits.process_memory_bytes
            if not kernel32.SetInformationJobObject(ctypes.c_void_p(handle), _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)):
                raise _last_error("SetInformationJobObject")
            cpu = _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(
                _JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | _JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP,
                resource_limits.cpu_rate,
            )
            if not kernel32.SetInformationJobObject(ctypes.c_void_p(handle), _JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION, ctypes.byref(cpu), ctypes.sizeof(cpu)):
                raise _last_error("SetInformationJobObject(CPU rate)")
            return job
        except BaseException as error:
            errors: list[tuple[str, BaseException]] = []
            _cleanup_attempt(errors, "unconfigured Job", job.close)
            if job._handle:
                error._cleanup_job = job
            _add_cleanup_notes(error, errors)
            raise

    def assign(self, process_handle: int) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure_kernel32(kernel32)
        if not kernel32.AssignProcessToJobObject(ctypes.c_void_p(self._handle), ctypes.c_void_p(process_handle)):
            raise _last_error("AssignProcessToJobObject")

    def resource_limits(self) -> AppContainerResourceLimits:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure_kernel32(kernel32)
        extended = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if not kernel32.QueryInformationJobObject(ctypes.c_void_p(self._handle), _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(extended), ctypes.sizeof(extended), None):
            raise _last_error("QueryInformationJobObject")
        cpu = _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        if not kernel32.QueryInformationJobObject(ctypes.c_void_p(self._handle), _JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION, ctypes.byref(cpu), ctypes.sizeof(cpu), None):
            raise _last_error("QueryInformationJobObject(CPU rate)")
        required = _JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | _JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
        if extended.BasicLimitInformation.LimitFlags & _JOB_OBJECT_LIMIT_PROCESS_MEMORY == 0 or cpu.ControlFlags & required != required:
            raise AppContainerError("AppContainer resource limits are unavailable")
        return AppContainerResourceLimits(int(extended.ProcessMemoryLimit), int(cpu.CpuRate))

    def close(self) -> None:
        _close_owned_handle(self, "_handle")


class AppContainerProcess:
    """Owns process, Job and parent-only stdio handles for one constrained child."""

    def __init__(self, process_handle: int, thread_handle: int, process_id: int, job: _NoBreakawayJob | None, stdin: io.BufferedWriter | None, stdout: io.BufferedReader | None, stderr: io.BufferedReader | None, *, stderr_reader: str = "internal") -> None:
        _validate_stderr_reader(stderr_reader)
        self.process_handle = process_handle
        self.thread_handle = thread_handle
        self.process_id = process_id
        self._job = job
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self._stdio: _AnonymousStdio | None = None
        self._stderr_drain: threading.Thread | None = None
        self._unconfirmed_launch = False
        self._activate_stderr_reader(stderr_reader)

    def _activate_stderr_reader(self, stderr_reader: str) -> None:
        # 读取归属只能二选一；宿主模式由调用方读取并收口自己的 reader。
        if stderr_reader == "internal":
            self._stderr_drain = threading.Thread(target=self._drain_stderr, daemon=True)
            self._stderr_drain.start()

    def _drain_stderr(self) -> None:
        try:
            while self.stderr is not None and self.stderr.read(8192):
                pass
        except (OSError, ValueError):
            pass

    def wait(self, timeout: float | None = None) -> int | None:
        milliseconds = _INVALID_DWORD if timeout is None else max(0, min(_INVALID_DWORD - 1, int(timeout * 1000)))
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure_kernel32(kernel32)
        result = kernel32.WaitForSingleObject(ctypes.c_void_p(self.process_handle), milliseconds)
        if result == 0x00000102:
            return None
        if result != 0:
            raise _last_error("WaitForSingleObject")
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(ctypes.c_void_p(self.process_handle), ctypes.byref(exit_code)):
            raise _last_error("GetExitCodeProcess")
        return int(exit_code.value)

    def is_appcontainer(self) -> bool:
        """Read the kernel token fact for the launched worker."""

        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(ctypes.c_void_p(self.process_handle), _TOKEN_QUERY, ctypes.byref(token)):
            raise _last_error("OpenProcessToken")
        try:
            value = wintypes.DWORD()
            returned = wintypes.DWORD()
            if not advapi32.GetTokenInformation(token, _TOKEN_IS_APP_CONTAINER, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(returned)):
                raise _last_error("GetTokenInformation(TokenIsAppContainer)")
            return bool(value.value)
        finally:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            _configure_kernel32(kernel32)
            kernel32.CloseHandle(token)

    def resource_limits(self) -> AppContainerResourceLimits:
        if self._job is None:
            raise AppContainerError("AppContainer Job is unavailable")
        return self._job.resource_limits()

    def close(self) -> None:
        errors: list[tuple[str, BaseException]] = []

        def close_stream(stream_name: str) -> None:
            stream = getattr(self, stream_name, None)
            if stream is not None:
                try:
                    stream.close()
                finally:
                    # 关闭前失败时仍保留真实流，让重复 close 可以重试。
                    if stream.closed:
                        setattr(self, stream_name, None)

        # 单个 EOF/管道错误不能阻止 Job 先杀进程，再解除双流的阻塞读取。
        _cleanup_attempt(errors, "stdin", lambda: close_stream("stdin"))
        if self._stdio is not None:
            _cleanup_attempt(errors, "pending pipes", self._stdio.close)
            if not self._stdio._has_cleanup_resources():
                self._stdio = None
        exit_deadline = time.monotonic() + 1
        if self._unconfirmed_launch and self.process_handle:
            _cleanup_attempt(errors, "terminate process", lambda: self._terminate_failed_launch(exit_deadline))
        if self._job is not None:
            before_job_errors = len(errors)
            _cleanup_attempt(errors, "job", self._job.close)
            if len(errors) != before_job_errors and self.process_handle:
                self._unconfirmed_launch = True
                _cleanup_attempt(errors, "terminate after Job close failure", lambda: self._terminate_failed_launch(exit_deadline))
        if self.process_handle:
            # Job 的终止也是异步的；正常交付的进程同样先确认退出再释放身份。
            self._unconfirmed_launch = True
            _cleanup_attempt(errors, "confirm process exit", lambda: self._confirm_failed_launch_exit(exit_deadline))
        # 未确认终止时不关闭可能阻塞的读流，也不丢弃可重试的进程身份。
        if not self._unconfirmed_launch:
            for stream_name in ("stdout", "stderr"):
                _cleanup_attempt(errors, stream_name, lambda name=stream_name: close_stream(name))
            if self._stderr_drain is not None:
                _cleanup_attempt(errors, "internal stderr reader", self._join_stderr_drain)
            for attribute in ("thread_handle", "process_handle"):
                _cleanup_attempt(errors, attribute, lambda name=attribute: _close_owned_handle(self, name))
        if errors:
            first = errors[0][1]
            _add_cleanup_notes(first, errors)
            raise first

    def _failed_launch_exited(self, milliseconds: int) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure_kernel32(kernel32)
        result = kernel32.WaitForSingleObject(ctypes.c_void_p(self.process_handle), milliseconds)
        if result == 0:
            return True
        if result == 0x00000102:
            return False
        raise _last_error("WaitForSingleObject(cleanup)")

    def _terminate_failed_launch(self, exit_deadline: float) -> None:
        # 已退出进程再次 TerminateProcess 会报 5，先查同一个 HANDLE 的终态。
        if self._failed_launch_exited(0):
            self._unconfirmed_launch = False
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure_kernel32(kernel32)
        if not kernel32.TerminateProcess(ctypes.c_void_p(self.process_handle), 1):
            failure = _last_error("TerminateProcess")
            # False 可与异步退出竞争；只在同一 HANDLE 的有界等待后裁定失败。
            if self._failed_launch_exited(_remaining_exit_wait(exit_deadline)):
                self._unconfirmed_launch = False
                return
            raise failure

    def _confirm_failed_launch_exit(self, exit_deadline: float) -> None:
        if not self._failed_launch_exited(_remaining_exit_wait(exit_deadline)):
            raise AppContainerError("AppContainer process exit is unconfirmed")
        self._unconfirmed_launch = False

    def _has_cleanup_resources(self) -> bool:
        return bool(self.process_handle or self.thread_handle or (self._job is not None and self._job._handle) or any(stream is not None for stream in (self.stdin, self.stdout, self.stderr)) or (self._stdio is not None and self._stdio._has_cleanup_resources()) or (self._stderr_drain is not None and self._stderr_drain.is_alive()))

    def _join_stderr_drain(self) -> None:
        # 只等待本 owner 创建的线程；永久失效的 reader 明确报错，不能无限等待。
        if self._stderr_drain.ident is None:
            return
        self._stderr_drain.join(timeout=1)
        if self._stderr_drain.is_alive():
            raise AppContainerError("AppContainer internal stderr reader did not stop")


def _validate_stderr_reader(value: str) -> None:
    if type(value) is not str or value not in ("internal", "host"):
        raise ValueError("AppContainer stderr reader must be internal or host")


def _remaining_exit_wait(deadline: float) -> int:
    # 同一次 close 共用一个等待期限，避免 Terminate False 与确认各等一秒。
    return max(0, min(1000, int((deadline - time.monotonic()) * 1000)))


def _cleanup_attempt(errors: list[tuple[str, BaseException]], label: str, action) -> None:
    try:
        action()
    except BaseException as error:
        errors.append((label, error))


def _add_cleanup_notes(error: BaseException, errors: list[tuple[str, BaseException]]) -> None:
    for label, failure in errors:
        error.add_note(f"AppContainer cleanup {label}: {type(failure).__name__}: {failure}")
        if failure is not error:
            for note in getattr(failure, "__notes__", ()):
                error.add_note(note)


def _raise_cleanup_failures(errors: list[tuple[str, BaseException]]) -> None:
    if errors:
        first = errors[0][1]
        _add_cleanup_notes(first, errors)
        raise first


def _close_owned_handle(owner, attribute: str) -> None:
    handle = getattr(owner, attribute)
    if handle:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure_kernel32(kernel32)
        if not kernel32.CloseHandle(ctypes.c_void_p(handle)):
            raise _last_error("CloseHandle(" + attribute + ")")
        setattr(owner, attribute, 0)


def launch_in_appcontainer(spec: AppContainerLaunchSpec, profile: AppContainerProfile, *, resource_limits: AppContainerResourceLimits, stderr_reader: str = "internal") -> AppContainerProcess:
    """Start a zero-network worker only after its AppContainer Job is assigned.

    Failure after ``CreateProcessW`` terminates the suspended child and returns
    no usable process object.  There is intentionally no ordinary-token path.
    """

    _validate_stderr_reader(stderr_reader)
    _require_windows()
    if not isinstance(profile, AppContainerProfile):
        raise AppContainerError("AppContainer profile is required")
    profile.sid
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _configure_kernel32(kernel32)
    stdio: _AnonymousStdio | None = None
    attributes: _AttributeList | None = None
    owner: AppContainerProcess | None = None
    try:
        stdio = _AnonymousStdio.create()
        attributes = _security_attributes(profile.sid, stdio.child_handle_list())
        startup = _STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = stdio.child_stdin
        startup.StartupInfo.hStdOutput = stdio.child_stdout
        startup.StartupInfo.hStdError = stdio.child_stderr
        startup.lpAttributeList = attributes.pointer
        command = ctypes.create_unicode_buffer(_command_line(spec.executable, spec.argv))
        environment = _environment_block(spec.environment)
        process = _PROCESS_INFORMATION()
        flags = _CREATE_SUSPENDED | _CREATE_UNICODE_ENVIRONMENT | _EXTENDED_STARTUPINFO_PRESENT
        created = kernel32.CreateProcessW(str(spec.executable), command, None, None, True, flags, environment, str(spec.cwd), ctypes.byref(startup.StartupInfo), ctypes.byref(process))
        if not created:
            raise _last_error("CreateProcessW")
        # 从原生创建成功开始就只有一个 owner；后续任一步失败均交回同一身份。
        owner = AppContainerProcess(int(process.hProcess), int(process.hThread), int(process.dwProcessId), None, None, None, None, stderr_reader="host")
        owner._unconfirmed_launch = True
        owner._stdio = stdio
        stdio.close_child()
        owner._job = _NoBreakawayJob.create(resource_limits)
        owner._job.assign(int(process.hProcess))
        owner.stdin, owner.stdout, owner.stderr = stdio.parent_files()
        stdio.release_parent_files()
        if not stdio._has_cleanup_resources():
            owner._stdio = None
        _resume_suspended_process(kernel32, int(process.hThread))
        owner._activate_stderr_reader(stderr_reader)
        owner._unconfirmed_launch = False
        return owner
    except BaseException as error:
        errors: list[tuple[str, BaseException]] = []
        if owner is not None:
            pending_job = getattr(error, "_cleanup_job", None)
            # 配置失败的原 Job 在进程清理前收编，重复 close 仍使用同一身份。
            if owner._job is None and isinstance(pending_job, _NoBreakawayJob) and pending_job._handle:
                owner._job = pending_job
            _cleanup_attempt(errors, "process owner", owner.close)
            # 单次关闭前故障只重试原 owner 一次，永久失败保留身份供调用方接管。
            if owner._has_cleanup_resources():
                _cleanup_attempt(errors, "process owner retry", owner.close)
            if owner._has_cleanup_resources():
                error._cleanup_process = owner
        elif stdio is not None:
            _cleanup_attempt(errors, "pipes without process", stdio.close)
            if stdio._has_cleanup_resources():
                _cleanup_attempt(errors, "pipes without process retry", stdio.close)
            if stdio._has_cleanup_resources():
                error._cleanup_stdio = stdio
        _add_cleanup_notes(error, errors)
        raise
    finally:
        if attributes is not None:
            _delete_attribute_list(attributes.pointer)


def _security_attributes(sid: int, child_handles: tuple[int, ...]) -> _AttributeList:
    """Return AppContainer + explicit stdio-only inherited handle attributes."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _configure_kernel32(kernel32)
    size = ctypes.c_size_t()
    kernel32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
    if not size.value:
        raise _last_error("InitializeProcThreadAttributeList")
    buffer = ctypes.create_string_buffer(size.value)
    attribute_list = ctypes.cast(buffer, ctypes.c_void_p)
    if not kernel32.InitializeProcThreadAttributeList(attribute_list, 2, 0, ctypes.byref(size)):
        raise _last_error("InitializeProcThreadAttributeList")
    capabilities = _SECURITY_CAPABILITIES(ctypes.c_void_p(sid), None, 0, 0)
    if not kernel32.UpdateProcThreadAttribute(attribute_list, 0, _PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, ctypes.byref(capabilities), ctypes.sizeof(capabilities), None, None):
        _delete_attribute_list(int(attribute_list.value))
        raise _last_error("UpdateProcThreadAttribute")
    handles = (ctypes.c_void_p * len(child_handles))(*child_handles)
    if not kernel32.UpdateProcThreadAttribute(attribute_list, 0, _PROC_THREAD_ATTRIBUTE_HANDLE_LIST, ctypes.byref(handles), ctypes.sizeof(handles), None, None):
        _delete_attribute_list(int(attribute_list.value))
        raise _last_error("UpdateProcThreadAttribute(handle list)")
    # Keep both Python objects alive through CreateProcessW: the native list
    # contains a raw pointer to ``capabilities``.
    return _AttributeList(int(attribute_list.value), buffer, capabilities, handles)


def _delete_attribute_list(attribute_list: int) -> None:
    if attribute_list:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure_kernel32(kernel32)
        kernel32.DeleteProcThreadAttributeList(ctypes.c_void_p(attribute_list))


def _configure_kernel32(kernel32: ctypes.WinDLL) -> None:
    """Declare every pointer-sized ABI used by the launcher on 64-bit Windows."""

    from ctypes import wintypes

    kernel32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(_STARTUPINFOW), ctypes.POINTER(_PROCESS_INFORMATION)]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.DeleteProcThreadAttributeList.restype = None
    kernel32.CreatePipe.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(_SECURITY_ATTRIBUTES), wintypes.DWORD]
    kernel32.CreatePipe.restype = wintypes.BOOL
    kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    kernel32.SetHandleInformation.restype = wintypes.BOOL


def _command_line(executable: Path, argv: Sequence[str]) -> str:
    # subprocess.list2cmdline follows the quoting rules expected by CreateProcessW.
    return subprocess.list2cmdline([str(executable), *argv])


def _environment_block(environment: Mapping[str, str]) -> ctypes.Array[ctypes.c_wchar]:
    if not environment:
        return ctypes.create_unicode_buffer("\0\0", 2)
    entries = [f"{key}={value}" for key, value in sorted(environment.items(), key=lambda item: item[0].upper())]
    block = "\0".join(entries) + "\0\0"
    return ctypes.create_unicode_buffer(block, len(block))


def _descriptor_from_handle(handle: int, *, write: bool) -> int:
    """Transfer a raw handle to the CRT only when conversion succeeds."""

    if not handle:
        raise AppContainerError("missing parent stdio handle")
    import msvcrt

    flags = os.O_BINARY | (os.O_WRONLY if write else os.O_RDONLY)
    return msvcrt.open_osfhandle(handle, flags)


def _resume_suspended_process(kernel32: ctypes.WinDLL, thread_handle: int) -> None:
    if kernel32.ResumeThread(ctypes.c_void_p(thread_handle)) == _INVALID_DWORD:
        raise _last_error("ResumeThread")


def _require_windows() -> None:
    capability = probe_appcontainer()
    if not capability.available:
        raise AppContainerUnavailable(capability.reason or "windows-appcontainer-unavailable")


def _last_error(operation: str, error: int | None = None) -> AppContainerError:
    error = ctypes.get_last_error() if error is None else error
    return AppContainerError(f"{operation} failed: winerror={error}")
