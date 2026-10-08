from __future__ import annotations

import json
import sys
import time
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi import FastAPI
import psutil
import pytest

from backend.api import ai_runtime
from backend.api.mcp_runtime import build_mcp_connection_manager, shutdown_ai_mcp_runtime
from core.mcp_host import MCPHttpResponse
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from backend.security.secrets import InMemorySecretStore
from core.ai_kernel import InMemoryTurnPayloadStore, ScopedCapabilityRegistry, ToolProviderFailure
from core.ai_tooling import (
    MCPServerSelectionBinding,
    ToolSelectionBinding,
    tool_contract_binding_identity,
    tool_contract_identity,
    tool_from_capability,
)


ROOT = Path(__file__).resolve().parents[4]


class _Search:
    def search(self, **_kwargs):
        return SimpleNamespace(hits=())


class _ExecutionControl:
    remaining_timeout_ms = 5_000

    def checkpoint(self) -> None:
        return None


def test_production_runtime_composes_profiled_stateless_stdio_and_persists_2026_receipt(
    tmp_path: Path, monkeypatch,
) -> None:
    child = tmp_path / "stateless_mcp_child.py"
    child.write_text(_stateless_child_program(), encoding="utf-8")
    pid_path = tmp_path / "stateless_mcp_child.pid"
    _write_stateless_authority(tmp_path, child, pid_path)
    _stub_native_dependencies(monkeypatch)
    application = FastAPI()
    runtime = ai_runtime.build_ai_runtime(
        SimpleNamespace(root_dir=tmp_path, secret_store=InMemorySecretStore({"mcp:calendar-server:token": "private-token"})),
        application=application,
    )
    manager = runtime.mcp_connection_manager
    assert manager.connected_server_ids == ("calendar-server",)
    definition, provider = runtime._registry.resolve("calendar.search")
    tool = definition.tool_definition
    assert tool is not None and tool.connection_identity.protocol_version == "2026-07-28"
    turn = runtime.submit_turn(_turn_request())
    result = provider.invoke({
        "tool_call_id": "stateless-call", "turn_id": turn.turn_id, "operation_id": "stateless-operation",
        "arguments": {"query": "calendar"}, "execution_context": _ExecutionControl(),
        "tool_contract": tool_contract_identity(tool),
    })
    receipt = runtime._payloads.get(result["receipt_ref"])
    assert receipt["protocol_version"] == "2026-07-28"
    child_pid = int(pid_path.read_text(encoding="utf-8"))
    assert psutil.pid_exists(child_pid)

    shutdown_ai_mcp_runtime(application)

    deadline = time.monotonic() + 2
    while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert manager.connected_server_ids == ()
    assert runtime._registry.resolve("calendar.search") is None
    assert not psutil.pid_exists(child_pid)


def test_production_runtime_composes_approved_stdio_with_project_filter_and_shutdown(
    tmp_path: Path, monkeypatch,
) -> None:
    child = tmp_path / "approved_mcp_child.py"
    child.write_text(_child_program(), encoding="utf-8")
    _write_authority(tmp_path, child)
    _stub_native_dependencies(monkeypatch)
    application = FastAPI()
    secrets = InMemorySecretStore({"mcp:calendar-server:token": "private-token"})

    runtime = ai_runtime.build_ai_runtime(
        SimpleNamespace(root_dir=tmp_path, secret_store=secrets), application=application,
    )

    manager = runtime.mcp_connection_manager
    assert manager.connected_server_ids == ("calendar-server",)
    assert manager.capability_ids == ("calendar.search",)
    assert application.state.ai_mcp_connection_manager is manager
    request = _turn_request()
    request["capability_policy"]["allowed"].append("calendar.search")
    registered = runtime._registry.list()
    default_manifest = runtime._manifest_resolver.resolve(request, registered)
    assert "calendar.search" not in default_manifest.capability_ids

    ProjectCapabilityProfileStore(tmp_path).update(
        "project-alpha", expected_revision=0,
        boundary_profile_id="project-boundary-project-alpha",
        boundary_profile_revision=1,
        enabled_sources=("core", "mcp"),
        enabled_mcp_server_ids=("calendar-server",),
        allowed_tool_ids=("calendar.search", "memory.recall"),
        tool_selection_bindings=tuple(
            ToolSelectionBinding(tool.tool_id, tool_contract_binding_identity(tool))
            for capability in registered
            for tool in (tool_from_capability(capability),)
            if tool.tool_id in {"calendar.search", "memory.recall"}
        ),
        mcp_server_selection_bindings=(MCPServerSelectionBinding(
            "calendar-server", "legacy_2025_11_25", 1,
            "calendar-endpoint", "calendar-user", 1,
        ),),
    )
    enabled_request = _turn_request()
    enabled_request["turn_id"] = "turn-fedcba9876543210fedcba9876543210"
    enabled_request["capability_policy"]["allowed"].append("calendar.search")
    enabled_manifest = runtime._manifest_resolver.resolve(enabled_request, registered)
    assert "calendar.search" in enabled_manifest.capability_ids
    capability = runtime._registry.get("calendar.search")
    assert capability is not None
    boundary = runtime._execution_boundary.evaluate(
        enabled_request, capability, {"arguments": {"query": "calendar"}},
    )
    assert boundary.outcome != "allow"

    shutdown_ai_mcp_runtime(application)
    shutdown_ai_mcp_runtime(application)
    assert manager.connected_server_ids == ()
    assert runtime._registry.resolve("calendar.search") is None


def test_invalid_mcp_authority_keeps_native_runtime_available(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / ".rebuild-data" / "security" / "mcp-approved-stdio.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema_version":"1.0.0","servers":"invalid"}', encoding="utf-8")
    _stub_native_dependencies(monkeypatch)

    runtime = ai_runtime.build_ai_runtime(SimpleNamespace(root_dir=tmp_path))

    assert runtime.mcp_connection_manager.connected_server_ids == ()
    assert runtime.mcp_connection_manager.statuses[0].error_code == "mcp.authority_invalid"
    assert runtime._registry.resolve("memory.recall") is not None


def test_production_manager_stateless_http_loopback_projects_headers_and_isolates_canaries(tmp_path: Path) -> None:
    with _loopback_mcp_server(header_mismatch=False) as server:
        authority_endpoint = f"https://mcp.example.test:{server.server_port}/rpc"
        _write_stateless_http_authority(tmp_path, authority_endpoint)
        registry = ScopedCapabilityRegistry()
        payloads = InMemoryTurnPayloadStore()
        manager = build_mcp_connection_manager(
            root_dir=tmp_path, registry=registry, turn_store=payloads,
            secret_store=InMemorySecretStore({"mcp:calendar-server:token": "Bearer authorization-canary"}),
            http_requester_factory=lambda: _LoopbackJsonRequester(server.server_port),
        )
        try:
            resolved = registry.resolve("calendar.search")
            assert resolved is not None, (manager.statuses, server.calls)
            definition, provider = resolved
            tool = definition.tool_definition
            assert tool is not None
            result = provider.invoke({
                "tool_call_id": "loopback-call", "turn_id": "loopback-turn", "operation_id": "loopback-operation",
                "arguments": {"query": "projected-secret-canary"}, "execution_context": _ExecutionControl(),
                "tool_contract": tool_contract_identity(tool),
            })
            receipt = payloads.get(result["receipt_ref"])
            assert manager.connected_server_ids == ("calendar-server",)
            assert receipt["status"] == "completed"
            assert [call["payload"]["method"] for call in server.calls] == ["server/discover", "tools/list", "tools/call"]
            for call in server.calls[:2]:
                assert "Mcp-Param-Query" not in call["headers"]
            call_headers = server.calls[-1]["headers"]
            assert call_headers["Mcp-Param-Query"] == "projected-secret-canary"
            assert call_headers["Authorization"] == "Bearer authorization-canary"
            for canary in ("authorization-canary", "projected-secret-canary", "remote-body-canary"):
                assert canary not in str(receipt)
                assert canary not in str(manager.statuses)
            assert authority_endpoint not in str(receipt)
            assert authority_endpoint not in str(manager.statuses)
        finally:
            manager.close_all()


def test_production_manager_stateless_http_header_mismatch_revokes_without_replay_or_receipt(tmp_path: Path) -> None:
    with _loopback_mcp_server(header_mismatch=True) as server:
        authority_endpoint = f"https://mcp.example.test:{server.server_port}/rpc"
        _write_stateless_http_authority(tmp_path, authority_endpoint)
        registry = ScopedCapabilityRegistry()
        payloads = InMemoryTurnPayloadStore()
        manager = build_mcp_connection_manager(
            root_dir=tmp_path, registry=registry, turn_store=payloads,
            secret_store=InMemorySecretStore({"mcp:calendar-server:token": "Bearer authorization-canary"}),
            http_requester_factory=lambda: _LoopbackJsonRequester(server.server_port),
        )
        try:
            resolved = registry.resolve("calendar.search")
            assert resolved is not None, (manager.statuses, server.calls)
            definition, provider = resolved
            tool = definition.tool_definition
            assert tool is not None
            request = {
                "tool_call_id": "mismatch-call", "turn_id": "mismatch-turn", "operation_id": "mismatch-operation",
                "arguments": {"query": "projected-secret-canary"}, "execution_context": _ExecutionControl(),
                "tool_contract": tool_contract_identity(tool),
            }
            with pytest.raises(ToolProviderFailure, match="mcp.transport_unconfirmed"):
                provider.invoke(request)
            with pytest.raises(ToolProviderFailure, match="connection_revoked"):
                provider.invoke(request)
            assert [call["payload"]["method"] for call in server.calls] == ["server/discover", "tools/list", "tools/call"]
            assert manager.connected_server_ids == ()
            assert registry.resolve("calendar.search") is None
            assert payloads._payloads == {}
            for canary in ("authorization-canary", "projected-secret-canary", "remote-body-canary"):
                assert canary not in str(manager.statuses)
            assert authority_endpoint not in str(manager.statuses)
        finally:
            manager.close_all()


def test_production_manager_secret_rotation_revokes_before_stateless_tool_post(tmp_path: Path) -> None:
    with _loopback_mcp_server(header_mismatch=False) as server:
        authority_endpoint = f"https://mcp.example.test:{server.server_port}/rpc"
        _write_stateless_http_authority(tmp_path, authority_endpoint)
        registry = ScopedCapabilityRegistry()
        payloads = InMemoryTurnPayloadStore()
        secrets = InMemorySecretStore({"mcp:calendar-server:token": "Bearer first-token"})
        manager = build_mcp_connection_manager(
            root_dir=tmp_path, registry=registry, turn_store=payloads, secret_store=secrets,
            http_requester_factory=lambda: _LoopbackJsonRequester(server.server_port),
        )
        try:
            definition, provider = registry.resolve("calendar.search")
            tool = definition.tool_definition
            assert tool is not None
            secrets.set("mcp:calendar-server:token", "Bearer rotated-token")
            request = {
                "tool_call_id": "rotated-call", "turn_id": "rotated-turn", "operation_id": "rotated-operation",
                "arguments": {"query": "calendar"}, "execution_context": _ExecutionControl(),
                "tool_contract": tool_contract_identity(tool),
            }

            with pytest.raises(ToolProviderFailure) as error:
                provider.invoke(request)

            assert error.value.error_code == "mcp.credential_generation_changed"
            assert error.value.effect_certainty == "confirmed_none"
            assert [call["payload"]["method"] for call in server.calls] == ["server/discover", "tools/list"]
            assert manager.connected_server_ids == ()
            assert registry.resolve("calendar.search") is None
            assert payloads._payloads == {}
            assert "first-token" not in str(manager.statuses)
            assert "rotated-token" not in str(manager.statuses)
        finally:
            manager.close_all()


def test_production_manager_legacy_http_secret_rotation_aborts_locally_without_cleanup_wire(tmp_path: Path) -> None:
    with _loopback_mcp_server(header_mismatch=False) as server:
        authority_endpoint = f"https://mcp.example.test:{server.server_port}/rpc"
        _write_http_authority(tmp_path, authority_endpoint)
        registry = ScopedCapabilityRegistry()
        payloads = InMemoryTurnPayloadStore()
        secrets = InMemorySecretStore({"mcp:calendar-server:token": "Bearer first-token"})
        manager = build_mcp_connection_manager(
            root_dir=tmp_path, registry=registry, turn_store=payloads, secret_store=secrets,
            http_requester_factory=lambda: _LoopbackJsonRequester(server.server_port),
        )
        try:
            resolved = registry.resolve("calendar.search")
            assert resolved is not None, (manager.statuses, server.calls)
            definition, provider = resolved
            tool = definition.tool_definition
            assert tool is not None
            secrets.delete("mcp:calendar-server:token")
            request = {
                "tool_call_id": "legacy-rotated-call", "turn_id": "legacy-rotated-turn",
                "operation_id": "legacy-rotated-operation", "arguments": {"query": "calendar"},
                "execution_context": _ExecutionControl(), "tool_contract": tool_contract_identity(tool),
            }

            with pytest.raises(ToolProviderFailure) as error:
                provider.invoke(request)

            assert error.value.error_code == "mcp.credential_generation_changed"
            assert error.value.effect_certainty == "confirmed_none"
            assert [call["payload"]["method"] for call in server.calls] == [
                "initialize", "notifications/initialized", "tools/list",
            ]
            assert manager.connected_server_ids == ()
            assert registry.resolve("calendar.search") is None
            assert payloads._payloads == {}
        finally:
            manager.close_all()


def test_missing_mcp_secret_fails_server_closed_without_breaking_native_runtime(
    tmp_path: Path, monkeypatch,
) -> None:
    child = tmp_path / "approved_mcp_child.py"
    child.write_text(_child_program(), encoding="utf-8")
    _write_authority(tmp_path, child)
    authority = tmp_path / ".rebuild-data" / "security" / "mcp-approved-stdio.json"
    payload = json.loads(authority.read_text(encoding="utf-8"))
    payload["servers"][0]["launch_manifest"]["secret_env_refs"] = {
        "MCP_TEST_SECRET": "mcp:calendar-server:token",
    }
    authority.write_text(json.dumps(payload), encoding="utf-8")
    _stub_native_dependencies(monkeypatch)

    runtime = ai_runtime.build_ai_runtime(
        SimpleNamespace(root_dir=tmp_path, secret_store=InMemorySecretStore()),
    )

    manager = runtime.mcp_connection_manager
    assert manager.connected_server_ids == ()
    assert manager.statuses == (
        type(manager.statuses[0])("calendar-server", "unavailable", "mcp.connection_failed"),
    )
    assert runtime._registry.resolve("calendar.search") is None
    assert runtime._registry.resolve("memory.recall") is not None
    assert "token" not in str(manager.statuses).lower()


def test_credentialed_mcp_rejects_legacy_secret_store_without_atomic_snapshot(tmp_path: Path) -> None:
    class _LegacySecretStore:
        def get(self, _key: str) -> str:  # pragma: no cover - capture must reject before legacy read
            raise AssertionError("credentialed MCP must require atomic snapshot support")

    with _loopback_mcp_server(header_mismatch=False) as server:
        authority_endpoint = f"https://mcp.example.test:{server.server_port}/rpc"
        _write_stateless_http_authority(tmp_path, authority_endpoint)
        registry = ScopedCapabilityRegistry()
        manager = build_mcp_connection_manager(
            root_dir=tmp_path, registry=registry, turn_store=InMemoryTurnPayloadStore(),
            secret_store=_LegacySecretStore(),  # type: ignore[arg-type]
            http_requester_factory=lambda: _LoopbackJsonRequester(server.server_port),
        )
        try:
            assert manager.connected_server_ids == ()
            assert registry.resolve("calendar.search") is None
            assert server.calls == []
        finally:
            manager.close_all()


def _stub_native_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(
        ai_runtime,
        "build_rebuild_object_store",
        lambda _root: (SimpleNamespace(namespace_id="default"), object()),
    )
    monkeypatch.setattr(ai_runtime, "build_library_search_service", lambda _root, _store: _Search())
    monkeypatch.setattr(
        ai_runtime,
        "resolve_model_gateway_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(
            gateway=None, egress_consented=False, adapter_kind="unavailable",
        ),
    )


def _turn_request() -> dict[str, object]:
    return json.loads((
        ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json"
    ).read_text(encoding="utf-8"))


def _write_authority(root: Path, child: Path) -> None:
    payload = {
        "schema_version": "1.0.0",
        "servers": [{
            "server_id": "calendar-server", "enabled": True,
            "approval_status": "approved", "approval_revision": 1,
            "host_connection": {
                "server_id": "calendar-server", "manifest_revision": 1,
                "endpoint_identity": "calendar-endpoint", "credential_subject_id": "calendar-user",
                "transport_generation": 1, "catalog_revision": 1,
            },
            "launch_manifest": {
                "server_id": "calendar-server", "manifest_revision": 1,
                "endpoint_identity": "calendar-endpoint", "credential_subject_id": "calendar-user",
                "transport_generation": 1, "approval_revision": 1,
                "approval_status": "approved", "executable": str(Path(sys.executable).resolve()),
                "argv": ["-u", str(child.resolve())], "cwd": None,
                "environment": {"NO_COLOR": "1"},
                "secret_env_refs": {},
            },
            "tool_policies": [{
                "tool_name": "calendar.search", "tool_id": "calendar.search", "version": 1,
                "display_name": "Search calendar", "description": "Reviewed calendar search",
                "effect": "read", "data_classes": ["calendar_event"],
                "input_schema_uri": "crp://schemas/calendar-search-input-v1",
                "output_schema_uri": "crp://schemas/calendar-search-output-v1",
                "receipt_schema_uri": None, "operation_semantics": "read_only",
                "execution_mode": "parallel", "resource_locks": ["mcp:calendar"],
                "idempotency": "never_retry",
                "retry_policy": {"max_attempts": 1, "backoff_ms": 0, "retryable_error_codes": []},
                "verification_tool_id": None, "compensation_tool_id": None,
                "mutability": "read_only", "egress_class": "remote",
                "network_scope": ["mcp:calendar"], "data_egress_scope": ["calendar_event"],
                "timeout_ms": 10000, "required_scopes": ["calendar.read"],
                "boundary_requirements": ["mcp_enabled"], "requires_approval": False,
                "tool_schema_revision": 1, "reviewed_input_schema": {"type": "object"},
                "reviewed_output_schema": None, "available": True,
                "remote_receipt_field": None, "reviewed_receipt_schema": None,
            }],
        }],
    }
    path = root / ".rebuild-data" / "security" / "mcp-approved-stdio.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_stateless_authority(root: Path, child: Path, pid_path: Path) -> None:
    _write_authority(root, child)
    legacy_path = root / ".rebuild-data" / "security" / "mcp-approved-stdio.json"
    payload = json.loads(legacy_path.read_text(encoding="utf-8"))
    record = payload["servers"][0]
    launch = record.pop("launch_manifest")
    record["transport_kind"] = "stdio"
    record["protocol_profile"] = "stateless_2026_07_28"
    record["host_connection"]["protocol_profile"] = "stateless_2026_07_28"
    launch["protocol_profile"] = "stateless_2026_07_28"
    launch["argv"].append(str(pid_path.resolve()))
    record["connection_manifest"] = launch
    payload["schema_version"] = "1.2.0"
    unified = root / ".rebuild-data" / "security" / "mcp-approved-servers.json"
    unified.write_text(json.dumps(payload), encoding="utf-8")
    legacy_path.unlink()


def _write_http_authority(root: Path, endpoint: str = "https://mcp.example.test/rpc") -> None:
    child = root / "unused.py"
    child.write_text("", encoding="utf-8")
    _write_authority(root, child)
    legacy_path = root / ".rebuild-data" / "security" / "mcp-approved-stdio.json"
    payload = json.loads(legacy_path.read_text(encoding="utf-8"))
    record = payload["servers"][0]
    launch = record.pop("launch_manifest")
    record["transport_kind"] = "streamable_http"
    record["connection_manifest"] = {
        "server_id": launch["server_id"], "manifest_revision": launch["manifest_revision"],
        "endpoint_identity": launch["endpoint_identity"], "credential_subject_id": launch["credential_subject_id"],
        "transport_generation": launch["transport_generation"], "approval_revision": launch["approval_revision"],
        "approval_status": "approved", "endpoint_url": endpoint,
        "headers": {"X-Client": "chriptmas"},
        "secret_header_refs": {"Authorization": "mcp:calendar-server:token"},
        "timeout_seconds": 20, "max_response_bytes": 4194304, "max_sse_events": 256,
    }
    payload["schema_version"] = "1.1.0"
    unified = root / ".rebuild-data" / "security" / "mcp-approved-servers.json"
    unified.write_text(json.dumps(payload), encoding="utf-8")
    legacy_path.unlink()


def _write_stateless_http_authority(root: Path, endpoint: str) -> None:
    _write_http_authority(root)
    path = root / ".rebuild-data" / "security" / "mcp-approved-servers.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    record = payload["servers"][0]
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string", "x-mcp-header": "Query"}},
    }
    record["protocol_profile"] = "stateless_2026_07_28"
    record["host_connection"]["protocol_profile"] = "stateless_2026_07_28"
    record["connection_manifest"]["protocol_profile"] = "stateless_2026_07_28"
    # The reviewed authority remains HTTPS.  The injected test requester alone
    # terminates this source-side fixture on its real loopback HTTP socket.
    record["connection_manifest"]["endpoint_url"] = endpoint
    record["tool_policies"][0]["reviewed_input_schema"] = schema
    payload["schema_version"] = "1.2.0"
    path.write_text(json.dumps(payload), encoding="utf-8")


class _LoopbackJsonRequester:
    """Test-only real socket requester; never a production network adapter."""

    def __init__(self, server_port: int) -> None:
        self._server_port = server_port

    def request(self, method, url, *, headers, body, timeout_seconds, max_response_bytes, control_check=None):
        if control_check is not None:
            control_check()
        parsed = urlsplit(url)
        assert parsed.scheme == "https" and parsed.hostname == "mcp.example.test"
        connection = HTTPConnection("127.0.0.1", self._server_port, timeout=timeout_seconds)
        try:
            connection.request(method, parsed.path or "/", body=body, headers=dict(headers))
            response = connection.getresponse()
            payload = response.read(max_response_bytes + 1)
            if len(payload) > max_response_bytes:
                raise OSError("loopback response exceeded bound")
            return MCPHttpResponse(response.status, dict(response.getheaders()), payload)
        finally:
            connection.close()


@contextmanager
def _loopback_mcp_server(*, header_mismatch: bool):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
            self.server.calls.append({"headers": dict(self.headers.items()), "payload": payload})
            method = payload["method"]
            if method == "initialize":
                result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}
                status = 200
            elif method == "notifications/initialized":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            elif method == "server/discover":
                result = {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 1000, "cacheScope": "private", "resultType": "complete"}
                status = 200
            elif method == "tools/list":
                schema = (
                    {"type": "object", "properties": {"query": {"type": "string", "x-mcp-header": "Query"}}}
                    if self.headers.get("MCP-Protocol-Version") == "2026-07-28"
                    else {"type": "object"}
                )
                result = {"tools": [{"name": "calendar.search", "description": "untrusted", "inputSchema": schema}], "ttlMs": 1000, "cacheScope": "private", "resultType": "complete"}
                status = 200
            elif header_mismatch:
                result = None
                status = 400
            else:
                result = {"content": [{"type": "text", "text": "remote-body-canary"}], "resultType": "complete"}
                status = 200
            envelope = (
                {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32020, "message": "HeaderMismatch"}}
                if result is None else {"jsonrpc": "2.0", "id": payload["id"], "result": result}
            )
            encoded = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if method == "initialize":
                self.send_header("MCP-Session-Id", "loopback-session")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.calls = []
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
        assert not thread.is_alive()


def _child_program() -> str:
    return r'''
import json, os, sys
if "MCP_TEST_SECRET" in os.environ:
    raise SystemExit(12)
for raw in sys.stdin.buffer:
    request = json.loads(raw.decode("utf-8"))
    method = request.get("method")
    if method == "notifications/initialized":
        continue
    request_id = request.get("id")
    if method == "initialize":
        result = {"protocolVersion":"2025-11-25","capabilities":{"tools":{}}}
    elif method == "tools/list":
        result = {"tools":[{"name":"calendar.search","description":"untrusted","inputSchema":{"type":"object"}}]}
    else:
        result = {"content":[{"type":"text","text":"private remote body"}]}
    response = {"jsonrpc":"2.0","id":request_id,"result":result}
    sys.stdout.buffer.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()
'''


def _stateless_child_program() -> str:
    return r'''
import json, os, sys
if "MCP_TEST_SECRET" in os.environ:
    raise SystemExit(12)
with open(sys.argv[1], "w", encoding="utf-8") as stream:
    stream.write(str(os.getpid()))
expected_meta = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "chriptmas-os", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}
for raw in sys.stdin.buffer:
    request = json.loads(raw.decode("utf-8"))
    method = request.get("method")
    if method in {"initialize", "notifications/initialized", "ping"}:
        raise SystemExit(31)
    if request.get("params", {}).get("_meta") != expected_meta:
        raise SystemExit(32)
    request_id = request.get("id")
    if method == "server/discover":
        result = {"supportedVersions":["2026-07-28"],"capabilities":{"tools":{}},"ttlMs":1000,"cacheScope":"private","resultType":"complete"}
    elif method == "tools/list":
        result = {"tools":[{"name":"calendar.search","description":"untrusted","inputSchema":{"type":"object"}}],"ttlMs":1000,"cacheScope":"private","resultType":"complete"}
    elif method == "tools/call":
        result = {"content":[{"type":"text","text":"private remote body"}],"resultType":"complete"}
    else:
        raise SystemExit(33)
    response = {"jsonrpc":"2.0","id":request_id,"result":result}
    sys.stdout.buffer.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()
'''
