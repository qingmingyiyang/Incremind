"""Strict, host-owned stdio runner for the disabled Plugin Hands boundary."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import ctypes
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, BinaryIO, Protocol

import psutil

from .contracts import PLUGIN_HANDS_PROTOCOL, PluginHandsControl, PluginHandsError, PluginHandsInvocation, PluginHandsLaunch, PluginHandsOutcome, plugin_hands_protocol_identity_environment
from .workspace import PluginHandsWorkspace, assert_workspace_resource_shape


MAX_FRAME_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
_BASE_FIELDS = frozenset({"protocol", "type", "launch_id", "lease_id", "invocation_id"})


class PluginHandsStdioTransport(Protocol):
    """The deliberately small process-facing surface used by `plugin-hands/1`.

    Containment launchers provide this surface after they have created their
    process.  Protocol code cannot inspect a PID, command line, environment,
    or any containment details.
    """

    stdin: BinaryIO | None
    stdout: BinaryIO | None

    def wait(self, timeout: float | None = None) -> object: ...


class PluginHandsProtocolExchange:
    """Run one strict `plugin-hands/1` exchange over a host-owned transport."""

    def exchange(self, transport: PluginHandsStdioTransport, launch: PluginHandsLaunch, invocation: PluginHandsInvocation, deadline_at: float, control: PluginHandsControl) -> PluginHandsOutcome:
        hello = self._read_frame(transport, deadline_at, control, 0)
        _validate_message(hello, "hello", launch, invocation, set(_BASE_FIELDS))
        self._write_frame(transport, {"protocol": PLUGIN_HANDS_PROTOCOL, "type": "invoke", "launch_id": launch.launch_id, "lease_id": invocation.lease.lease_id, "invocation_id": invocation.invocation_id, "input": dict(invocation.input)})
        terminal = self._read_frame(transport, deadline_at, control, MAX_FRAME_BYTES)
        kind = terminal.get("type") if isinstance(terminal, Mapping) else None
        if kind == "result":
            _validate_message(terminal, "result", launch, invocation, {*_BASE_FIELDS, "output"})
            output = terminal.get("output")
            if not isinstance(output, Mapping):
                raise _ProtocolViolation("protocol-error")
            self._require_single_terminal(transport, deadline_at)
            return PluginHandsOutcome(invocation.invocation_id, invocation.lease.lease_id, "success", output=dict(output))
        if kind == "error":
            _validate_message(terminal, "error", launch, invocation, {*_BASE_FIELDS, "code"})
            self._require_single_terminal(transport, deadline_at)
            return _unknown(invocation, "child-error")
        raise _ProtocolViolation("protocol-error")

    def exchange_outcome(self, transport: PluginHandsStdioTransport, launch: PluginHandsLaunch, invocation: PluginHandsInvocation, deadline_at: float, control: PluginHandsControl) -> PluginHandsOutcome:
        """Map every post-spawn uncertainty to an explicit unknown outcome."""

        try:
            return self.exchange(transport, launch, invocation, deadline_at, control)
        except _Cancelled:
            return _unknown(invocation, "cancelled")
        except _TimedOut:
            return _unknown(invocation, "deadline-exceeded")
        except _ProtocolViolation as error:
            return _unknown(invocation, error.code)
        except (OSError, ValueError, subprocess.SubprocessError):
            return _unknown(invocation, "runner-failed")

    def _write_frame(self, transport: PluginHandsStdioTransport, value: Mapping[str, object]) -> None:
        if transport.stdin is None:
            raise _ProtocolViolation("eof")
        try:
            encoded = _encode_frame(value)
        except PluginHandsError as error:
            raise _ProtocolViolation("input-invalid") from error
        try:
            transport.stdin.write(encoded + b"\n")
            transport.stdin.flush()
            transport.stdin.close()
        except OSError as error:
            raise _ProtocolViolation("eof") from error

    def _read_frame(self, transport: PluginHandsStdioTransport, deadline_at: float, control: PluginHandsControl, prior_output: int) -> dict[str, object]:
        if transport.stdout is None:
            raise _ProtocolViolation("eof")
        result: list[bytes] = []
        done = threading.Event()

        def read() -> None:
            try:
                result.append(transport.stdout.readline(MAX_FRAME_BYTES + 1))
            except (OSError, ValueError):
                result.append(b"")
            finally:
                done.set()

        threading.Thread(target=read, daemon=True).start()
        while not done.wait(0.01):
            if control.cancelled():
                raise _Cancelled()
            if time.monotonic() >= deadline_at:
                raise _TimedOut()
        if control.cancelled():
            raise _Cancelled()
        frame = result[0]
        if not frame:
            raise _ProtocolViolation("eof")
        if len(frame) > MAX_FRAME_BYTES or not frame.endswith(b"\n") or prior_output + len(frame) > MAX_OUTPUT_BYTES:
            raise _ProtocolViolation("output-oversize")
        try:
            value = json.loads(frame[:-1].decode("utf-8"), object_pairs_hook=_no_duplicate_object, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise _ProtocolViolation("protocol-error") from error
        if not isinstance(value, dict):
            raise _ProtocolViolation("protocol-error")
        return value

    def _require_single_terminal(self, transport: PluginHandsStdioTransport, deadline_at: float) -> None:
        try:
            completed = transport.wait(timeout=max(0.001, deadline_at - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise _TimedOut() from error
        if completed is None:
            raise _TimedOut()
        if transport.stdout is not None and transport.stdout.read(1):
            raise _ProtocolViolation("protocol-error")


class PluginHandsStdioRunner:
    """Execute one already-authorized invocation without Plugin-owned launch control."""

    def run(self, launch: PluginHandsLaunch, workspace: PluginHandsWorkspace, invocation: PluginHandsInvocation, control: PluginHandsControl = PluginHandsControl()) -> PluginHandsOutcome:
        try:
            validate_plugin_hands_request(launch, workspace, invocation, control)
        except (PluginHandsError, TypeError, ValueError):
            return _failed(invocation, "invalid-invocation")

        process: subprocess.Popen[bytes] | None = None
        job: _WindowsJob | None = None
        try:
            if control.cancelled():
                return _failed(invocation, "cancelled-before-spawn")
            process = self._spawn(launch, workspace, invocation)
            job = _WindowsJob.attach(process) if os.name == "nt" else None
            deadline_at = time.monotonic() + invocation.deadline_ms / 1000
            return PluginHandsProtocolExchange().exchange_outcome(process, launch, invocation, deadline_at, control)
        except (OSError, ValueError, subprocess.SubprocessError):
            return _unknown(invocation, "runner-failed") if process is not None else _failed(invocation, "spawn-failed")
        finally:
            if process is not None:
                _terminate_tree(process)
            if job is not None:
                job.close()

    def _spawn(self, launch: PluginHandsLaunch, workspace: PluginHandsWorkspace, invocation: PluginHandsInvocation) -> subprocess.Popen[bytes]:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        return subprocess.Popen([str(launch.executable), *launch.argv], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False, cwd=str(workspace.root), env=plugin_hands_environment(launch, workspace, invocation), creationflags=flags, start_new_session=os.name != "nt")

class _WindowsJob:
    """Windows process-tree lifetime fence; this is not a security sandbox."""

    _KILL_ON_JOB_CLOSE = 0x00002000
    _EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, handle: int) -> None:
        self._handle = handle

    @classmethod
    def attach(cls, process: subprocess.Popen[bytes]) -> _WindowsJob:
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimitInformation), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = cls._KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, cls._EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, "SetInformationJobObject failed")
        if not kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(int(process._handle))):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, "AssignProcessToJobObject failed")
        return cls(handle)

    def close(self) -> None:
        if self._handle:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self._handle)
            self._handle = 0


class _ProtocolViolation(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


class _TimedOut(Exception):
    pass


class _Cancelled(Exception):
    pass


def validate_plugin_hands_request(launch: PluginHandsLaunch, workspace: PluginHandsWorkspace, invocation: PluginHandsInvocation, control: PluginHandsControl) -> None:
    if not isinstance(launch, PluginHandsLaunch) or not isinstance(workspace, PluginHandsWorkspace) or not isinstance(invocation, PluginHandsInvocation) or not isinstance(control, PluginHandsControl):
        raise TypeError
    if launch != invocation.launch or workspace.lease != invocation.lease:
        raise ValueError
    assert_workspace_resource_shape(workspace)
    expires_at = datetime.fromisoformat(invocation.lease.expires_at[:-1] + "+00:00")
    if expires_at <= datetime.now(timezone.utc):
        raise ValueError
    _encode_frame({"protocol": PLUGIN_HANDS_PROTOCOL, "type": "invoke", "launch_id": launch.launch_id, "lease_id": invocation.lease.lease_id, "invocation_id": invocation.invocation_id, "input": dict(invocation.input)})


def plugin_hands_environment(launch: PluginHandsLaunch, workspace: PluginHandsWorkspace, invocation: PluginHandsInvocation) -> dict[str, str]:
    root = str(workspace.root)
    environment = dict(launch.environment)
    environment.update({"SYSTEMROOT": os.environ.get("SYSTEMROOT", "C:\\Windows"), "PATH": str(launch.executable.parent), "TEMP": root, "TMP": root})
    environment.update(plugin_hands_protocol_identity_environment(launch, invocation))
    return environment


def _encode_frame(value: Mapping[str, object]) -> bytes:
    try:
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PluginHandsError("Plugin Hands input is not JSON serializable") from error
    if len(encoded) + 1 > MAX_FRAME_BYTES:
        raise PluginHandsError("Plugin Hands input exceeds the protocol frame limit")
    return encoded


def _validate_message(value: Mapping[str, object], kind: str, launch: PluginHandsLaunch, invocation: PluginHandsInvocation, fields: set[str]) -> None:
    if set(value) != fields or value.get("protocol") != PLUGIN_HANDS_PROTOCOL or value.get("type") != kind:
        raise _ProtocolViolation("protocol-error")
    if value.get("launch_id") != launch.launch_id or value.get("lease_id") != invocation.lease.lease_id or value.get("invocation_id") != invocation.invocation_id:
        raise _ProtocolViolation("identity-mismatch")


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON member")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _failed(invocation: PluginHandsInvocation, code: str) -> PluginHandsOutcome:
    return PluginHandsOutcome(invocation.invocation_id, invocation.lease.lease_id, "failed", error_code=code)


def _unknown(invocation: PluginHandsInvocation, code: str) -> PluginHandsOutcome:
    return PluginHandsOutcome(invocation.invocation_id, invocation.lease.lease_id, "unknown", error_code=code)


def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    try:
        root = psutil.Process(process.pid)
        members = root.children(recursive=True) + [root]
    except (psutil.Error, OSError):
        members = []
    for member in reversed(members):
        try:
            member.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(members, timeout=0.5)
    for member in alive:
        try:
            member.kill()
        except psutil.Error:
            pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
