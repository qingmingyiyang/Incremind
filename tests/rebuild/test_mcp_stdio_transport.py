from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

import psutil
import pytest

from core.ai_kernel import ScopedCapabilityRegistry, ToolDispatchCancelled, ToolProviderFailure
from core.ai_tooling import ToolRetryPolicy, tool_contract_identity
from core.mcp_host.stdio_config import (
    MCPStdioConfigError,
    MCPStdioConnectionConfig,
    MCPStdioLaunchAuthority,
    MCPStdioLaunchManifest,
)
from core.mcp_host import (
    MCPCredentialGenerationChanged,
    MCPHostConnection,
    MCPHostConnectionConfig,
    MCPToolPolicy,
)
from core.mcp_host.stdio_transport import MCPStdioTransport, MCPStdioTransportError


class _Secrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def resolve(self, secret_ref: str) -> str:
        return self._values[secret_ref]


class _Receipts:
    def write_metadata(self, receipt):
        return f"crp://default/mcp-receipts/{receipt['receipt_id']}"


class _ManifestStore:
    def __init__(self, manifest: MCPStdioLaunchManifest | None) -> None:
        self._manifest = manifest

    def get_approved(self, server_id: str) -> MCPStdioLaunchManifest | None:
        if self._manifest is not None and self._manifest.server_id == server_id:
            return self._manifest
        return None


def _authority(manifest: MCPStdioLaunchManifest) -> MCPStdioLaunchAuthority:
    return MCPStdioLaunchAuthority(_ManifestStore(manifest))


class _Cancel:
    remaining_timeout_ms = 5_000

    def __init__(self) -> None:
        self._started = time.monotonic()

    def checkpoint(self) -> None:
        if time.monotonic() - self._started > 0.12:
            raise ToolDispatchCancelled(provider_started=True)


def _host_config(*, protocol_profile: str = "legacy_2025_11_25") -> MCPHostConnectionConfig:
    return MCPHostConnectionConfig(
        server_id="stdio-test-server",
        manifest_revision=1,
        endpoint_identity="stdio-endpoint-1",
        credential_subject_id="stdio-credential-1",
        transport_generation=1,
        catalog_revision=1,
        protocol_profile=protocol_profile,
    )


def _manifest(mode: str, tmp_path: Path, *, secret: bool = False, extra_argv: tuple[str, ...] = ()) -> MCPStdioLaunchManifest:
    child = tmp_path / "mcp_stdio_child.py"
    child.write_text(_child_program(), encoding="utf-8")
    host = _host_config()
    return MCPStdioLaunchManifest(
        server_id=host.server_id,
        manifest_revision=host.manifest_revision,
        endpoint_identity=host.endpoint_identity,
        credential_subject_id=host.credential_subject_id,
        transport_generation=host.transport_generation,
        approval_revision=1,
        approval_status="approved",
        executable=str(Path(sys.executable).resolve(strict=True)),
        argv=("-u", str(child.resolve(strict=True)), mode, *extra_argv),
        secret_env_refs={"MCP_TEST_SECRET": "secret-ref-1"} if secret else None,
    )


def _config(mode: str, tmp_path: Path, *, secret: bool = False, extra_argv: tuple[str, ...] = ()) -> tuple[MCPStdioConnectionConfig, MCPHostConnectionConfig]:
    host = _host_config()
    return _authority(
        _manifest(mode, tmp_path, secret=secret, extra_argv=extra_argv),
    ).resolve(host), host


def _transport(mode: str, tmp_path: Path, *, secret: bool = False, max_frame_bytes: int = 64 * 1024) -> MCPStdioTransport:
    config, host = _config(mode, tmp_path, secret=secret)
    return MCPStdioTransport(
        config,
        host_connection=host,
        startup_timeout_ms=1_000,
        max_frame_bytes=max_frame_bytes,
    )


def _invocation_envelope() -> dict[str, object]:
    return {
        "turn_id": "turn-mcp-1",
        "invocation_id": "tool-call-mcp-1",
        "operation_id": "operation-mcp-1",
        "idempotency_key": "operation-mcp-1:tool-call-mcp-1",
        "attempt": 1,
    }


def test_stdio_stateless_profile_uses_discover_without_initialize_or_ping(tmp_path: Path) -> None:
    host = _host_config(protocol_profile="stateless_2026_07_28")
    manifest = replace(_manifest("stateless", tmp_path), protocol_profile="stateless_2026_07_28")
    config = _authority(manifest).resolve(host)
    transport = MCPStdioTransport(config, host_connection=host, startup_timeout_ms=1_000)
    try:
        assert transport.server_discover() == {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 1000, "cacheScope": "private"}
        assert transport.list_tools() == {"tools": [], "ttlMs": 1000, "cacheScope": "private"}
        assert transport.call_tool(
            "test.tool", {}, timeout_ms=1_000, execution_control=_CancelNever(),
            parameter_headers={"Mcp-Param-Only-For-Http": "ignored"},
        )
        with pytest.raises(MCPStdioTransportError, match="unavailable"):
            transport.ping(timeout_ms=1_000)
    finally:
        transport.close()


def test_stdio_tool_invocation_envelope_is_host_metadata_and_preserves_model_arguments(tmp_path: Path) -> None:
    host = _host_config(protocol_profile="stateless_2026_07_28")
    manifest = replace(_manifest("stateless-envelope", tmp_path), protocol_profile="stateless_2026_07_28")
    config = _authority(manifest).resolve(host)
    transport = MCPStdioTransport(config, host_connection=host, startup_timeout_ms=1_000)
    arguments = {"_meta": {"untrusted": "model-value"}, "value": "preserve"}
    try:
        transport.server_discover()
        assert transport.call_tool(
            "test.tool", arguments, timeout_ms=1_000, execution_control=_CancelNever(),
            invocation_envelope=_invocation_envelope(),
        )
        assert transport.continue_tool(
            "test.tool", arguments, b"opaque-state", timeout_ms=1_000,
            execution_control=_CancelNever(), invocation_envelope=_invocation_envelope(),
        )
    finally:
        transport.close()


def test_stdio_invalid_invocation_envelope_fails_closed_before_tool_wire(tmp_path: Path) -> None:
    host = _host_config(protocol_profile="stateless_2026_07_28")
    manifest = replace(_manifest("stateless", tmp_path), protocol_profile="stateless_2026_07_28")
    config = _authority(manifest).resolve(host)
    transport = MCPStdioTransport(config, host_connection=host, startup_timeout_ms=1_000)
    try:
        transport.server_discover()
        with pytest.raises(MCPStdioTransportError, match="invocation envelope"):
            transport.call_tool(
                "test.tool", {}, timeout_ms=1_000, execution_control=_CancelNever(),
                invocation_envelope={"turn_id": "turn-mcp-1"},
            )
        assert transport.failed_closed is True
    finally:
        transport.close()


def _initialize(transport: MCPStdioTransport) -> None:
    result = transport.initialize({"protocolVersion": "2025-11-25", "capabilities": {}})
    assert result == {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}
    transport.notify_initialized()


def test_stdio_transport_rejects_legacy_secret_environment_before_spawn(tmp_path: Path) -> None:
    transport = _transport("normal", tmp_path, secret=True)
    try:
        with pytest.raises(MCPStdioTransportError, match="could not start"):
            _initialize(transport)
    finally:
        transport.close()


def test_stdio_transport_rechecks_generation_at_tool_frame_boundary(tmp_path: Path) -> None:
    config, host = _config("normal", tmp_path)
    checks = iter((True, True, True, True, False))
    transport = MCPStdioTransport(
        config, host_connection=host,
        credential_generation_current=lambda: next(checks), startup_timeout_ms=1_000,
    )
    try:
        _initialize(transport)
        assert transport.list_tools() == {"tools": []}
        with pytest.raises(MCPCredentialGenerationChanged):
            transport.call_tool("test.tool", {}, timeout_ms=1_000, execution_control=_CancelNever())
        assert transport.failed_closed is True
    finally:
        transport.close()


@pytest.mark.parametrize("mode", [
    "wrong-id", "bool-id", "server-request", "malformed", "eof", "non-utf8", "oversized",
    "extra-response",
])
def test_stdio_transport_fails_closed_for_untrusted_frames(mode: str, tmp_path: Path) -> None:
    transport = _transport(mode, tmp_path, max_frame_bytes=256)
    try:
        with pytest.raises(MCPStdioTransportError) as error:
            transport.initialize({"protocolVersion": "2025-11-25", "capabilities": {}})
        assert "-c" not in str(error.value)
        assert "secret" not in str(error.value).lower()
        with pytest.raises(MCPStdioTransportError, match="closed"):
            transport.list_tools()
    finally:
        transport.close()


def test_stdio_transport_discards_unbounded_stderr_without_deadlock(tmp_path: Path) -> None:
    transport = _transport("stderr-flood", tmp_path)
    try:
        _initialize(transport)
        assert transport.list_tools() == {"tools": []}
    finally:
        transport.close()


def test_stdio_transport_limits_server_notifications(tmp_path: Path) -> None:
    transport = _transport("notifications", tmp_path)
    try:
        with pytest.raises(MCPStdioTransportError, match="notification limit"):
            transport.initialize({"protocolVersion": "2025-11-25", "capabilities": {}})
    finally:
        transport.close()


def test_stdio_transport_enforces_initialize_lifecycle(tmp_path: Path) -> None:
    before_initialize = _transport("normal", tmp_path)
    with pytest.raises(MCPStdioTransportError, match="lifecycle"):
        before_initialize.list_tools()

    duplicate_initialize = _transport("normal", tmp_path)
    try:
        _initialize(duplicate_initialize)
        with pytest.raises(MCPStdioTransportError, match="lifecycle"):
            duplicate_initialize.initialize({"protocolVersion": "2025-11-25", "capabilities": {}})
    finally:
        duplicate_initialize.close()


def test_stdio_transport_timeout_and_control_cancellation_reap_process(tmp_path: Path) -> None:
    timeout_transport = _transport("slow", tmp_path)
    try:
        with pytest.raises(MCPStdioTransportError, match="timed out"):
            timeout_transport.initialize({"protocolVersion": "2025-11-25", "capabilities": {}})
    finally:
        timeout_transport.close()

    cancelled = _transport("slow-call", tmp_path)
    try:
        _initialize(cancelled)
        with pytest.raises(ToolDispatchCancelled):
            cancelled.call_tool("test.tool", {}, timeout_ms=1_000, execution_control=_Cancel())
        with pytest.raises(MCPStdioTransportError, match="closed"):
            cancelled.list_tools()
    finally:
        cancelled.close()

    ping_timeout = _transport("slow-ping", tmp_path)
    try:
        _initialize(ping_timeout)
        with pytest.raises(MCPStdioTransportError, match="timed out"):
            ping_timeout.ping(timeout_ms=100)
        assert ping_timeout.failed_closed is True
    finally:
        ping_timeout.close()


def test_stdio_ping_waits_for_inflight_tool_without_frame_interleaving(tmp_path: Path) -> None:
    transport = _transport("slow-call", tmp_path)
    tool_done = Event()
    ping_done = Event()
    errors: list[Exception] = []
    try:
        _initialize(transport)

        def call_tool() -> None:
            try:
                transport.call_tool(
                    "test.tool", {}, timeout_ms=3_000, execution_control=_CancelNever(),
                )
            except Exception as error:
                errors.append(error)
            finally:
                tool_done.set()

        def ping() -> None:
            try:
                transport.ping(timeout_ms=1_000)
            except Exception as error:
                errors.append(error)
            finally:
                ping_done.set()

        tool_thread = Thread(target=call_tool)
        ping_thread = Thread(target=ping)
        tool_thread.start()
        time.sleep(0.1)
        ping_thread.start()
        assert not ping_done.wait(timeout=0.1)
        tool_thread.join(timeout=3)
        ping_thread.join(timeout=2)

        assert tool_done.is_set() and ping_done.is_set()
        assert errors == []
    finally:
        transport.close()


def test_fatal_stdio_failure_revokes_host_connection_and_registry_lease(tmp_path: Path) -> None:
    config, host_config = _config("host-eof", tmp_path)
    transport = MCPStdioTransport(
        config, host_connection=host_config, startup_timeout_ms=1_000,
    )
    registry = ScopedCapabilityRegistry()
    policy = MCPToolPolicy(
        tool_name="calendar.search", tool_id="calendar.search", version=1,
        display_name="Search calendar", description="Reviewed local policy", effect="read",
        data_classes=("calendar_event",),
        input_schema_uri="crp://schemas/calendar-search-input-v1",
        output_schema_uri="crp://schemas/calendar-search-output-v1", receipt_schema_uri=None,
        operation_semantics="read_only", execution_mode="parallel",
        resource_locks=("mcp:calendar",), idempotency="never_retry",
        retry_policy=ToolRetryPolicy(1, 0, ()), verification_tool_id=None,
        compensation_tool_id=None, mutability="read_only", egress_class="remote",
        network_scope=("mcp:calendar",), data_egress_scope=("calendar_event",),
        timeout_ms=1_000, required_scopes=("calendar.read",),
        boundary_requirements=("mcp_enabled",), requires_approval=False,
        tool_schema_revision=1, reviewed_input_schema={"type": "object"},
        reviewed_output_schema=None,
    )
    connection = MCPHostConnection(
        config=host_config, transport=transport, registry=registry,
        policies=(policy,), receipt_store=_Receipts(),
    )
    definition = connection.connect()[0]
    resolved = registry.resolve("calendar.search")
    assert resolved is not None
    provider = resolved[1]
    request = {
        "tool_call_id": "fatal-call", "turn_id": "fatal-turn", "operation_id": "fatal-operation",
        "arguments": {}, "execution_context": _CancelNever(),
        "tool_contract": tool_contract_identity(definition.tool_definition),
    }

    with pytest.raises(ToolProviderFailure) as error:
        provider.invoke(request)

    assert error.value.error_code == "mcp.transport_unconfirmed"
    assert error.value.effect_certainty == "unknown"
    assert connection.connected is False
    assert registry.resolve("calendar.search") is None
    with pytest.raises(ToolProviderFailure) as revoked:
        provider.invoke(request)
    assert revoked.value.error_code == "mcp.connection_revoked"
    assert revoked.value.effect_certainty == "confirmed_none"


def test_stdio_close_reaps_the_real_child_process_tree(tmp_path: Path) -> None:
    pid_file = tmp_path / "descendant.pid"
    config, host = _config("spawn-child", tmp_path, extra_argv=(str(pid_file),))
    transport = MCPStdioTransport(
        config, host_connection=host, startup_timeout_ms=1_000,
    )
    _initialize(transport)
    descendant_pid = int(pid_file.read_text(encoding="utf-8"))
    assert psutil.pid_exists(descendant_pid)

    transport.close()

    deadline = time.monotonic() + 2
    while psutil.pid_exists(descendant_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not psutil.pid_exists(descendant_pid)


def test_stdio_close_is_idempotent_and_config_rejects_unsafe_values_without_leaks(tmp_path: Path) -> None:
    transport = _transport("normal", tmp_path)
    _initialize(transport)
    transport.close()
    transport.close()
    with pytest.raises(MCPStdioTransportError, match="closed"):
        transport.list_tools()

    manifest = _manifest("normal", tmp_path)
    host = _host_config()
    with pytest.raises(MCPStdioConfigError) as argv_error:
        replace(manifest, argv=("bad\narg",))
    assert "bad" not in str(argv_error.value)
    with pytest.raises(MCPStdioConfigError):
        replace(manifest, executable="relative-command", argv=("run",))
    with pytest.raises(MCPStdioConfigError):
        replace(
            manifest,
            environment={"API_KEY": "constant"}, secret_env_refs={"API_KEY": "secret-ref"},
        )
    if sys.platform == "win32":
        with pytest.raises(MCPStdioConfigError, match="conflict"):
            replace(
                manifest,
                environment={"NO_COLOR": "1"},
                secret_env_refs={"no_color": "secret-ref"},
            )
    with pytest.raises(MCPStdioConfigError, match="allowlisted"):
        replace(manifest, environment={"FOO": "actual-secret-that-must-not-persist"})
    empty = _authority(replace(manifest, argv=())).resolve(host)
    assert empty.argv == ()
    missing = _authority(replace(
        manifest, cwd=str(tmp_path.resolve()), secret_env_refs={"MCP_SECRET": "missing-ref"},
    )).resolve(host)
    with pytest.raises(MCPStdioConfigError) as secret_error:
        missing.build_environment()
    assert "missing-ref" not in str(secret_error.value)
    with pytest.raises(MCPStdioConfigError, match="does not match"):
        _authority(manifest).resolve(replace(host, transport_generation=2))
    with pytest.raises(MCPStdioConfigError, match="not approved"):
        replace(manifest, approval_status="pending")
    with pytest.raises(MCPStdioConfigError, match="authority"):
        MCPStdioConnectionConfig(_approval=object(), manifest=manifest)
    with pytest.raises(MCPStdioConfigError, match="authority"):
        MCPStdioLaunchAuthority((manifest,))


def test_launch_authority_fails_closed_for_unavailable_or_invalid_store(tmp_path: Path) -> None:
    host = _host_config()

    class _FailingStore:
        def get_approved(self, _server_id):
            raise RuntimeError("private storage detail")

    class _InvalidStore:
        def get_approved(self, _server_id):
            return {"approval_status": "approved"}

    with pytest.raises(MCPStdioConfigError, match="unavailable") as unavailable:
        MCPStdioLaunchAuthority(_FailingStore()).resolve(host)
    assert "private storage detail" not in str(unavailable.value)
    with pytest.raises(MCPStdioConfigError, match="manifest is unavailable"):
        MCPStdioLaunchAuthority(_ManifestStore(None)).resolve(host)
    with pytest.raises(MCPStdioConfigError, match="invalid record"):
        MCPStdioLaunchAuthority(_InvalidStore()).resolve(host)


class _CancelNever:
    remaining_timeout_ms = 10_000

    def checkpoint(self) -> None:
        return None


def _child_program() -> str:
    return r'''
import json, os, subprocess, sys, time
mode = sys.argv[1]
if mode == "spawn-child":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    with open(sys.argv[2], "w", encoding="utf-8") as stream:
        stream.write(str(child.pid))
for raw in sys.stdin.buffer:
    request = json.loads(raw.decode("utf-8"))
    if request.get("method") == "notifications/initialized":
        continue
    request_id = request.get("id")
    if mode == "eof":
        raise SystemExit(0)
    if mode == "slow" or (mode == "slow-call" and request.get("method") == "tools/call") or (mode == "slow-ping" and request.get("method") == "ping"):
        time.sleep(2)
    if mode == "malformed":
        sys.stdout.buffer.write(b"not-json\n"); sys.stdout.buffer.flush(); continue
    if mode == "non-utf8":
        sys.stdout.buffer.write(b"\xff\n"); sys.stdout.buffer.flush(); continue
    if mode == "oversized":
        sys.stdout.buffer.write(b"x" * 300 + b"\n"); sys.stdout.buffer.flush(); continue
    if mode == "server-request":
        response = {"jsonrpc":"2.0","id":9,"method":"server/request","params":{}}
    elif mode == "wrong-id":
        response = {"jsonrpc":"2.0","id":999,"result":{}}
    elif mode == "bool-id":
        response = {"jsonrpc":"2.0","id":True,"result":{}}
    elif request.get("method") == "initialize":
        response = {"jsonrpc":"2.0","id":request_id,"result":{"protocolVersion":"2025-11-25","capabilities":{"tools":{}}}}
    elif request.get("method") == "server/discover":
        if mode in {"stateless", "stateless-envelope"} and request["params"].get("_meta") != {"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"chriptmas-os","version":"1"},"io.modelcontextprotocol/clientCapabilities":{}}:
            raise SystemExit(31)
        response = {"jsonrpc":"2.0","id":request_id,"result":{"supportedVersions":["2026-07-28"],"capabilities":{"tools":{}},"ttlMs":1000,"cacheScope":"private","resultType":"complete"}}
    elif request.get("method") == "tools/list":
        if mode == "stderr-flood":
            sys.stderr.write("x" * 2_000_000); sys.stderr.flush()
        tools = []
        if mode == "host-eof":
            tools = [{"name":"calendar.search","description":"untrusted","inputSchema":{"type":"object"}}]
        result = {"tools":tools}
        if mode in {"stateless", "stateless-envelope"}:
            if request["params"].get("_meta") != {"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"chriptmas-os","version":"1"},"io.modelcontextprotocol/clientCapabilities":{}}:
                raise SystemExit(32)
            result["resultType"] = "complete"
            result["ttlMs"] = 1000
            result["cacheScope"] = "private"
        response = {"jsonrpc":"2.0","id":request_id,"result":result}
    elif request.get("method") == "tools/call":
        if mode == "host-eof":
            raise SystemExit(0)
        if mode == "stateless-envelope":
            expected = {"turn_id":"turn-mcp-1","invocation_id":"tool-call-mcp-1","operation_id":"operation-mcp-1","idempotency_key":"operation-mcp-1:tool-call-mcp-1","attempt":1}
            expected_meta = {"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"chriptmas-os","version":"1"},"io.modelcontextprotocol/clientCapabilities":{},"io.chriptmas/invocation":expected}
            if request["params"].get("_meta") != expected_meta:
                raise SystemExit(34)
            if request["params"].get("arguments") != {"_meta":{"untrusted":"model-value"},"value":"preserve"}:
                raise SystemExit(35)
            if request["params"].get("requestState") not in {None, "opaque-state"}:
                raise SystemExit(36)
        result = {"content":[{"type":"text","text":"secret-present" if os.environ.get("MCP_TEST_SECRET") else "missing-secret"}]}
        if mode == "stateless":
            if request["params"].get("_meta") != {"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"chriptmas-os","version":"1"},"io.modelcontextprotocol/clientCapabilities":{}}:
                raise SystemExit(33)
        if mode in {"stateless", "stateless-envelope"}:
            result["resultType"] = "complete"
        response = {"jsonrpc":"2.0","id":request_id,"result":result}
    else:
        response = {"jsonrpc":"2.0","id":request_id,"result":{}}
    if mode == "extra-response":
        response["debug"] = "untrusted"
    if mode == "notifications":
        for _ in range(33):
            sys.stdout.buffer.write(b'{"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n')
    sys.stdout.buffer.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()
'''
