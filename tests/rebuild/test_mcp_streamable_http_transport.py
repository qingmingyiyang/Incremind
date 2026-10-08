from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from core.mcp_host import (
    MCPFatalTransportError,
    MCPInputRequiredError,
    MCPHostConnectionConfig,
    MCPHttpResponse,
    MCPStreamableHTTPAuthority,
    MCPStreamableHTTPManifest,
    MCPStreamableHTTPTransport,
)
from core.ai_kernel import ToolDispatchCancelled


class _Secrets:
    def headers_for_wire(self, *, url, secret_header_refs, purpose):
        assert url == "https://mcp.example.test/rpc"
        assert purpose == "mcp_http_wire"
        return {
            name: "Bearer private-value"
            for name, secret_ref in secret_header_refs.items()
            if secret_ref == "mcp:remote-server:authorization"
        }


class _Store:
    def __init__(self, manifest: MCPStreamableHTTPManifest) -> None:
        self.manifest = manifest

    def get_approved_http(self, server_id: str):
        return self.manifest if server_id == "remote-server" else None


class _Control:
    def __init__(self) -> None:
        self.calls = 0

    def checkpoint(self) -> None:
        self.calls += 1


class _Requester:
    def __init__(self, *, sse_call: bool = False, expire_call: bool = False) -> None:
        self.calls: list[tuple[str, Mapping[str, str], object]] = []
        self.sse_call = sse_call
        self.expire_call = expire_call

    def request(self, method, url, *, headers, body, **_values):
        payload = json.loads(body) if body else None
        self.calls.append((method, dict(headers), payload))
        if method == "DELETE":
            return MCPHttpResponse(405, {"Content-Type": "application/json"}, b"")
        if payload["method"] in {"notifications/initialized", "notifications/cancelled"}:
            return MCPHttpResponse(202, {}, b"")
        request_id = payload["id"]
        if payload["method"] == "initialize":
            result = {"protocolVersion": "2025-11-25", "capabilities": {}, "serverInfo": {"name": "test", "version": "1"}}
            return MCPHttpResponse(200, {"Content-Type": "application/json", "MCP-Session-Id": "session-opaque-1"}, _envelope(request_id, result))
        if payload["method"] == "tools/list":
            return MCPHttpResponse(200, {"Content-Type": "application/json"}, _envelope(request_id, {"tools": []}))
        if payload["method"] == "ping":
            return MCPHttpResponse(200, {"Content-Type": "application/json"}, _envelope(request_id, {}))
        if self.expire_call:
            return MCPHttpResponse(404, {"Content-Type": "application/json"}, b"{}")
        result = {"content": [{"type": "text", "text": "safe"}]}
        if self.sse_call:
            notification = json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
            response = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})
            body_value = f"data: {notification}\n\ndata: {response}\n\n".encode()
            return MCPHttpResponse(200, {"Content-Type": "text/event-stream"}, body_value)
        return MCPHttpResponse(200, {"Content-Type": "application/json"}, _envelope(request_id, result))


def _envelope(request_id: int, result: object) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode()


def _invocation_envelope() -> dict[str, object]:
    return {
        "turn_id": "turn-mcp-1",
        "invocation_id": "tool-call-mcp-1",
        "operation_id": "operation-mcp-1",
        "idempotency_key": "operation-mcp-1:tool-call-mcp-1",
        "attempt": 1,
    }


def _config(*, protocol_profile: str = "legacy_2025_11_25"):
    manifest = MCPStreamableHTTPManifest(
        server_id="remote-server", manifest_revision=2, endpoint_identity="remote-endpoint-2",
        credential_subject_id="credential-2", transport_generation=3, approval_revision=4,
        approval_status="approved", endpoint_url="https://mcp.example.test/rpc",
        headers={"X-Client": "chriptmas"},
        secret_header_refs={"Authorization": "mcp:remote-server:authorization"},
        protocol_profile=protocol_profile,
    )
    host = MCPHostConnectionConfig(
        server_id="remote-server", manifest_revision=2, endpoint_identity="remote-endpoint-2",
        credential_subject_id="credential-2", transport_generation=3, catalog_revision=1,
        protocol_profile=protocol_profile,
    )
    return MCPStreamableHTTPAuthority(_Store(manifest)).resolve(host)


def test_http_transport_runs_session_json_sse_ping_and_delete() -> None:
    requester = _Requester(sse_call=True)
    transport = MCPStreamableHTTPTransport(_config(), secret_injector=_Secrets(), requester=requester)
    initialized = transport.initialize({"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
    transport.notify_initialized()
    assert transport.list_tools() == {"tools": []}
    transport.ping(timeout_ms=1000)
    control = _Control()
    result = transport.call_tool("review", {"value": 1}, timeout_ms=1000, execution_control=control)
    transport.close()

    assert initialized["protocolVersion"] == "2025-11-25"
    assert result["content"][0]["text"] == "safe"
    assert control.calls >= 2
    assert requester.calls[0][1].get("MCP-Session-Id") is None
    assert all(call[1].get("MCP-Protocol-Version") == "2025-11-25" for call in requester.calls)
    assert all(call[1].get("MCP-Session-Id") == "session-opaque-1" for call in requester.calls[1:])
    assert requester.calls[-1][0] == "DELETE"
    assert "private-value" not in repr(transport)


def test_http_tool_invocation_envelope_is_host_metadata_and_preserves_model_arguments() -> None:
    requester = _Requester()
    transport = MCPStreamableHTTPTransport(_config(), secret_injector=_Secrets(), requester=requester)
    transport.initialize({"protocolVersion": "2025-11-25", "capabilities": {}})
    transport.notify_initialized()
    arguments = {"_meta": {"untrusted": "model-value"}, "value": "preserve"}
    assert transport.call_tool(
        "review", arguments, timeout_ms=1_000, execution_control=_Control(),
        invocation_envelope=_invocation_envelope(),
    ) == {"content": [{"type": "text", "text": "safe"}]}
    payload = requester.calls[-1][2]
    assert payload["params"]["arguments"] == arguments
    assert payload["params"]["_meta"] == {"io.chriptmas/invocation": _invocation_envelope()}
    transport.close()


def test_http_stateless_tool_and_continuation_carry_host_invocation_envelope() -> None:
    class StatelessRequester(_Requester):
        def request(self, method, url, *, headers, body, **values):
            payload = json.loads(body) if body else None
            self.calls.append((method, dict(headers), payload))
            if payload["method"] == "server/discover":
                value = {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 1000, "cacheScope": "private", "resultType": "complete"}
            elif payload["method"] == "tools/list":
                value = {"tools": [], "ttlMs": 1000, "cacheScope": "private", "resultType": "complete"}
            else:
                value = {"content": [], "resultType": "complete"}
            return MCPHttpResponse(200, {"Content-Type": "application/json"}, _envelope(payload["id"], value))

    requester = StatelessRequester()
    transport = MCPStreamableHTTPTransport(_config(protocol_profile="stateless_2026_07_28"), secret_injector=_Secrets(), requester=requester)
    arguments = {"_meta": {"untrusted": "model-value"}, "value": "preserve"}
    transport.server_discover()
    transport.list_tools()
    assert transport.call_tool("review", arguments, timeout_ms=1_000, execution_control=_Control(), invocation_envelope=_invocation_envelope()) == {"content": []}
    assert transport.continue_tool("review", arguments, b"opaque-state", timeout_ms=1_000, execution_control=_Control(), invocation_envelope=_invocation_envelope()) == {"content": []}
    expected_meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "chriptmas-os", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.chriptmas/invocation": _invocation_envelope(),
    }
    for call in requester.calls[-2:]:
        assert call[2]["params"]["arguments"] == arguments
        assert call[2]["params"]["_meta"] == expected_meta
    assert requester.calls[-1][2]["params"]["requestState"] == "opaque-state"
    transport.close()


def test_http_invalid_invocation_envelope_fails_closed_before_tool_wire() -> None:
    requester = _Requester()
    transport = MCPStreamableHTTPTransport(_config(), secret_injector=_Secrets(), requester=requester)
    transport.initialize({"protocolVersion": "2025-11-25", "capabilities": {}})
    transport.notify_initialized()
    with pytest.raises(MCPFatalTransportError, match="invocation envelope"):
        transport.call_tool(
            "review", {}, timeout_ms=1_000, execution_control=_Control(),
            invocation_envelope={"turn_id": "turn-mcp-1"},
        )
    assert transport.failed_closed is True
    assert [call[2]["method"] for call in requester.calls] == ["initialize", "notifications/initialized"]


def test_http_stateless_profile_uses_per_request_headers_without_session_or_resume() -> None:
    class StatelessRequester(_Requester):
        def request(self, method, url, *, headers, body, **values):
            payload = json.loads(body) if body else None
            self.calls.append((method, dict(headers), payload))
            assert method == "POST"
            assert headers["MCP-Protocol-Version"] == "2026-07-28"
            assert headers["Mcp-Method"] == payload["method"]
            assert "MCP-Session-Id" not in headers
            assert payload["params"]["_meta"] == {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {"name": "chriptmas-os", "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": {},
            }
            if payload["method"] == "server/discover":
                return MCPHttpResponse(200, {"Content-Type": "application/json"}, _envelope(payload["id"], {
                    "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 1000, "cacheScope": "private", "resultType": "complete",
                }))
            if payload["method"] == "tools/list":
                return MCPHttpResponse(200, {"Content-Type": "application/json"}, _envelope(payload["id"], {"tools": [], "ttlMs": 1000, "cacheScope": "private", "resultType": "complete"}))
            assert headers["Mcp-Name"] == payload["params"]["name"]
            return MCPHttpResponse(200, {"Content-Type": "application/json"}, _envelope(payload["id"], {"content": [], "resultType": "complete"}))

    requester = StatelessRequester()
    transport = MCPStreamableHTTPTransport(_config(protocol_profile="stateless_2026_07_28"), secret_injector=_Secrets(), requester=requester)
    assert transport.server_discover()["supportedVersions"] == ["2026-07-28"]
    assert transport.list_tools() == {"tools": [], "ttlMs": 1000, "cacheScope": "private"}
    assert transport.call_tool("review", {}, timeout_ms=1_000, execution_control=_Control()) == {"content": []}
    transport.close()
    assert [call[0] for call in requester.calls] == ["POST", "POST", "POST"]


def test_http_stateless_projects_only_explicit_parameter_headers_and_encodes_name() -> None:
    class HeaderRequester(_Requester):
        def request(self, method, url, *, headers, body, **values):
            payload = json.loads(body) if body else None
            self.calls.append((method, dict(headers), payload))
            if payload["method"] == "server/discover":
                value = {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 1, "cacheScope": "private", "resultType": "complete"}
            elif payload["method"] == "tools/list":
                value = {"tools": [], "ttlMs": 1, "cacheScope": "private", "resultType": "complete"}
            else:
                assert headers["Mcp-Name"] == "=?base64?5Lit5paH?="
                assert headers["Mcp-Param-Query"] == "=?base64?5Lit5paH?="
                assert headers["Mcp-Param-Count"] == "2"
                assert "Mcp-Param-Unknown" not in headers
                value = {"content": [], "resultType": "complete"}
            return MCPHttpResponse(200, {"Content-Type": "application/json"}, _envelope(payload["id"], value))

    requester = HeaderRequester()
    transport = MCPStreamableHTTPTransport(_config(protocol_profile="stateless_2026_07_28"), secret_injector=_Secrets(), requester=requester)
    transport.server_discover()
    transport.list_tools()
    assert transport.call_tool("中文", {}, timeout_ms=1_000, execution_control=_Control(), parameter_headers={"Mcp-Param-Query": "=?base64?5Lit5paH?=", "Mcp-Param-Count": "2"}) == {"content": []}
    with pytest.raises(MCPFatalTransportError):
        transport.call_tool("review", {}, timeout_ms=1_000, execution_control=_Control(), parameter_headers={"X-Unexpected": "no"})


def test_http_stateless_recognizes_exact_header_mismatch_without_retry() -> None:
    class MismatchRequester(_Requester):
        def request(self, method, url, *, headers, body, **values):
            payload = json.loads(body) if body else None
            self.calls.append((method, dict(headers), payload))
            if payload["method"] == "server/discover":
                result = {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 1, "cacheScope": "private", "resultType": "complete"}
                return MCPHttpResponse(200, {"Content-Type": "application/json"}, _envelope(payload["id"], result))
            if payload["method"] == "tools/list":
                result = {"tools": [], "ttlMs": 1, "cacheScope": "private", "resultType": "complete"}
                return MCPHttpResponse(200, {"Content-Type": "application/json"}, _envelope(payload["id"], result))
            envelope = {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32020, "message": "HeaderMismatch"}}
            return MCPHttpResponse(400, {"Content-Type": "application/json"}, json.dumps(envelope).encode())

    requester = MismatchRequester()
    transport = MCPStreamableHTTPTransport(_config(protocol_profile="stateless_2026_07_28"), secret_injector=_Secrets(), requester=requester)
    transport.server_discover()
    transport.list_tools()
    with pytest.raises(MCPFatalTransportError, match="HeaderMismatch"):
        transport.call_tool("review", {}, timeout_ms=1_000, execution_control=_Control())
    assert [call[2]["method"] for call in requester.calls] == ["server/discover", "tools/list", "tools/call"]


@pytest.mark.parametrize(("mutation", "content_type"), [
    ({"id": 999}, "application/json"),
    ({"error": {"code": -32021, "message": "HeaderMismatch"}}, "application/json"),
    ({"error": {"code": -32020, "message": "header mismatch"}}, "application/json"),
    ({}, "text/plain"),
])
def test_http_stateless_does_not_misclassify_near_header_mismatch(mutation, content_type) -> None:
    class NearMismatchRequester(_Requester):
        def request(self, method, url, *, headers, body, **values):
            payload = json.loads(body) if body else None
            self.calls.append((method, dict(headers), payload))
            if payload["method"] == "server/discover":
                result = {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 1, "cacheScope": "private", "resultType": "complete"}
                return MCPHttpResponse(200, {"Content-Type": "application/json"}, _envelope(payload["id"], result))
            if payload["method"] == "tools/list":
                result = {"tools": [], "ttlMs": 1, "cacheScope": "private", "resultType": "complete"}
                return MCPHttpResponse(200, {"Content-Type": "application/json"}, _envelope(payload["id"], result))
            envelope = {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32020, "message": "HeaderMismatch"}}
            envelope.update(mutation)
            return MCPHttpResponse(400, {"Content-Type": content_type}, json.dumps(envelope).encode())

    requester = NearMismatchRequester()
    transport = MCPStreamableHTTPTransport(_config(protocol_profile="stateless_2026_07_28"), secret_injector=_Secrets(), requester=requester)
    transport.server_discover()
    transport.list_tools()
    with pytest.raises(MCPFatalTransportError, match="response status is invalid"):
        transport.call_tool("review", {}, timeout_ms=1_000, execution_control=_Control())
    assert [call[2]["method"] for call in requester.calls] == ["server/discover", "tools/list", "tools/call"]


@pytest.mark.parametrize(("result", "error"), [
    ({"content": []}, MCPFatalTransportError),
    ({"content": [], "resultType": "unknown"}, MCPFatalTransportError),
    ({"inputRequests": {}, "resultType": "input_required"}, MCPInputRequiredError),
])
def test_http_stateless_rejects_non_complete_tool_results_without_replay(result, error) -> None:
    class ResultRequester(_Requester):
        def request(self, method, url, *, headers, body, **values):
            payload = json.loads(body) if body else None
            self.calls.append((method, dict(headers), payload))
            if payload["method"] == "server/discover":
                value = {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 1000, "cacheScope": "private", "resultType": "complete"}
            elif payload["method"] == "tools/list":
                value = {"tools": [], "ttlMs": 1000, "cacheScope": "private", "resultType": "complete"}
            else:
                value = result
            return MCPHttpResponse(200, {"Content-Type": "application/json"}, _envelope(payload["id"], value))

    requester = ResultRequester()
    transport = MCPStreamableHTTPTransport(_config(protocol_profile="stateless_2026_07_28"), secret_injector=_Secrets(), requester=requester)
    transport.server_discover()
    transport.list_tools()
    with pytest.raises(error):
        transport.call_tool("review", {}, timeout_ms=1_000, execution_control=_Control())
    assert [call[0] for call in requester.calls] == ["POST", "POST", "POST"]
    assert [call[2]["method"] for call in requester.calls] == ["server/discover", "tools/list", "tools/call"]


def test_http_stateless_incomplete_stream_fails_closed_without_get_or_tool_replay() -> None:
    class BreakRequester(_Requester):
        def request(self, method, url, *, headers, body, **values):
            payload = json.loads(body) if body else None
            self.calls.append((method, dict(headers), payload))
            if payload["method"] == "server/discover":
                value = {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 1000, "cacheScope": "private", "resultType": "complete"}
                return MCPHttpResponse(200, {"Content-Type": "application/json"}, _envelope(payload["id"], value))
            if payload["method"] == "tools/list":
                return MCPHttpResponse(200, {"Content-Type": "application/json"}, _envelope(payload["id"], {"tools": [], "ttlMs": 1000, "cacheScope": "private", "resultType": "complete"}))
            return MCPHttpResponse(200, {"Content-Type": "text/event-stream"}, b"data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n\n", complete=False)

    requester = BreakRequester()
    transport = MCPStreamableHTTPTransport(_config(protocol_profile="stateless_2026_07_28"), secret_injector=_Secrets(), requester=requester)
    transport.server_discover()
    transport.list_tools()
    with pytest.raises(MCPFatalTransportError, match="incomplete"):
        transport.call_tool("review", {}, timeout_ms=1_000, execution_control=_Control())
    assert [call[0] for call in requester.calls] == ["POST", "POST", "POST"]


def test_http_stateless_cancellation_has_no_legacy_followup_post_or_session_cleanup() -> None:
    class CancelRequester(_Requester):
        def request(self, method, url, *, headers, body, **values):
            payload = json.loads(body) if body else None
            self.calls.append((method, dict(headers), payload))
            if payload["method"] == "server/discover":
                value = {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 1000, "cacheScope": "private", "resultType": "complete"}
                return MCPHttpResponse(200, {"Content-Type": "application/json"}, _envelope(payload["id"], value))
            if payload["method"] == "tools/list":
                return MCPHttpResponse(200, {"Content-Type": "application/json"}, _envelope(payload["id"], {"tools": [], "ttlMs": 1000, "cacheScope": "private", "resultType": "complete"}))
            raise ToolDispatchCancelled(provider_started=True)

    requester = CancelRequester()
    transport = MCPStreamableHTTPTransport(_config(protocol_profile="stateless_2026_07_28"), secret_injector=_Secrets(), requester=requester)
    transport.server_discover()
    transport.list_tools()
    with pytest.raises(ToolDispatchCancelled):
        transport.call_tool("review", {}, timeout_ms=1_000, execution_control=_Control())
    assert [call[0] for call in requester.calls] == ["POST", "POST", "POST"]
    assert [call[2]["method"] for call in requester.calls] == ["server/discover", "tools/list", "tools/call"]
    assert transport.failed_closed is True


def test_http_session_expiry_is_fatal_and_never_retries_tool() -> None:
    requester = _Requester(expire_call=True)
    transport = MCPStreamableHTTPTransport(_config(), secret_injector=_Secrets(), requester=requester)
    transport.initialize({"protocolVersion": "2025-11-25"})
    transport.notify_initialized()
    with pytest.raises(MCPFatalTransportError, match="session expired"):
        transport.call_tool("write", {}, timeout_ms=1000, execution_control=_Control())
    assert [call[2]["method"] for call in requester.calls if call[2]] == [
        "initialize", "notifications/initialized", "tools/call",
    ]


def test_http_incomplete_sse_resumes_with_cursor_without_reposting_tool() -> None:
    class ResumeRequester(_Requester):
        def request(self, method, url, *, headers, body, **values):
            if method == "GET":
                self.calls.append((method, dict(headers), None))
                response = json.dumps({
                    "jsonrpc": "2.0", "id": 2,
                    "result": {"content": [{"type": "text", "text": "resumed"}]},
                })
                return MCPHttpResponse(200, {"Content-Type": "text/event-stream"}, f"id: event-2\ndata: {response}\n\n".encode())
            response = super().request(method, url, headers=headers, body=body, **values)
            payload = json.loads(body) if body else None
            if payload and payload.get("method") == "tools/call":
                return MCPHttpResponse(
                    200, {"Content-Type": "text/event-stream"},
                    b"id: event-1\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n\n",
                    complete=False,
                )
            return response

    requester = ResumeRequester()
    transport = MCPStreamableHTTPTransport(_config(), secret_injector=_Secrets(), requester=requester)
    transport.initialize({"protocolVersion": "2025-11-25"})
    transport.notify_initialized()
    result = transport.call_tool("write", {}, timeout_ms=1000, execution_control=_Control())

    assert result["content"][0]["text"] == "resumed"
    assert [call[0] for call in requester.calls].count("POST") == 3
    assert requester.calls[-1][0] == "GET"
    assert requester.calls[-1][1]["Last-Event-ID"] == "event-1"
    assert requester.calls[-1][1]["MCP-Session-Id"] == "session-opaque-1"


def test_http_incomplete_sse_without_cursor_fails_closed_without_reposting_tool() -> None:
    class NoCursorRequester(_Requester):
        def request(self, method, url, *, headers, body, **values):
            response = super().request(method, url, headers=headers, body=body, **values)
            payload = json.loads(body) if body else None
            if payload and payload.get("method") == "tools/call":
                return MCPHttpResponse(
                    200, {"Content-Type": "text/event-stream"},
                    b"data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n\n",
                    complete=False,
                )
            return response

    requester = NoCursorRequester()
    transport = MCPStreamableHTTPTransport(_config(), secret_injector=_Secrets(), requester=requester)
    transport.initialize({"protocolVersion": "2025-11-25"})
    transport.notify_initialized()
    with pytest.raises(MCPFatalTransportError, match="incomplete"):
        transport.call_tool("write", {}, timeout_ms=1000, execution_control=_Control())
    assert [call[2]["method"] for call in requester.calls if call[2]] == [
        "initialize", "notifications/initialized", "tools/call",
    ]


def test_http_incomplete_sse_does_not_resume_from_partial_event_identity() -> None:
    class PartialIdentityRequester(_Requester):
        def request(self, method, url, *, headers, body, **values):
            response = super().request(method, url, headers=headers, body=body, **values)
            payload = json.loads(body) if body else None
            if payload and payload.get("method") == "tools/call":
                return MCPHttpResponse(
                    200, {"Content-Type": "text/event-stream"},
                    b"id: uncommitted-event",
                    complete=False,
                )
            return response

    requester = PartialIdentityRequester()
    transport = MCPStreamableHTTPTransport(_config(), secret_injector=_Secrets(), requester=requester)
    transport.initialize({"protocolVersion": "2025-11-25"})
    transport.notify_initialized()
    with pytest.raises(MCPFatalTransportError, match="incomplete"):
        transport.call_tool("write", {}, timeout_ms=1000, execution_control=_Control())
    assert all(call[0] != "GET" for call in requester.calls)


def test_http_cancel_during_sse_resume_terminates_session_without_reposting_tool() -> None:
    class CancelResumeRequester(_Requester):
        def request(self, method, url, *, headers, body, **values):
            if method == "GET":
                self.calls.append((method, dict(headers), None))
                raise ToolDispatchCancelled(provider_started=True)
            response = super().request(method, url, headers=headers, body=body, **values)
            payload = json.loads(body) if body else None
            if payload and payload.get("method") == "tools/call":
                return MCPHttpResponse(
                    200, {"Content-Type": "text/event-stream"},
                    b"id: event-1\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n\n",
                    complete=False,
                )
            return response

    requester = CancelResumeRequester()
    transport = MCPStreamableHTTPTransport(_config(), secret_injector=_Secrets(), requester=requester)
    transport.initialize({"protocolVersion": "2025-11-25"})
    transport.notify_initialized()
    with pytest.raises(ToolDispatchCancelled):
        transport.call_tool("write", {}, timeout_ms=1000, execution_control=_Control())
    methods = [call[2].get("method") for call in requester.calls if call[2]]
    assert methods == ["initialize", "notifications/initialized", "tools/call", "notifications/cancelled"]
    assert requester.calls[-1][0] == "DELETE"
    assert transport.failed_closed is True


def test_http_incomplete_sse_with_wrong_response_identity_fails_without_resume() -> None:
    class WrongIdentityRequester(_Requester):
        def request(self, method, url, *, headers, body, **values):
            response = super().request(method, url, headers=headers, body=body, **values)
            payload = json.loads(body) if body else None
            if payload and payload.get("method") == "tools/call":
                return MCPHttpResponse(
                    200, {"Content-Type": "text/event-stream"},
                    b'id: event-1\ndata: {"jsonrpc":"2.0","id":999,"result":{}}\n\n',
                    complete=False,
                )
            return response

    requester = WrongIdentityRequester()
    transport = MCPStreamableHTTPTransport(_config(), secret_injector=_Secrets(), requester=requester)
    transport.initialize({"protocolVersion": "2025-11-25"})
    transport.notify_initialized()
    with pytest.raises(MCPFatalTransportError, match="identity"):
        transport.call_tool("write", {}, timeout_ms=1000, execution_control=_Control())
    assert all(call[0] != "GET" for call in requester.calls)


def test_http_unterminated_result_event_is_not_treated_as_complete() -> None:
    class UnterminatedResultRequester(_Requester):
        def request(self, method, url, *, headers, body, **values):
            if method == "GET":
                self.calls.append((method, dict(headers), None))
                result = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"content": []}})
                return MCPHttpResponse(200, {"Content-Type": "text/event-stream"}, f"id: event-2\ndata: {result}\n\n".encode())
            response = super().request(method, url, headers=headers, body=body, **values)
            payload = json.loads(body) if body else None
            if payload and payload.get("method") == "tools/call":
                return MCPHttpResponse(
                    200, {"Content-Type": "text/event-stream"},
                    b'id: event-1\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
                    b'id: event-2\ndata: {"jsonrpc":"2.0","id":2,"result":{"content":[]}}',
                    complete=False,
                )
            return response

    requester = UnterminatedResultRequester()
    transport = MCPStreamableHTTPTransport(_config(), secret_injector=_Secrets(), requester=requester)
    transport.initialize({"protocolVersion": "2025-11-25"})
    transport.notify_initialized()
    assert transport.call_tool("write", {}, timeout_ms=1000, execution_control=_Control()) == {"content": []}
    assert requester.calls[-1][0] == "GET"
    assert requester.calls[-1][1]["Last-Event-ID"] == "event-1"


def test_http_local_cancel_sends_notification_then_terminates_session_without_retry() -> None:
    class CancellingRequester(_Requester):
        def request(self, method, url, *, headers, body, control_check=None, **values):
            payload = json.loads(body) if body else None
            if payload and payload.get("method") == "tools/call" and control_check is not None:
                self.calls.append((method, dict(headers), payload))
                raise ToolDispatchCancelled(provider_started=True)
            return super().request(method, url, headers=headers, body=body, control_check=control_check, **values)

    requester = CancellingRequester()
    transport = MCPStreamableHTTPTransport(_config(), secret_injector=_Secrets(), requester=requester)
    transport.initialize({"protocolVersion": "2025-11-25"})
    transport.notify_initialized()
    with pytest.raises(ToolDispatchCancelled):
        transport.call_tool("write", {}, timeout_ms=1000, execution_control=_Control())
    methods = [call[2].get("method") for call in requester.calls if call[2]]
    assert methods == ["initialize", "notifications/initialized", "tools/call", "notifications/cancelled"]
    assert requester.calls[-1][0] == "DELETE"
    assert transport.failed_closed is True


def test_http_post_wire_cancellation_generation_drift_suppresses_cleanup_without_rewriting_cancel() -> None:
    class CancellingRequester(_Requester):
        def request(self, method, url, *, headers, body, control_check=None, **values):
            payload = json.loads(body) if body else None
            if payload and payload.get("method") == "tools/call" and control_check is not None:
                self.calls.append((method, dict(headers), payload))
                # The Tool POST has crossed the local wire boundary.  A later
                # generation check must not reinterpret that delivery as none.
                raise ToolDispatchCancelled(provider_started=True)
            return super().request(method, url, headers=headers, body=body, control_check=control_check, **values)

    checks = iter((True, True, True, False))
    requester = CancellingRequester()
    transport = MCPStreamableHTTPTransport(
        _config(), secret_injector=_Secrets(), requester=requester,
        credential_generation_current=lambda: next(checks),
    )
    transport.initialize({"protocolVersion": "2025-11-25"})
    transport.notify_initialized()

    with pytest.raises(ToolDispatchCancelled):
        transport.call_tool("write", {}, timeout_ms=1000, execution_control=_Control())

    assert [call[0] for call in requester.calls] == ["POST", "POST", "POST"]
    assert [call[2]["method"] for call in requester.calls] == [
        "initialize", "notifications/initialized", "tools/call",
    ]
    assert transport.failed_closed is True


@pytest.mark.parametrize("endpoint", [
    "http://mcp.example.test/rpc",
    "https://user@mcp.example.test/rpc",
    "https://mcp.example.test/rpc#fragment",
])
def test_http_manifest_rejects_unsafe_endpoint(endpoint: str) -> None:
    with pytest.raises(ValueError):
        MCPStreamableHTTPManifest(
            server_id="remote-server", manifest_revision=1, endpoint_identity="endpoint-1",
            credential_subject_id="credential-1", transport_generation=1, approval_revision=1,
            approval_status="approved", endpoint_url=endpoint,
        )


def test_http_manifest_rejects_case_insensitive_duplicate_and_literal_secret_headers() -> None:
    common = dict(
        server_id="remote-server", manifest_revision=1, endpoint_identity="endpoint-1",
        credential_subject_id="credential-1", transport_generation=1, approval_revision=1,
        approval_status="approved", endpoint_url="https://mcp.example.test/rpc",
    )
    with pytest.raises(ValueError, match="headers"):
        MCPStreamableHTTPManifest(
            **common,
            secret_header_refs={
                "Authorization": "mcp:remote-server:one",
                "authorization": "mcp:remote-server:two",
            },
        )
    with pytest.raises(ValueError, match="headers"):
        MCPStreamableHTTPManifest(**common, headers={"Authorization": "Bearer literal"})
    with pytest.raises(ValueError, match="headers"):
        MCPStreamableHTTPManifest(**common, headers={"Mcp-Param-Query": "must-be-compiled"})
