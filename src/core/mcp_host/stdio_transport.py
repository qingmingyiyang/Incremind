from __future__ import annotations

import json
import os
import queue
import subprocess
import time
from collections.abc import Mapping
from threading import RLock, Thread
from typing import Callable, Final

import psutil

from .host import (
    MCPCredentialGenerationChanged,
    MCPFatalTransportError,
    MCPHostConnectionConfig,
    MCPInputRequiredError,
    MCPTransportPort,
)
from .invocation_envelope import INVOCATION_META_KEY, validated_invocation_envelope
from .stdio_config import MCPStdioConnectionConfig


class MCPStdioTransportError(MCPFatalTransportError):
    """Safe stdio transport failure without process, secret, or wire details."""


_FRAME_OVERFLOW: Final = object()


class MCPStdioTransport(MCPTransportPort):
    """Bounded binary-pipe JSON-RPC transport for a reviewed local MCP command."""

    def __init__(
        self,
        config: MCPStdioConnectionConfig,
        *,
        host_connection: MCPHostConnectionConfig,
        credential_generation_current: Callable[[], bool] | None = None,
        startup_timeout_ms: int = 10_000,
        max_frame_bytes: int = 64 * 1024,
        max_notifications_per_request: int = 32,
    ) -> None:
        if not isinstance(startup_timeout_ms, int) or isinstance(startup_timeout_ms, bool) or not 1 <= startup_timeout_ms <= 120_000:
            raise MCPStdioTransportError("MCP stdio startup timeout is invalid")
        if not isinstance(max_frame_bytes, int) or isinstance(max_frame_bytes, bool) or not 256 <= max_frame_bytes <= 1_048_576:
            raise MCPStdioTransportError("MCP stdio frame limit is invalid")
        if not isinstance(max_notifications_per_request, int) or isinstance(max_notifications_per_request, bool) or not 0 <= max_notifications_per_request <= 256:
            raise MCPStdioTransportError("MCP stdio notification limit is invalid")
        config.assert_matches(host_connection)
        self._config = config
        self._credential_generation_current = credential_generation_current or (lambda: True)
        self._startup_timeout_ms = startup_timeout_ms
        self._max_frame_bytes = max_frame_bytes
        self._max_notifications = max_notifications_per_request
        self._lock = RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._frames: queue.Queue[bytes | None | object] | None = None
        self._reader: Thread | None = None
        self._next_request_id = 1
        self._in_flight = False
        self._closed = False
        self._initialized = False
        self._ready = False

    @property
    def failed_closed(self) -> bool:
        return self._closed

    def initialize(self, request: Mapping[str, object]) -> Mapping[str, object]:
        if self._config.protocol_profile != "legacy_2025_11_25":
            self._fail_closed("MCP stdio initialize is unavailable for stateless profile")
        with self._lock:
            if self._closed:
                raise MCPStdioTransportError("MCP stdio transport is closed")
            if self._initialized:
                self._fail_closed("MCP stdio lifecycle is invalid")
        result = self._request("initialize", _mapping(request), self._startup_timeout_ms, None)
        self._initialized = True
        return result

    def notify_initialized(self) -> None:
        if self._config.protocol_profile != "legacy_2025_11_25":
            self._fail_closed("MCP stdio initialized notification is unavailable for stateless profile")
        with self._lock:
            if not self._initialized:
                self._fail_closed("MCP stdio lifecycle is invalid")
            if self._ready:
                self._fail_closed("MCP stdio lifecycle is invalid")
            self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._ready = True

    def list_tools(self, cursor: str | None = None) -> Mapping[str, object]:
        with self._lock:
            if self._closed:
                raise MCPStdioTransportError("MCP stdio transport is closed")
            if not self._ready:
                self._fail_closed("MCP stdio lifecycle is invalid")
        params: dict[str, object] = {}
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor:
                self._fail_closed("MCP stdio cursor is invalid")
            params["cursor"] = cursor
        return self._request("tools/list", params, self._startup_timeout_ms, None)

    def server_discover(self) -> Mapping[str, object]:
        with self._lock:
            if self._closed:
                raise MCPStdioTransportError("MCP stdio transport is closed")
            if self._config.protocol_profile != "stateless_2026_07_28":
                self._fail_closed("MCP stdio discovery is unavailable for legacy profile")
        result = self._request("server/discover", {}, self._startup_timeout_ms, None)
        self._ready = True
        return result

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        timeout_ms: int,
        execution_control: object,
        parameter_headers: Mapping[str, str] | None = None,
        invocation_envelope: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        with self._lock:
            if self._closed:
                raise MCPStdioTransportError("MCP stdio transport is closed")
            if not self._ready:
                self._fail_closed("MCP stdio lifecycle is invalid")
        if not isinstance(name, str) or not name:
            self._fail_closed("MCP stdio tool name is invalid")
        if not isinstance(arguments, Mapping):
            self._fail_closed("MCP stdio arguments are invalid")
        # Stdio has no HTTP field channel.  The Host passes the same frozen
        # plan to every transport, and local stdio intentionally ignores it.
        _ = parameter_headers
        try:
            envelope = validated_invocation_envelope(invocation_envelope)
        except ValueError:
            self._fail_closed("MCP stdio invocation envelope is invalid")
        return self._request(
            "tools/call", {"name": name, "arguments": dict(arguments)}, timeout_ms, execution_control,
            invocation_envelope=envelope,
        )

    def continue_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        request_state: bytes,
        *,
        timeout_ms: int,
        execution_control: object,
        parameter_headers: Mapping[str, str] | None = None,
        invocation_envelope: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        if self._config.protocol_profile != "stateless_2026_07_28":
            self._fail_closed("MCP stdio continuation is unavailable for legacy profile")
        if not isinstance(request_state, bytes) or not request_state or len(request_state) > 4096:
            self._fail_closed("MCP stdio continuation state is invalid")
        try:
            state = request_state.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            self._fail_closed("MCP stdio continuation state is invalid")
        _ = parameter_headers
        try:
            envelope = validated_invocation_envelope(invocation_envelope)
        except ValueError:
            self._fail_closed("MCP stdio invocation envelope is invalid")
        return self._request(
            "tools/call", {"name": name, "arguments": dict(arguments), "requestState": state},
            timeout_ms, execution_control, invocation_envelope=envelope,
        )

    def ping(self, *, timeout_ms: int) -> None:
        if self._config.protocol_profile != "legacy_2025_11_25":
            self._fail_closed("MCP stdio ping is unavailable for stateless profile")
        with self._lock:
            if self._closed:
                raise MCPStdioTransportError("MCP stdio transport is closed")
            if not self._ready:
                self._fail_closed("MCP stdio lifecycle is invalid")
        # MCP 2025-11-25 models ping as EmptyResult, which still permits
        # protocol metadata and extensions. The common response parser has
        # already required a JSON object, so no remote fields are consumed.
        self._request("ping", {}, timeout_ms, None)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            process, self._process = self._process, None
            self._initialized = False
            self._ready = False
            if process is None:
                return
            # Capture descendants before closing stdin.  A compliant server may
            # exit immediately on EOF and orphan helpers before a later tree
            # walk can still associate them with the original parent.
            known_targets = _process_targets(process)
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            _terminate_tree(process, known_targets=known_targets)

    def _request(
        self,
        method: str,
        params: Mapping[str, object],
        timeout_ms: int,
        execution_control: object | None,
        *,
        invocation_envelope: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms < 1:
            self._fail_closed("MCP stdio timeout is invalid")
        with self._lock:
            if self._in_flight:
                self._fail_closed("MCP stdio concurrent request is invalid")
            self._spawn_if_needed(method)
            request_id = self._next_request_id
            self._next_request_id += 1
            self._in_flight = True
            try:
                self._checkpoint(execution_control)
                self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": self._params(params, invocation_envelope=invocation_envelope)})
                self._checkpoint(execution_control)
                result = self._read_response(request_id, timeout_ms, execution_control)
                if self._config.protocol_profile != "stateless_2026_07_28":
                    return result
                try:
                    return _stateless_result(result)
                except MCPInputRequiredError:
                    self.close()
                    raise
            finally:
                self._in_flight = False

    def _params(
        self,
        params: Mapping[str, object],
        *,
        invocation_envelope: Mapping[str, object] | None,
    ) -> dict[str, object]:
        values = dict(params)
        if "_meta" in values:
            self._fail_closed("MCP stdio caller cannot override Host metadata")
        if self._config.protocol_profile == "stateless_2026_07_28":
            values["_meta"] = _stateless_meta(invocation_envelope)
        elif invocation_envelope is not None:
            values["_meta"] = {INVOCATION_META_KEY: dict(invocation_envelope)}
        return values

    def _spawn_if_needed(self, method: str) -> None:
        if self._closed:
            raise MCPStdioTransportError("MCP stdio transport is closed")
        if self._process is not None:
            return
        try:
            self._check_generation_before_wire(method)
            environment = self._config.build_environment()
            process = subprocess.Popen(
                [self._config.executable, *self._config.argv],
                cwd=self._config.cwd,
                env=environment,
                shell=False,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=False,
                creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
        except Exception:
            self._closed = True
            raise MCPStdioTransportError("MCP stdio process could not start") from None
        if process.stdin is None or process.stdout is None:
            self._closed = True
            _terminate_tree(process)
            raise MCPStdioTransportError("MCP stdio process pipes are unavailable")
        frames: queue.Queue[bytes | None | object] = queue.Queue(maxsize=self._max_notifications + 2)
        reader = Thread(target=_read_frames, args=(process.stdout, frames, self._max_frame_bytes), daemon=True)
        reader.start()
        self._process, self._frames, self._reader = process, frames, reader

    def _write(self, payload: Mapping[str, object]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            self._fail_closed("MCP stdio process is unavailable")
        try:
            frame = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8", "strict")
        except (TypeError, UnicodeError):
            self._fail_closed("MCP stdio request is invalid")
        if len(frame) > self._max_frame_bytes:
            self._fail_closed("MCP stdio request exceeds frame limit")
        self._check_generation_before_wire(payload.get("method"))
        try:
            process.stdin.write(frame + b"\n")
            process.stdin.flush()
        except OSError:
            self._fail_closed("MCP stdio write failed")

    def _generation_current(self) -> bool:
        try:
            return self._credential_generation_current() is True
        except Exception:
            return False

    def _check_generation_before_wire(self, method: object) -> None:
        if self._generation_current():
            return
        self.close()
        if method == "tools/call":
            raise MCPCredentialGenerationChanged("MCP credential generation changed")
        raise MCPStdioTransportError("MCP credential generation changed")

    def _read_response(
        self,
        request_id: int,
        timeout_ms: int,
        execution_control: object | None,
    ) -> Mapping[str, object]:
        frames = self._frames
        if frames is None:
            self._fail_closed("MCP stdio process is unavailable")
        deadline = time.monotonic() + timeout_ms / 1000.0
        notifications = 0
        while True:
            self._checkpoint(execution_control)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._fail_closed("MCP stdio request timed out")
            try:
                raw = frames.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if raw is None:
                self._fail_closed("MCP stdio process ended")
            if raw is _FRAME_OVERFLOW:
                self._fail_closed("MCP stdio frame queue limit exceeded")
            try:
                assert isinstance(raw, bytes)
                message = _decode_message(raw, self._max_frame_bytes)
            except MCPStdioTransportError:
                self._fail_closed("MCP stdio frame is invalid")
            if "method" in message:
                if "id" in message:
                    self._fail_closed("MCP stdio server requests are unsupported")
                if (
                    set(message) - {"jsonrpc", "method", "params"}
                    or not isinstance(message.get("method"), str)
                    or not message["method"]
                    or ("params" in message and not isinstance(message["params"], Mapping))
                ):
                    self._fail_closed("MCP stdio notification is invalid")
                notifications += 1
                if notifications > self._max_notifications:
                    self._fail_closed("MCP stdio notification limit exceeded")
                continue
            if type(message.get("id")) is not int or message["id"] != request_id:
                self._fail_closed("MCP stdio response identity is invalid")
            if set(message) - {"jsonrpc", "id", "result"} or "result" not in message or "error" in message:
                self._fail_closed("MCP stdio response is invalid")
            result = message["result"]
            if not isinstance(result, Mapping):
                self._fail_closed("MCP stdio result is invalid")
            return dict(result)

    def _checkpoint(self, execution_control: object | None) -> None:
        if execution_control is None:
            return
        checkpoint = getattr(execution_control, "checkpoint", None)
        if not callable(checkpoint):
            self._fail_closed("MCP stdio execution control is invalid")
        try:
            checkpoint()
        except BaseException:
            self.close()
            raise

    def _fail_closed(self, message: str) -> None:
        self.close()
        raise MCPStdioTransportError(message) from None


def _read_frames(
    stream,
    frames: queue.Queue[bytes | None | object],
    max_frame_bytes: int,
) -> None:
    try:
        while True:
            frame = stream.readline(max_frame_bytes + 1)
            if not frame:
                try:
                    frames.put(None, timeout=0.05)
                except queue.Full:
                    while True:
                        try:
                            frames.get_nowait()
                        except queue.Empty:
                            break
                    frames.put_nowait(_FRAME_OVERFLOW)
                return
            try:
                frames.put(frame, timeout=0.05)
            except queue.Full:
                while True:
                    try:
                        frames.get_nowait()
                    except queue.Empty:
                        break
                frames.put_nowait(_FRAME_OVERFLOW)
                return
    except OSError:
        try:
            frames.put_nowait(None)
        except queue.Full:
            pass


def _decode_message(raw: bytes, max_frame_bytes: int) -> dict[str, object]:
    if len(raw) > max_frame_bytes or not raw.endswith(b"\n") or b"\r" in raw:
        raise MCPStdioTransportError("MCP stdio frame is invalid")
    try:
        decoded = raw[:-1].decode("utf-8", "strict")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise MCPStdioTransportError("MCP stdio frame is invalid") from None
    if not isinstance(value, Mapping) or value.get("jsonrpc") != "2.0":
        raise MCPStdioTransportError("MCP stdio JSON-RPC envelope is invalid")
    return dict(value)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise MCPStdioTransportError("MCP stdio request is invalid")
    return dict(value)


def _stateless_meta(invocation_envelope: Mapping[str, object] | None = None) -> dict[str, object]:
    meta: dict[str, object] = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "chriptmas-os", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    if invocation_envelope is not None:
        meta[INVOCATION_META_KEY] = dict(invocation_envelope)
    return meta


def _stateless_result(result: Mapping[str, object]) -> Mapping[str, object]:
    result_type = result.get("resultType")
    if result_type == "input_required":
        if "requestState" not in result:
            raise MCPInputRequiredError("MCP stateless input is required")
        state = result.get("requestState")
        if (
            not isinstance(state, str)
            or not state
            or len(state.encode("utf-8")) > 4096
            or set(result) - {"resultType", "requestState"}
        ):
            raise MCPStdioTransportError("MCP stateless continuation state is invalid")
        raise MCPInputRequiredError(
            "MCP stateless continuation is required",
            request_state=state.encode("utf-8"),
        )
    if result_type != "complete":
        raise MCPStdioTransportError("MCP stateless result type is invalid")
    return {key: value for key, value in result.items() if key != "resultType"}


def _process_targets(process: subprocess.Popen[bytes]) -> tuple[psutil.Process, ...]:
    try:
        parent = psutil.Process(process.pid)
        return (*parent.children(recursive=True), parent)
    except (psutil.Error, OSError):
        return ()


def _terminate_tree(
    process: subprocess.Popen[bytes],
    *,
    known_targets: tuple[psutil.Process, ...] = (),
) -> None:
    targets_by_pid = {target.pid: target for target in (*known_targets, *_process_targets(process))}
    targets = list(targets_by_pid.values())
    for target in reversed(targets):
        try:
            if target.is_running():
                target.terminate()
        except (psutil.Error, OSError):
            pass
    _gone, alive = psutil.wait_procs(targets, timeout=0.5) if targets else ([], [])
    for target in alive:
        try:
            target.kill()
        except (psutil.Error, OSError):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
