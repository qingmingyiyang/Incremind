from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Protocol

from core.ai_kernel import ToolDispatchCancelled, ToolDispatchDeadlineExceeded

from .host import (
    MCPCredentialGenerationChanged,
    MCPFatalTransportError,
    MCPInputRequiredError,
    MCP_PROTOCOL_VERSION,
    MCP_STATELESS_PROTOCOL_VERSION,
)
from .invocation_envelope import INVOCATION_META_KEY, validated_invocation_envelope
from .header_projection import HeaderProjectionError, encode_header_value
from .streamable_http_config import (
    MCPStreamableHTTPConnectionConfig,
    MCPStreamableHTTPSecretInjectorPort,
)


_PARAMETER_HEADER = re.compile(r"^Mcp-Param-[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


@dataclass(frozen=True, slots=True)
class MCPHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    complete: bool = True


class MCPHttpRequesterPort(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
        max_response_bytes: int,
        control_check: Callable[[], None] | None = None,
    ) -> MCPHttpResponse: ...


class MCPStreamableHTTPTransport:
    """Strict MCP 2025-11-25 Streamable HTTP client with no Tool retry."""

    def __init__(
        self,
        config: MCPStreamableHTTPConnectionConfig,
        *,
        secret_injector: MCPStreamableHTTPSecretInjectorPort,
        requester: MCPHttpRequesterPort,
        credential_generation_current: Callable[[], bool] | None = None,
    ) -> None:
        self._config = config
        self._requester = requester
        self._secret_injector = secret_injector
        self._credential_generation_current = credential_generation_current or (lambda: True)
        self._session_id: str | None = None
        self._next_id = 1
        self._initialized = False
        self._closed = False
        self._lock = RLock()

    @property
    def _stateless(self) -> bool:
        return self._config.manifest.protocol_profile == "stateless_2026_07_28"

    @property
    def _protocol_version(self) -> str:
        return MCP_STATELESS_PROTOCOL_VERSION if self._stateless else MCP_PROTOCOL_VERSION

    @property
    def failed_closed(self) -> bool:
        return self._closed

    def initialize(self, request: Mapping[str, object]) -> Mapping[str, object]:
        with self._lock:
            if self._stateless:
                self._fatal("MCP HTTP initialize is unavailable for stateless profile")
            if self._closed or self._initialized:
                self._fatal("MCP HTTP lifecycle is invalid")
            result, response = self._rpc("initialize", request, timeout_ms=None, control_check=None, include_session=False)
            session = _header(response.headers, "mcp-session-id")
            if session is not None:
                if not session or len(session) > 512 or any(not 0x21 <= ord(char) <= 0x7E for char in session):
                    self._fatal("MCP HTTP session identity is invalid")
                self._session_id = session
            self._initialized = True
            return result

    def notify_initialized(self) -> None:
        with self._lock:
            if self._stateless:
                self._fatal("MCP HTTP initialized notification is unavailable for stateless profile")
            self._require_initialized()
            response = self._send(
                "POST", {"jsonrpc": "2.0", "method": "notifications/initialized"},
                timeout_ms=None, control_check=None,
            )
            if response.status != 202 or response.body:
                self._fatal("MCP HTTP initialized notification was rejected")

    def list_tools(self, cursor: str | None = None) -> Mapping[str, object]:
        with self._lock:
            self._require_initialized()
            params: dict[str, object] = {}
            if cursor is not None:
                params["cursor"] = cursor
            result, _response = self._rpc("tools/list", params, timeout_ms=None, control_check=None)
            return result

    def server_discover(self) -> Mapping[str, object]:
        with self._lock:
            if not self._stateless or self._closed:
                self._fatal("MCP HTTP discovery lifecycle is invalid")
            result, _response = self._rpc("server/discover", {}, timeout_ms=None, control_check=None)
            self._initialized = True
            return result

    def ping(self, *, timeout_ms: int) -> None:
        with self._lock:
            if self._stateless:
                self._fatal("MCP HTTP ping is unavailable for stateless profile")
            self._require_initialized()
            result, _response = self._rpc("ping", {}, timeout_ms=timeout_ms, control_check=None)
            if result:
                self._fatal("MCP HTTP ping result is invalid")

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
            self._require_initialized()
            checkpoint = getattr(execution_control, "checkpoint", None)
            if not callable(checkpoint):
                self._fatal("MCP HTTP execution control is invalid")
            try:
                checkpoint()
                try:
                    envelope = validated_invocation_envelope(invocation_envelope)
                except ValueError:
                    self._fatal("MCP HTTP invocation envelope is invalid")
                result, _response = self._rpc(
                    "tools/call", {"name": name, "arguments": dict(arguments)},
                    timeout_ms=timeout_ms, control_check=checkpoint, cancel_on_abort=True,
                    parameter_headers=parameter_headers,
                    invocation_envelope=envelope,
                )
                checkpoint()
                return result
            except (ToolDispatchCancelled, ToolDispatchDeadlineExceeded):
                raise

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
        """Send the explicit stateless continuation once, with a new RPC id."""
        with self._lock:
            self._require_initialized()
            if not self._stateless:
                self._fatal("MCP HTTP continuation is unavailable for legacy profile")
            checkpoint = getattr(execution_control, "checkpoint", None)
            if not callable(checkpoint) or not isinstance(name, str) or not name or not isinstance(arguments, Mapping):
                self._fatal("MCP HTTP continuation request is invalid")
            if not isinstance(request_state, bytes) or not request_state or len(request_state) > 4096:
                self._fatal("MCP HTTP continuation state is invalid")
            try:
                state = request_state.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                self._fatal("MCP HTTP continuation state is invalid")
            checkpoint()
            try:
                envelope = validated_invocation_envelope(invocation_envelope)
            except ValueError:
                self._fatal("MCP HTTP invocation envelope is invalid")
            result, _response = self._rpc(
                "tools/call", {"name": name, "arguments": dict(arguments), "requestState": state},
                timeout_ms=timeout_ms, control_check=checkpoint, cancel_on_abort=True,
                parameter_headers=parameter_headers,
                invocation_envelope=envelope,
            )
            checkpoint()
            return result

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            session = self._session_id
            self._closed = True
            self._initialized = False
            self._session_id = None
            if session is None or self._stateless:
                return
            if not self._generation_current():
                self.abort_local()
                return
            try:
                headers = self._wire_headers(session=session, accept="application/json")
                response = self._requester.request(
                    "DELETE", self._config.manifest.endpoint_url, headers=headers, body=None,
                    timeout_seconds=self._config.manifest.timeout_seconds,
                    max_response_bytes=1024,
                )
                if response.status not in {200, 202, 204, 405}:
                    return
            except Exception:
                return

    def abort_local(self) -> None:
        """Irreversibly discard a credentialed session without wire cleanup.

        Credential-generation drift is a local authority revocation, not a
        remote protocol failure.  It must not send DELETE, cancellation, SSE
        resume, or any other request using the stale session or headers.
        """
        with self._lock:
            self._closed = True
            self._initialized = False
            self._session_id = None

    def _rpc(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout_ms: int | None,
        control_check: Callable[[], None] | None,
        include_session: bool = True,
        cancel_on_abort: bool = False,
        parameter_headers: Mapping[str, str] | None = None,
        invocation_envelope: Mapping[str, object] | None = None,
    ) -> tuple[Mapping[str, object], MCPHttpResponse]:
        request_id = self._next_id
        self._next_id += 1
        values = dict(params)
        if self._stateless:
            if "_meta" in values:
                self._fatal("MCP HTTP caller cannot override stateless metadata")
            values["_meta"] = _stateless_meta(invocation_envelope)
        elif invocation_envelope is not None:
            if "_meta" in values:
                self._fatal("MCP HTTP caller cannot override Host metadata")
            values["_meta"] = {INVOCATION_META_KEY: dict(invocation_envelope)}
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": values}
        deadline = monotonic() + timeout_ms / 1000 if timeout_ms is not None else None
        try:
            response = self._send(
                "POST", payload, timeout_ms=_remaining_timeout_ms(deadline), control_check=control_check,
                include_session=include_session, parameter_headers=parameter_headers,
            )
            response = self._resume_sse(
                response, request_id,
                timeout_ms=_remaining_timeout_ms(deadline), control_check=control_check,
            )
        except (ToolDispatchCancelled, ToolDispatchDeadlineExceeded):
            if cancel_on_abort:
                if self._stateless:
                    # Stateless 2026 has no session cancellation lifecycle.
                    # The ambiguous original Tool POST is the final wire action.
                    self._closed = True
                    self._initialized = False
                    self._session_id = None
                else:
                    self._cancel_and_terminate(request_id)
            raise
        envelope = _response_envelope(response, request_id, self._config.manifest.max_sse_events)
        result = envelope.get("result")
        if not isinstance(result, Mapping):
            self._fatal("MCP HTTP result is invalid")
        if not self._stateless:
            return dict(result), response
        try:
            return _stateless_result(result), response
        except MCPInputRequiredError:
            self._closed = True
            self._initialized = False
            self._session_id = None
            raise

    def _resume_sse(
        self,
        response: MCPHttpResponse,
        request_id: int,
        *,
        timeout_ms: int | None,
        control_check: Callable[[], None] | None,
    ) -> MCPHttpResponse:
        if response.complete or not _is_sse(response):
            return response
        if self._stateless:
            self._fatal("MCP HTTP stateless stream is incomplete")
        envelope = _response_envelope(
            response, request_id, self._config.manifest.max_sse_events,
            allow_incomplete=True, complete_sse_events_only=True,
        )
        if envelope is not None:
            return MCPHttpResponse(response.status, response.headers, response.body, complete=True)
        cursor = _last_sse_event_id(response.body, self._config.manifest.max_sse_events)
        if cursor is None or self._session_id is None:
            self._fatal("MCP HTTP SSE response is incomplete")
        try:
            if not self._generation_current():
                self.abort_local()
                raise MCPFatalTransportError("MCP credential generation changed")
            resumed = self._requester.request(
                "GET",
                self._config.manifest.endpoint_url,
                headers={
                    **self._wire_headers(session=self._session_id, accept="text/event-stream"),
                    "Last-Event-ID": cursor,
                },
                body=None,
                timeout_seconds=min(
                    self._config.manifest.timeout_seconds,
                    timeout_ms / 1000 if timeout_ms is not None else self._config.manifest.timeout_seconds,
                ),
                max_response_bytes=self._config.manifest.max_response_bytes,
                control_check=control_check,
            )
        except (ToolDispatchCancelled, ToolDispatchDeadlineExceeded):
            raise
        except MCPCredentialGenerationChanged:
            raise
        except Exception:
            self._fatal("MCP HTTP SSE resume failed")
        if resumed.status == 404:
            self._fatal("MCP HTTP session expired")
        if resumed.status < 200 or resumed.status >= 300 or not resumed.complete or not _is_sse(resumed):
            self._fatal("MCP HTTP SSE resume failed")
        return resumed

    def _send(
        self,
        method: str,
        payload: Mapping[str, object],
        *,
        timeout_ms: int | None,
        control_check: Callable[[], None] | None,
        include_session: bool = True,
        parameter_headers: Mapping[str, str] | None = None,
    ) -> MCPHttpResponse:
        try:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers = self._wire_headers(
                session=self._session_id if include_session else None,
                accept="application/json, text/event-stream",
            )
            if self._stateless:
                rpc_method = payload.get("method")
                if not isinstance(rpc_method, str) or not rpc_method:
                    self._fatal("MCP HTTP stateless method is invalid")
                headers["Mcp-Method"] = rpc_method
                params = payload.get("params")
                if rpc_method == "tools/call":
                    name = params.get("name") if isinstance(params, Mapping) else None
                    if not isinstance(name, str) or not name:
                        self._fatal("MCP HTTP stateless tool name is invalid")
                    headers["Mcp-Name"] = encode_header_value(name)
                    headers.update(_parameter_headers(parameter_headers, headers))
                elif parameter_headers:
                    self._fatal("MCP HTTP parameter headers are only valid for tools/call")
            elif parameter_headers:
                self._fatal("MCP HTTP parameter headers are unavailable for legacy profile")
            rpc_method = payload.get("method")
            self._check_generation_before_wire(rpc_method)
            response = self._requester.request(
                method,
                self._config.manifest.endpoint_url,
                headers=headers,
                body=body,
                timeout_seconds=min(
                    self._config.manifest.timeout_seconds,
                    timeout_ms / 1000 if timeout_ms is not None else self._config.manifest.timeout_seconds,
                ),
                max_response_bytes=self._config.manifest.max_response_bytes,
                control_check=control_check,
            )
        except (ToolDispatchCancelled, ToolDispatchDeadlineExceeded):
            raise
        except MCPCredentialGenerationChanged:
            raise
        except Exception:
            self._fatal("MCP HTTP request failed")
        if response.status == 404 and self._session_id is not None:
            self._fatal("MCP HTTP session expired")
        if response.status == 400 and _is_header_mismatch(response, payload.get("id")):
            self._fatal("MCP HTTP HeaderMismatch")
        if response.status < 200 or response.status >= 300:
            self._fatal("MCP HTTP response status is invalid")
        return response

    def _wire_headers(self, *, session: str | None, accept: str) -> dict[str, str]:
        headers = {
            **self._config.build_headers(self._secret_injector, purpose="mcp_http_wire"),
            "Accept": accept,
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self._protocol_version,
        }
        if session is not None:
            headers["MCP-Session-Id"] = session
        return headers

    def _cancel_and_terminate(self, request_id: int) -> None:
        session = self._session_id
        cleanup_timeout = min(self._config.manifest.timeout_seconds, 2.0)
        if not self._generation_current():
            self.abort_local()
            return
        try:
            payload = json.dumps({
                "jsonrpc": "2.0", "method": "notifications/cancelled",
                "params": {"requestId": request_id, "reason": "local execution cancelled"},
            }, separators=(",", ":")).encode("utf-8")
            self._requester.request(
                "POST", self._config.manifest.endpoint_url,
                headers=self._wire_headers(session=session, accept="application/json, text/event-stream"),
                body=payload, timeout_seconds=cleanup_timeout,
                max_response_bytes=1024,
            )
        except Exception:
            pass
        if session is not None:
            try:
                if not self._generation_current():
                    self.abort_local()
                    return
                self._requester.request(
                    "DELETE", self._config.manifest.endpoint_url,
                    headers=self._wire_headers(session=session, accept="application/json"),
                    body=None, timeout_seconds=cleanup_timeout,
                    max_response_bytes=1024,
                )
            except Exception:
                pass
        self._session_id = None
        self._initialized = False
        self._closed = True

    def _require_initialized(self) -> None:
        if self._closed or not self._initialized:
            self._fatal("MCP HTTP lifecycle is invalid")

    def _generation_current(self) -> bool:
        try:
            return self._credential_generation_current() is True
        except Exception:
            return False

    def _check_generation_before_wire(self, rpc_method: object) -> None:
        if self._generation_current():
            return
        self.abort_local()
        if rpc_method == "tools/call":
            raise MCPCredentialGenerationChanged("MCP credential generation changed")
        raise MCPFatalTransportError("MCP credential generation changed")

    def _fatal(self, message: str):
        self._closed = True
        self._initialized = False
        self._session_id = None
        raise MCPFatalTransportError(message)


def _parameter_headers(
    value: Mapping[str, str] | None,
    existing: Mapping[str, str],
) -> dict[str, str]:
    """Accept only the Host's explicit stateless parameter header namespace."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise HeaderProjectionError("MCP HTTP parameter headers are invalid")
    result: dict[str, str] = {}
    occupied = {name.casefold() for name in existing}
    for name, item in value.items():
        normalized = name.casefold() if isinstance(name, str) else ""
        if (
            not isinstance(name, str)
            or not _PARAMETER_HEADER.fullmatch(name)
            or not isinstance(item, str)
            or normalized in occupied
            or normalized in {known.casefold() for known in result}
        ):
            raise HeaderProjectionError("MCP HTTP parameter headers are invalid")
        # The compiler owns suffix grammar and value encoding.  The transport
        # still rejects newline/NUL injection if called outside the Host seam.
        if any(char in item for char in "\r\n\x00"):
            raise HeaderProjectionError("MCP HTTP parameter headers are invalid")
        result[name] = item
    return result


def _response_envelope(
    response: MCPHttpResponse,
    request_id: int,
    max_events: int,
    *,
    allow_incomplete: bool = False,
    complete_sse_events_only: bool = False,
) -> Mapping[str, object] | None:
    media_type = (_header(response.headers, "content-type") or "").split(";", 1)[0].strip().lower()
    if media_type == "application/json":
        values = [_json_object(response.body)]
    elif media_type == "text/event-stream":
        values = _sse_objects(response.body, max_events, complete_only=complete_sse_events_only)
    else:
        raise MCPFatalTransportError("MCP HTTP content type is invalid")
    matched: list[Mapping[str, object]] = []
    for value in values:
        if value.get("jsonrpc") != "2.0":
            raise MCPFatalTransportError("MCP HTTP JSON-RPC envelope is invalid")
        if "id" not in value:
            method = value.get("method")
            if not isinstance(method, str) or not method.startswith("notifications/"):
                raise MCPFatalTransportError("MCP HTTP server request is unsupported")
            continue
        if value.get("id") != request_id:
            raise MCPFatalTransportError("MCP HTTP response identity is invalid")
        matched.append(value)
    if not matched and allow_incomplete:
        return None
    if len(matched) != 1 or "error" in matched[0] or set(matched[0]) - {"jsonrpc", "id", "result"}:
        raise MCPFatalTransportError("MCP HTTP response is invalid")
    return matched[0]


def _is_sse(response: MCPHttpResponse) -> bool:
    return ((_header(response.headers, "content-type") or "").split(";", 1)[0].strip().lower()
            == "text/event-stream")


def _last_sse_event_id(body: bytes, maximum: int) -> str | None:
    try:
        text = body.decode("utf-8", "strict").replace("\r\n", "\n")
    except UnicodeDecodeError as error:
        raise MCPFatalTransportError("MCP HTTP SSE encoding is invalid") from error
    cursor: str | None = None
    event_count = 0
    complete_events = text.split("\n\n")
    if not text.endswith("\n\n"):
        complete_events.pop()
    for event in complete_events:
        if not event.strip():
            continue
        event_count += 1
        if event_count > maximum:
            raise MCPFatalTransportError("MCP HTTP SSE event limit exceeded")
        event_ids = [line[3:].lstrip() for line in event.split("\n") if line.startswith("id:")]
        if len(event_ids) > 1 or any("\x00" in value or "\r" in value or "\n" in value for value in event_ids):
            raise MCPFatalTransportError("MCP HTTP SSE event identity is invalid")
        if event_ids:
            if (
                not event_ids[0] or len(event_ids[0]) > 512
                or any(not 0x21 <= ord(char) <= 0x7E for char in event_ids[0])
            ):
                raise MCPFatalTransportError("MCP HTTP SSE event identity is invalid")
            cursor = event_ids[0]
    return cursor


def _sse_objects(body: bytes, maximum: int, *, complete_only: bool = False) -> list[Mapping[str, object]]:
    try:
        text = body.decode("utf-8", "strict").replace("\r\n", "\n")
    except UnicodeDecodeError as error:
        raise MCPFatalTransportError("MCP HTTP SSE encoding is invalid") from error
    events = text.split("\n\n")
    if complete_only and not text.endswith("\n\n"):
        events.pop()
    values: list[Mapping[str, object]] = []
    for event in events:
        if not event.strip():
            continue
        data = [line[5:].lstrip() for line in event.split("\n") if line.startswith("data:")]
        if not data:
            continue
        values.append(_json_object("\n".join(data).encode("utf-8")))
        if len(values) > maximum:
            raise MCPFatalTransportError("MCP HTTP SSE event limit exceeded")
    if not values:
        raise MCPFatalTransportError("MCP HTTP SSE response is incomplete")
    return values


def _json_object(body: bytes) -> Mapping[str, object]:
    try:
        value = json.loads(body.decode("utf-8", "strict"))
    except Exception:
        raise MCPFatalTransportError("MCP HTTP JSON response is invalid") from None
    if not isinstance(value, Mapping):
        raise MCPFatalTransportError("MCP HTTP JSON response is invalid")
    return dict(value)


def _is_header_mismatch(response: MCPHttpResponse, request_id: object) -> bool:
    if request_id is None:
        return False
    media_type = (_header(response.headers, "content-type") or "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        return False
    try:
        envelope = _json_object(response.body)
    except MCPFatalTransportError:
        return False
    error = envelope.get("error")
    return (
        envelope.get("jsonrpc") == "2.0"
        and envelope.get("id") == request_id
        and isinstance(error, Mapping)
        and error.get("code") == -32020
        and error.get("message") == "HeaderMismatch"
    )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    return next((str(value) for key, value in headers.items() if key.casefold() == name.casefold()), None)


def _remaining_timeout_ms(deadline: float | None) -> int | None:
    if deadline is None:
        return None
    remaining = int((deadline - monotonic()) * 1000)
    if remaining <= 0:
        raise ToolDispatchDeadlineExceeded(provider_started=True)
    return remaining


def _stateless_meta(invocation_envelope: Mapping[str, object] | None = None) -> dict[str, object]:
    meta: dict[str, object] = {
        "io.modelcontextprotocol/protocolVersion": MCP_STATELESS_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": {"name": "chriptmas-os", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    if invocation_envelope is not None:
        meta[INVOCATION_META_KEY] = dict(invocation_envelope)
    return meta


def _stateless_result(result: Mapping[str, object]) -> Mapping[str, object]:
    result_type = result.get("resultType")
    if result_type == "input_required":
        # 2026 requestState is opaque transport data, not a client prompt.
        # JSON has already decoded it as Unicode; preserve the strict UTF-8
        # representation that the JSON-RPC encoder will use on continuation.
        if "requestState" not in result:
            # Compatibility fail-closed path.  It is not continuable because
            # this client never advertises interactive input capability.
            raise MCPInputRequiredError("MCP stateless input is required")
        state = result.get("requestState")
        if (
            not isinstance(state, str)
            or not state
            or len(state.encode("utf-8")) > 4096
            or set(result) - {"resultType", "requestState"}
        ):
            raise MCPFatalTransportError("MCP stateless continuation state is invalid")
        raise MCPInputRequiredError(
            "MCP stateless continuation is required",
            request_state=state.encode("utf-8"),
        )
    if result_type != "complete":
        raise MCPFatalTransportError("MCP stateless result type is invalid")
    return {key: value for key, value in result.items() if key != "resultType"}
