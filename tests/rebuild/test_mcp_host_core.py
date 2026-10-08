from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import json
from pathlib import Path
from threading import Event, Thread

import pytest

from core.ai_kernel import (
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    ScopedCapabilityRegistry,
    SynchronousAIRuntime,
    ToolProviderFailure,
    ToolDispatchDeadlineExceeded,
)
from core.mcp_host import MCPInputRequiredError
from core.ai_tooling import ToolRetryPolicy, tool_contract_identity
from core.mcp_host import (
    MCPHttpResponse,
    MCPHostConnection,
    MCPHostConnectionConfig,
    MCPHostError,
    MCPHostLimits,
    MCPToolPolicy,
    MCPStreamableHTTPAuthority,
    MCPStreamableHTTPManifest,
    MCPStreamableHTTPTransport,
    TurnPayloadMCPReceiptStore,
)


ROOT = Path(__file__).resolve().parents[2]


class _Transport:
    def __init__(self, pages: list[Mapping[str, object]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, object]] = []
        self.initialized = False
        self.result: Mapping[str, object] = {"content": [{"type": "text", "text": "private remote result"}]}
        self.call_error: Exception | None = None

    def initialize(self, request):
        self.calls.append(("initialize", dict(request)))
        return {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}

    def notify_initialized(self):
        self.calls.append(("initialized", None))
        self.initialized = True

    def list_tools(self, cursor=None):
        self.calls.append(("list_tools", cursor))
        assert self.initialized
        return self.pages.pop(0)

    def call_tool(self, name, arguments, *, timeout_ms, execution_control, parameter_headers=None, invocation_envelope=None):
        self.calls.append(("call_tool", (name, dict(arguments), timeout_ms, execution_control, dict(parameter_headers or {}), dict(invocation_envelope or {}))))
        if self.call_error is not None:
            raise self.call_error
        return self.result

    def ping(self, *, timeout_ms):
        self.calls.append(("ping", timeout_ms))

    def close(self):
        self.calls.append(("close", None))


class _Receipts:
    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []
        self.intents: dict[tuple[str, str], dict[str, object]] = {}
        self.replay_claims: dict[tuple[str, str], dict[str, object]] = {}
        self.probe_claims: dict[tuple[str, str], dict[str, object]] = {}
        self.unknowns: list[tuple[dict[str, object], str]] = []
        self.write_error: Exception | None = None

    def write_metadata(self, receipt):
        if self.write_error is not None:
            raise self.write_error
        self.items.append(dict(receipt))
        return f"crp://default/mcp-receipts/{receipt['receipt_id']}"

    def completed_invocation(self, *, turn_id, invocation_id):
        for receipt in self.items:
            if receipt.get("turn_id") == turn_id and receipt.get("invocation_id") == invocation_id:
                return f"crp://default/mcp-receipts/{receipt['receipt_id']}", dict(receipt)
        return None

    def reserve_side_effect(self, intent):
        key = (intent["turn_id"], intent["invocation_id"])
        existing = self.intents.get(key)
        if existing is not None:
            if existing != intent:
                raise ValueError("side-effect intent conflict")
            return False
        self.intents[key] = dict(intent)
        return True

    def side_effect_reserved(self, intent):
        key = (intent["turn_id"], intent["invocation_id"])
        existing = self.intents.get(key)
        if existing is None:
            return False
        if existing != intent:
            raise ValueError("side-effect intent conflict")
        return True

    def mark_side_effect_unknown(self, intent, *, error_code):
        self.unknowns.append((dict(intent), error_code))

    def reserve_verified_none_replay(self, claim):
        key = (claim["turn_id"], claim["invocation_id"])
        existing = self.replay_claims.get(key)
        if existing is not None:
            if existing != claim:
                raise ValueError("verified-none replay claim conflict")
            return False
        self.replay_claims[key] = dict(claim)
        return True

    def record_verified_none_probe(self, claim):
        key = (claim["turn_id"], claim["invocation_id"])
        existing = self.probe_claims.get(key)
        if existing is not None and existing != claim:
            raise ValueError("verified-none probe claim conflict")
        self.probe_claims[key] = dict(claim)
        return f"crp://default/mcp-probes/{claim['invocation_id']}"


def _policy(name: str = "calendar.search", tool_id: str = "calendar.search") -> MCPToolPolicy:
    return MCPToolPolicy(
        tool_name=name,
        tool_id=tool_id,
        version=1,
        display_name="Search calendar",
        description="Local policy description",
        effect="read",
        data_classes=("calendar_event",),
        input_schema_uri="crp://schemas/calendar-search-input-v1",
        output_schema_uri="crp://schemas/calendar-search-output-v1",
        receipt_schema_uri=None,
        operation_semantics="read_only",
        execution_mode="parallel",
        resource_locks=("mcp:calendar",),
        idempotency="never_retry",
        retry_policy=ToolRetryPolicy(1, 0, ()),
        verification_tool_id=None,
        compensation_tool_id=None,
        mutability="read_only",
        egress_class="remote",
        network_scope=("mcp:calendar",),
        data_egress_scope=("calendar_event",),
        timeout_ms=10_000,
        required_scopes=("calendar.read",),
        boundary_requirements=("mcp_enabled",),
        requires_approval=False,
        tool_schema_revision=1,
        reviewed_input_schema={"type": "object"},
        reviewed_output_schema=None,
    )


def _connection(transport, *, policies=(_policy(),), registry=None, receipts=None, limits=None):
    return MCPHostConnection(
        config=MCPHostConnectionConfig(
            server_id="calendar-server",
            manifest_revision=1,
            endpoint_identity="endpoint-1",
            credential_subject_id="credential-subject-1",
            transport_generation=1,
            catalog_revision=1,
        ),
        transport=transport,
        registry=registry or ScopedCapabilityRegistry(),
        policies=policies,
        receipt_store=receipts or _Receipts(),
        limits=limits,
    )


def _descriptor(name="calendar.search", **extra):
    return {"name": name, "description": "Untrusted server prose", "inputSchema": {"type": "object"}, **extra}


def _remote_status_policies() -> tuple[MCPToolPolicy, MCPToolPolicy]:
    status_input = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version", "server_id", "tool_name", "tool_id", "turn_id",
            "invocation_id", "operation_id", "idempotency_key",
        ],
        "properties": {
            name: {"type": "string"}
            for name in (
                "schema_version", "server_id", "tool_name", "tool_id", "turn_id",
                "invocation_id", "operation_id", "idempotency_key",
            )
        },
    }
    remote_receipt = {
        "type": "object",
        "additionalProperties": True,
        "required": ["operation_id", "invocation_id", "tool_name", "server_id"],
        "properties": {
            name: {"type": "string"}
            for name in ("operation_id", "invocation_id", "tool_name", "server_id")
        },
    }
    status_output = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version", "server_id", "tool_name", "tool_id", "turn_id",
            "invocation_id", "operation_id", "idempotency_key", "effect_certainty",
        ],
        "properties": {
            **{
                name: {"type": "string"}
                for name in (
                    "schema_version", "server_id", "tool_name", "tool_id", "turn_id",
                    "invocation_id", "operation_id", "idempotency_key",
                )
            },
            "effect_certainty": {
                "type": "string",
                "enum": ["confirmed_applied", "confirmed_none", "unknown"],
            },
            "remote_receipt": remote_receipt,
        },
    }
    status = replace(
        _policy(),
        tool_name="calendar.effect-status",
        tool_id="calendar.effect-status",
        display_name="Internal effect status",
        idempotency="idempotent",
        reviewed_input_schema=status_input,
        reviewed_output_schema=status_output,
        resource_locks=("mcp:calendar",),
    )
    target = replace(
        _policy(),
        tool_name="calendar.create",
        tool_id="calendar.create",
        display_name="Create calendar event",
        effect="write",
        receipt_schema_uri="crp://schemas/calendar-create-receipt-v1",
        operation_semantics="receipt_required",
        execution_mode="exclusive",
        idempotency="verify_before_retry",
        verification_tool_id=status.tool_id,
        mutability="irreversible",
        requires_approval=True,
        remote_receipt_field="operation_id",
        reviewed_receipt_schema=remote_receipt,
    )
    return target, status


def _remote_status_descriptor(policy: MCPToolPolicy) -> Mapping[str, object]:
    return _descriptor(
        policy.tool_name,
        inputSchema=policy.reviewed_input_schema,
        outputSchema=policy.reviewed_output_schema,
    )


class _Control:
    remaining_timeout_ms = 9_000

    def checkpoint(self):
        return None


class _Clock:
    def __init__(self, now: float = 0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _StatelessTransport(_Transport):
    def server_discover(self):
        self.calls.append(("server_discover", None))
        self.initialized = True
        return {
            "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}},
            "ttlMs": 1_000, "cacheScope": "private",
        }

    def initialize(self, request):  # pragma: no cover - must never run
        raise AssertionError("legacy initialize must not run")

    def notify_initialized(self):  # pragma: no cover - must never run
        raise AssertionError("legacy notification must not run")

    def ping(self, *, timeout_ms):  # pragma: no cover - must never run
        raise AssertionError("legacy ping must not run")


def _stateless_connection(transport, *, registry=None, receipts=None, clock=None):
    return MCPHostConnection(
        config=MCPHostConnectionConfig(
            server_id="calendar-server", manifest_revision=1, endpoint_identity="endpoint-1",
            credential_subject_id="credential-subject-1", transport_generation=1, catalog_revision=1,
            protocol_profile="stateless_2026_07_28",
        ),
        transport=transport, registry=registry or ScopedCapabilityRegistry(), policies=(_policy(),),
        receipt_store=receipts or _Receipts(), monotonic_clock=clock or _Clock(),
    )


def _request(definition, *, call_id="catalog-call"):
    return {
        "tool_call_id": call_id, "turn_id": f"{call_id}-turn", "operation_id": f"{call_id}-operation",
        "arguments": {}, "execution_context": _Control(),
        "tool_contract": tool_contract_identity(definition.tool_definition),
    }


def test_connect_negotiates_then_discovers_only_local_allowlist() -> None:
    transport = _Transport([{
        "tools": [
            _descriptor(annotations={"readOnlyHint": False, "destructiveHint": True}),
            _descriptor("unlisted.delete"),
        ],
    }])
    connection = _connection(transport)

    definitions = connection.connect()

    assert [call[0] for call in transport.calls[:3]] == ["initialize", "initialized", "list_tools"]
    assert [definition.capability_id for definition in definitions] == ["calendar.search"]
    native = definitions[0].tool_definition
    assert native is not None and native.description == "Local policy description"
    assert native.effect == "read" and native.mutability == "read_only"
    assert native.connection_identity is not None
    assert native.connection_identity.catalog_revision == 1
    assert native.connection_identity.endpoint_identity == "endpoint-1"
    assert native.connection_identity.credential_subject_id == "credential-subject-1"
    assert native.connection_identity.tool_schema_revision == 1


def test_host_exposes_only_anonymous_catalog_missing_count_without_discovery_on_read() -> None:
    transport = _Transport([{"tools": [_descriptor()] }])
    connection = _connection(
        transport,
        policies=(
            _policy(),
            _policy("private.canary", "private.canary"),
        ),
    )

    connection.connect()
    calls_before_read = tuple(transport.calls)

    assert connection.installation_reason_counts == (("catalog_missing", 1),)
    assert tuple(transport.calls) == calls_before_read
    assert "canary" not in str(connection.installation_reason_counts)
    connection.close()
    assert connection.installation_reason_counts == ()


def test_protocol_probe_does_not_invoke_tool_or_change_catalog() -> None:
    transport = _Transport([{"tools": [_descriptor()]}])
    registry = ScopedCapabilityRegistry()
    connection = _connection(transport, registry=registry)
    connection.connect()

    connection.probe(timeout_ms=250)

    assert ("ping", 250) in transport.calls
    assert not any(call[0] == "call_tool" for call in transport.calls)
    assert registry.resolve("calendar.search") is not None


def test_credential_generation_drift_revokes_leases_before_connect_probe_or_tool_wire() -> None:
    state = {"current": False}
    connect_transport = _Transport([{"tools": [_descriptor()]}])
    connection = MCPHostConnection(
        config=MCPHostConnectionConfig(
            server_id="calendar-server", manifest_revision=1, endpoint_identity="endpoint-1",
            credential_subject_id="credential-subject-1", transport_generation=1, catalog_revision=1,
        ),
        transport=connect_transport, registry=ScopedCapabilityRegistry(), policies=(_policy(),),
        receipt_store=_Receipts(), credential_generation_current=lambda: state["current"],
    )

    with pytest.raises(MCPHostError, match="credential generation"):
        connection.connect()
    assert connect_transport.calls == [("close", None), ("close", None)]

    state["current"] = True
    registry = ScopedCapabilityRegistry()
    transport = _Transport([{"tools": [_descriptor()]}])
    connection = MCPHostConnection(
        config=MCPHostConnectionConfig(
            server_id="calendar-server", manifest_revision=1, endpoint_identity="endpoint-1",
            credential_subject_id="credential-subject-1", transport_generation=1, catalog_revision=1,
        ),
        transport=transport, registry=registry, policies=(_policy(),), receipt_store=_Receipts(),
        credential_generation_current=lambda: state["current"],
    )
    definition = connection.connect()[0]
    _definition, provider = registry.resolve("calendar.search")
    state["current"] = False

    with pytest.raises(ToolProviderFailure) as error:
        provider.invoke(_request(definition, call_id="credential-drift"))
    assert error.value.error_code == "mcp.credential_generation_changed"
    assert error.value.effect_certainty == "confirmed_none"
    assert not any(name in {"ping", "call_tool"} for name, _value in transport.calls)
    assert connection.connected is False
    assert registry.resolve("calendar.search") is None


def test_credential_generation_is_rechecked_after_argument_validation_before_tool_wire() -> None:
    checks = iter((True, True, True, False))
    transport = _Transport([{"tools": [_descriptor()]}])
    registry = ScopedCapabilityRegistry()
    connection = MCPHostConnection(
        config=MCPHostConnectionConfig(
            server_id="calendar-server", manifest_revision=1, endpoint_identity="endpoint-1",
            credential_subject_id="credential-subject-1", transport_generation=1, catalog_revision=1,
        ),
        transport=transport, registry=registry, policies=(_policy(),), receipt_store=_Receipts(),
        credential_generation_current=lambda: next(checks),
    )
    definition = connection.connect()[0]
    _definition, provider = registry.resolve("calendar.search")

    with pytest.raises(ToolProviderFailure) as error:
        provider.invoke(_request(definition, call_id="late-credential-drift"))

    assert error.value.error_code == "mcp.credential_generation_changed"
    assert error.value.effect_certainty == "confirmed_none"
    assert not any(name == "call_tool" for name, _value in transport.calls)
    assert registry.resolve("calendar.search") is None


def test_http_transport_generation_race_after_host_fence_stops_first_tool_post() -> None:
    class _Store:
        def get_approved_http(self, server_id):
            return manifest if server_id == "calendar-server" else None

    class _Secrets:
        def headers_for_wire(self, *, url, secret_header_refs, purpose):
            assert purpose == "mcp_http_wire"
            return {name: "Bearer private" for name in secret_header_refs}

    class _Requester:
        def __init__(self):
            self.calls = []

        def request(self, method, _url, *, headers, body, **_values):
            payload = json.loads(body) if body else None
            self.calls.append((method, payload))
            if payload["method"] == "initialize":
                return MCPHttpResponse(200, {"Content-Type": "application/json", "MCP-Session-Id": "opaque"}, json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}}).encode())
            if payload["method"] == "notifications/initialized":
                return MCPHttpResponse(202, {}, b"")
            if payload["method"] == "tools/list":
                return MCPHttpResponse(200, {"Content-Type": "application/json"}, json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": [_descriptor()]}}).encode())
            raise AssertionError("generation drift must stop tools/call before requester")

    manifest = MCPStreamableHTTPManifest(
        server_id="calendar-server", manifest_revision=1, endpoint_identity="endpoint-1",
        credential_subject_id="credential-subject-1", transport_generation=1, approval_revision=1,
        approval_status="approved", endpoint_url="https://mcp.example.test/rpc",
        secret_header_refs={"Authorization": "mcp:calendar-server:token"},
    )
    host = MCPHostConnectionConfig(
        server_id="calendar-server", manifest_revision=1, endpoint_identity="endpoint-1",
        credential_subject_id="credential-subject-1", transport_generation=1, catalog_revision=1,
    )
    checks = iter((True, True, True, False))
    requester = _Requester()
    registry = ScopedCapabilityRegistry()
    connection = MCPHostConnection(
        config=host,
        transport=MCPStreamableHTTPTransport(
            MCPStreamableHTTPAuthority(_Store()).resolve(host), secret_injector=_Secrets(),
            requester=requester, credential_generation_current=lambda: next(checks),
        ),
        registry=registry, policies=(_policy(),), receipt_store=_Receipts(),
        credential_generation_current=lambda: True,
    )
    definition = connection.connect()[0]
    _definition, provider = registry.resolve("calendar.search")

    with pytest.raises(ToolProviderFailure) as error:
        provider.invoke(_request(definition, call_id="http-wire-race"))

    assert error.value.error_code == "mcp.credential_generation_changed"
    assert error.value.effect_certainty == "confirmed_none"
    assert [payload["method"] for _method, payload in requester.calls] == [
        "initialize", "notifications/initialized", "tools/list",
    ]
    assert registry.resolve("calendar.search") is None


def test_http_post_wire_generation_drift_aborts_sse_resume_without_rewriting_unknown_effect() -> None:
    class _Store:
        def get_approved_http(self, server_id):
            return manifest if server_id == "calendar-server" else None

    class _Secrets:
        def headers_for_wire(self, *, url, secret_header_refs, purpose):
            return {name: "Bearer private" for name in secret_header_refs}

    class _Requester:
        def __init__(self):
            self.calls = []

        def request(self, method, _url, *, headers, body, **_values):
            payload = json.loads(body) if body else None
            self.calls.append((method, payload))
            if payload["method"] == "initialize":
                return MCPHttpResponse(200, {"Content-Type": "application/json", "MCP-Session-Id": "opaque"}, json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}}).encode())
            if payload["method"] == "notifications/initialized":
                return MCPHttpResponse(202, {}, b"")
            if payload["method"] == "tools/list":
                return MCPHttpResponse(200, {"Content-Type": "application/json"}, json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": [_descriptor()]}}).encode())
            if payload["method"] == "tools/call":
                return MCPHttpResponse(200, {"Content-Type": "text/event-stream"}, b"id: 1\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\",\"params\":{}}\n\n", complete=False)
            raise AssertionError("post-wire drift must prohibit resume and cleanup requests")

    manifest = MCPStreamableHTTPManifest(
        server_id="calendar-server", manifest_revision=1, endpoint_identity="endpoint-1",
        credential_subject_id="credential-subject-1", transport_generation=1, approval_revision=1,
        approval_status="approved", endpoint_url="https://mcp.example.test/rpc",
        secret_header_refs={"Authorization": "mcp:calendar-server:token"},
    )
    host = MCPHostConnectionConfig(
        server_id="calendar-server", manifest_revision=1, endpoint_identity="endpoint-1",
        credential_subject_id="credential-subject-1", transport_generation=1, catalog_revision=1,
    )
    checks = iter((True, True, True, True, False))
    requester = _Requester()
    registry = ScopedCapabilityRegistry()
    connection = MCPHostConnection(
        config=host,
        transport=MCPStreamableHTTPTransport(
            MCPStreamableHTTPAuthority(_Store()).resolve(host), secret_injector=_Secrets(),
            requester=requester, credential_generation_current=lambda: next(checks),
        ),
        registry=registry, policies=(_policy(),), receipt_store=_Receipts(),
        credential_generation_current=lambda: True,
    )
    definition = connection.connect()[0]
    _definition, provider = registry.resolve("calendar.search")

    with pytest.raises(ToolProviderFailure) as error:
        provider.invoke(_request(definition, call_id="http-post-wire-drift"))

    assert error.value.error_code == "mcp.transport_unconfirmed"
    assert error.value.effect_certainty == "unknown"
    assert [method for method, _payload in requester.calls] == ["POST", "POST", "POST", "POST"]
    assert [payload["method"] for _method, payload in requester.calls] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call",
    ]
    assert registry.resolve("calendar.search") is None


def test_stateless_profile_discovers_without_legacy_lifecycle_and_freezes_version_identity() -> None:
    class _StatelessTransport(_Transport):
        def server_discover(self):
            self.calls.append(("server_discover", None))
            self.initialized = True
            return {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 1000, "cacheScope": "private", "resultType": "complete"}

        def initialize(self, request):  # pragma: no cover - must never run
            raise AssertionError("legacy initialize must not run")

        def notify_initialized(self):  # pragma: no cover - must never run
            raise AssertionError("legacy notification must not run")

        def ping(self, *, timeout_ms):  # pragma: no cover - must never run
            raise AssertionError("legacy ping must not run")

    transport = _StatelessTransport([{"tools": [_descriptor()], "ttlMs": 1000, "cacheScope": "private"}])
    config = MCPHostConnectionConfig(
        server_id="calendar-server", manifest_revision=1, endpoint_identity="endpoint-1",
        credential_subject_id="credential-subject-1", transport_generation=1, catalog_revision=1,
        protocol_profile="stateless_2026_07_28",
    )
    receipts = _Receipts()
    connection = MCPHostConnection(
        config=config, transport=transport, registry=ScopedCapabilityRegistry(),
        policies=(_policy(),), receipt_store=receipts,
    )
    definition = connection.connect()[0]
    assert [name for name, _value in transport.calls[:2]] == ["server_discover", "list_tools"]
    assert definition.tool_definition.connection_identity.protocol_version == "2026-07-28"
    _definition, provider = connection._registry.resolve("calendar.search")
    provider.invoke({
        "tool_call_id": "stateless-call", "turn_id": "stateless-turn", "operation_id": "stateless-operation",
        "arguments": {}, "execution_context": _Control(),
        "tool_contract": tool_contract_identity(definition.tool_definition),
    })
    assert receipts.items[0]["protocol_version"] == "2026-07-28"
    connection.probe(timeout_ms=250)
    assert [name for name, _value in transport.calls].count("server_discover") == 2


def test_stateless_ttl_zero_refreshes_before_the_first_tool_post() -> None:
    clock = _Clock()
    transport = _StatelessTransport([
        {"tools": [_descriptor()], "ttlMs": 0, "cacheScope": "private"},
        {"tools": [_descriptor()], "ttlMs": 0, "cacheScope": "private"},
    ])
    registry = ScopedCapabilityRegistry()
    connection = _stateless_connection(transport, registry=registry, clock=clock)
    definition = connection.connect()[0]
    _definition, provider = registry.resolve("calendar.search")

    provider.invoke(_request(definition))

    assert [name for name, _value in transport.calls].count("list_tools") == 2
    assert [name for name, _value in transport.calls].count("call_tool") == 1


def test_stateless_unexpired_catalog_does_not_list_again() -> None:
    clock = _Clock()
    transport = _StatelessTransport([{"tools": [_descriptor()], "ttlMs": 1_000, "cacheScope": "private"}])
    registry = ScopedCapabilityRegistry()
    connection = _stateless_connection(transport, registry=registry, clock=clock)
    definition = connection.connect()[0]
    _definition, provider = registry.resolve("calendar.search")
    clock.now = 0.5

    provider.invoke(_request(definition))

    assert [name for name, _value in transport.calls].count("list_tools") == 1
    assert [name for name, _value in transport.calls].count("call_tool") == 1


def test_stateless_expired_unchanged_catalog_refreshes_once_then_calls() -> None:
    clock = _Clock()
    transport = _StatelessTransport([
        {"tools": [_descriptor()], "ttlMs": 1_000, "cacheScope": "private"},
        {"tools": [_descriptor()], "ttlMs": 1_000, "cacheScope": "private"},
    ])
    registry = ScopedCapabilityRegistry()
    connection = _stateless_connection(transport, registry=registry, clock=clock)
    definition = connection.connect()[0]
    _definition, provider = registry.resolve("calendar.search")
    clock.now = 1

    provider.invoke(_request(definition))

    assert connection.catalog_revision == 1
    assert [name for name, _value in transport.calls].count("list_tools") == 2
    assert [name for name, _value in transport.calls].count("call_tool") == 1


def test_stateless_expired_catalog_drift_revokes_before_tool_post() -> None:
    clock = _Clock()
    transport = _StatelessTransport([
        {"tools": [_descriptor()], "ttlMs": 1_000, "cacheScope": "private"},
        {"tools": [_descriptor(description="drift")], "ttlMs": 1_000, "cacheScope": "private"},
    ])
    registry = ScopedCapabilityRegistry()
    connection = _stateless_connection(transport, registry=registry, clock=clock)
    definition = connection.connect()[0]
    _definition, provider = registry.resolve("calendar.search")
    clock.now = 1

    with pytest.raises(ToolProviderFailure) as error:
        provider.invoke(_request(definition))
    assert error.value.error_code == "mcp.catalog_unavailable"
    assert error.value.effect_certainty == "confirmed_none"

    assert connection.connected is False
    assert registry.resolve("calendar.search") is None
    assert not any(name == "call_tool" for name, _value in transport.calls)


def test_stateless_concurrent_expired_calls_share_one_catalog_refresh() -> None:
    clock = _Clock(1)
    transport = _StatelessTransport([
        {"tools": [_descriptor()], "ttlMs": 1_000, "cacheScope": "private"},
        {"tools": [_descriptor()], "ttlMs": 1_000, "cacheScope": "private"},
    ])
    registry = ScopedCapabilityRegistry()
    connection = _stateless_connection(transport, registry=registry, clock=clock)
    definition = connection.connect()[0]
    _definition, provider = registry.resolve("calendar.search")
    clock.now = 2
    errors: list[BaseException] = []

    def invoke(call_id: str) -> None:
        try:
            provider.invoke(_request(definition, call_id=call_id))
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    first = Thread(target=invoke, args=("catalog-concurrent-1",))
    second = Thread(target=invoke, args=("catalog-concurrent-2",))
    first.start()
    second.start()
    first.join(2)
    second.join(2)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert [name for name, _value in transport.calls].count("list_tools") == 2
    assert [name for name, _value in transport.calls].count("call_tool") == 2


def test_stateless_catalog_uses_the_shortest_page_ttl() -> None:
    clock = _Clock()
    transport = _StatelessTransport([
        {"tools": [_descriptor()], "nextCursor": "next", "ttlMs": 2_000, "cacheScope": "private"},
        {"tools": [], "ttlMs": 5, "cacheScope": "private"},
        {"tools": [_descriptor()], "ttlMs": 1_000, "cacheScope": "private"},
    ])
    registry = ScopedCapabilityRegistry()
    connection = _stateless_connection(transport, registry=registry, clock=clock)
    definition = connection.connect()[0]
    _definition, provider = registry.resolve("calendar.search")
    clock.now = 0.006

    provider.invoke(_request(definition))

    assert [name for name, _value in transport.calls].count("list_tools") == 3
    assert [name for name, _value in transport.calls].count("call_tool") == 1


def test_stateless_expired_catalog_refresh_network_failure_revokes_before_tool_post() -> None:
    class _FailingRefreshTransport(_StatelessTransport):
        def list_tools(self, cursor=None):
            if not self.pages:
                raise ConnectionError("refresh unavailable")
            return super().list_tools(cursor)

    clock = _Clock()
    transport = _FailingRefreshTransport([{"tools": [_descriptor()], "ttlMs": 1_000, "cacheScope": "private"}])
    registry = ScopedCapabilityRegistry()
    connection = _stateless_connection(transport, registry=registry, clock=clock)
    definition = connection.connect()[0]
    _definition, provider = registry.resolve("calendar.search")
    clock.now = 1

    with pytest.raises(ToolProviderFailure) as error:
        provider.invoke(_request(definition))
    assert error.value.error_code == "mcp.catalog_unavailable"
    assert error.value.effect_certainty == "confirmed_none"

    assert connection.connected is False
    assert registry.resolve("calendar.search") is None
    assert not any(name == "call_tool" for name, _value in transport.calls)


@pytest.mark.parametrize("invalid_clock", [float("nan"), float("inf"), float("-inf")])
def test_stateless_invalid_monotonic_clock_fails_closed_without_registry_lease(invalid_clock) -> None:
    clock = _Clock()
    clock.now = invalid_clock
    transport = _StatelessTransport([{"tools": [_descriptor()], "ttlMs": 1_000, "cacheScope": "private"}])
    registry = ScopedCapabilityRegistry()
    connection = _stateless_connection(transport, registry=registry, clock=clock)

    with pytest.raises(MCPHostError, match="monotonic clock"):
        connection.connect()

    assert connection.connected is False
    assert registry.resolve("calendar.search") is None
    assert ("close", None) in transport.calls


def test_stateless_clock_failure_during_expired_refresh_is_confirmed_none_and_revokes() -> None:
    clock = _Clock()
    transport = _StatelessTransport([
        {"tools": [_descriptor()], "ttlMs": 1_000, "cacheScope": "private"},
        {"tools": [_descriptor()], "ttlMs": 1_000, "cacheScope": "private"},
    ])
    registry = ScopedCapabilityRegistry()
    connection = _stateless_connection(transport, registry=registry, clock=clock)
    definition = connection.connect()[0]
    _definition, provider = registry.resolve("calendar.search")
    clock.now = float("nan")

    with pytest.raises(ToolProviderFailure) as error:
        provider.invoke(_request(definition))
    assert error.value.error_code == "mcp.catalog_unavailable"
    assert error.value.effect_certainty == "confirmed_none"
    assert connection.connected is False
    assert registry.resolve("calendar.search") is None
    assert not any(name == "call_tool" for name, _value in transport.calls)


def test_stateless_clock_failure_during_changed_explicit_refresh_revokes_new_leases() -> None:
    clock = _Clock()
    transport = _StatelessTransport([
        {"tools": [_descriptor()], "ttlMs": 1_000, "cacheScope": "private"},
        {"tools": [_descriptor(), _descriptor("unlisted.new")], "ttlMs": 1_000, "cacheScope": "private"},
    ])
    registry = ScopedCapabilityRegistry()
    connection = _stateless_connection(transport, registry=registry, clock=clock)
    connection.connect()
    clock.now = float("inf")

    with pytest.raises(MCPHostError, match="monotonic clock"):
        connection.refresh(catalog_revision=2)

    assert connection.connected is False
    assert registry.resolve("calendar.search") is None
    assert ("close", None) in transport.calls


def test_stateless_remote_ttl_is_capped_by_local_freshness_limit() -> None:
    clock = _Clock()
    transport = _StatelessTransport([
        {"tools": [_descriptor()], "ttlMs": 86_400_000, "cacheScope": "private"},
        {"tools": [_descriptor()], "ttlMs": 86_400_000, "cacheScope": "private"},
    ])
    registry = ScopedCapabilityRegistry()
    connection = _stateless_connection(transport, registry=registry, clock=clock)
    definition = connection.connect()[0]
    _definition, provider = registry.resolve("calendar.search")
    clock.now = 300

    provider.invoke(_request(definition))

    assert [name for name, _value in transport.calls].count("list_tools") == 2
    assert [name for name, _value in transport.calls].count("call_tool") == 1


def test_provider_projects_only_frozen_reviewed_parameter_headers_after_argument_validation() -> None:
    schema = {"type": "object", "properties": {
        "query": {"type": "string", "x-mcp-header": "Query"},
        "nested": {"type": "object", "properties": {
            "count": {"type": "integer", "x-mcp-header": "Count"},
        }},
    }}
    policy = replace(_policy(), reviewed_input_schema=schema)
    class _StatelessTransport(_Transport):
        def server_discover(self):
            self.initialized = True
            return {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 1_000, "cacheScope": "private", "resultType": "complete"}

    transport = _StatelessTransport([{"tools": [_descriptor(inputSchema=schema)], "ttlMs": 1_000, "cacheScope": "private"}])
    connection = MCPHostConnection(
        config=MCPHostConnectionConfig(
            server_id="calendar-server", manifest_revision=1, endpoint_identity="endpoint-1",
            credential_subject_id="credential-subject-1", transport_generation=1, catalog_revision=1,
            protocol_profile="stateless_2026_07_28",
        ),
        transport=transport, registry=ScopedCapabilityRegistry(), policies=(policy,), receipt_store=_Receipts(),
    )
    definition = connection.connect()[0]
    _definition, provider = connection._registry.resolve("calendar.search")
    provider.invoke({
        "tool_call_id": "headers-call", "turn_id": "headers-turn", "operation_id": "headers-operation",
        "arguments": {"query": "中文", "nested": {"count": 3}, "unreviewed": "ignored"},
        "execution_context": _Control(), "tool_contract": tool_contract_identity(definition.tool_definition),
    })
    call = next(value for name, value in transport.calls if name == "call_tool")
    assert call[4] == {"Mcp-Param-Query": "=?base64?5Lit5paH?=", "Mcp-Param-Count": "3"}


def test_legacy_provider_never_sends_parameter_headers() -> None:
    schema = {"type": "object", "properties": {"query": {"type": "string", "x-mcp-header": "Query"}}}
    policy = replace(_policy(), reviewed_input_schema=schema)
    transport = _Transport([{"tools": [_descriptor(inputSchema=schema)]}])
    connection = _connection(transport, policies=(policy,))
    definition = connection.connect()[0]
    _definition, provider = connection._registry.resolve("calendar.search")
    provider.invoke({
        "tool_call_id": "legacy-headers-call", "turn_id": "legacy-headers-turn", "operation_id": "legacy-headers-operation",
        "arguments": {"query": "alpha"}, "execution_context": _Control(),
        "tool_contract": tool_contract_identity(definition.tool_definition),
    })
    call = next(value for name, value in transport.calls if name == "call_tool")
    assert call[4] == {}


def test_stateless_http_header_mismatch_revokes_connection_without_refresh_retry_or_receipt() -> None:
    class _Store:
        def get_approved_http(self, server_id):
            return manifest if server_id == "calendar-server" else None

    class _Secrets:
        def headers_for_wire(self, *, url, secret_header_refs, purpose):
            assert not secret_header_refs
            return {}

    class _Requester:
        def __init__(self):
            self.calls = []

        def request(self, method, url, *, headers, body, **_values):
            payload = json.loads(body) if body else None
            self.calls.append((method, dict(headers), payload))
            if payload["method"] == "server/discover":
                result = {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 1_000, "cacheScope": "private", "resultType": "complete"}
                return MCPHttpResponse(200, {"Content-Type": "application/json"}, json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result}).encode())
            if payload["method"] == "tools/list":
                result = {"tools": [_descriptor(inputSchema=schema)], "ttlMs": 1_000, "cacheScope": "private", "resultType": "complete"}
                return MCPHttpResponse(200, {"Content-Type": "application/json"}, json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result}).encode())
            error = {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32020, "message": "HeaderMismatch"}}
            return MCPHttpResponse(400, {"Content-Type": "application/json"}, json.dumps(error).encode())

    schema = {"type": "object", "properties": {"query": {"type": "string", "x-mcp-header": "Query"}}}
    host = MCPHostConnectionConfig(
        server_id="calendar-server", manifest_revision=1, endpoint_identity="calendar-endpoint",
        credential_subject_id="calendar-user", transport_generation=1, catalog_revision=1,
        protocol_profile="stateless_2026_07_28",
    )
    manifest = MCPStreamableHTTPManifest(
        server_id="calendar-server", manifest_revision=1, endpoint_identity="calendar-endpoint",
        credential_subject_id="calendar-user", transport_generation=1, approval_revision=1,
        approval_status="approved", endpoint_url="https://mcp.example.test/rpc",
        protocol_profile="stateless_2026_07_28",
    )
    requester = _Requester()
    transport = MCPStreamableHTTPTransport(
        MCPStreamableHTTPAuthority(_Store()).resolve(host), secret_injector=_Secrets(), requester=requester,
    )
    receipts = _Receipts()
    registry = ScopedCapabilityRegistry()
    connection = MCPHostConnection(
        config=host, transport=transport, registry=registry,
        policies=(replace(_policy(), reviewed_input_schema=schema),), receipt_store=receipts,
    )
    definition = connection.connect()[0]
    _definition, provider = registry.resolve("calendar.search")
    request = {
        "tool_call_id": "header-mismatch", "turn_id": "header-mismatch-turn", "operation_id": "header-mismatch-operation",
        "arguments": {"query": "alpha"}, "execution_context": _Control(),
        "tool_contract": tool_contract_identity(definition.tool_definition),
    }
    with pytest.raises(ToolProviderFailure) as error:
        provider.invoke(request)
    assert error.value.error_code == "mcp.transport_unconfirmed"
    assert error.value.effect_certainty == "unknown"
    assert receipts.items == []
    assert [call[2]["method"] for call in requester.calls] == ["server/discover", "tools/list", "tools/call"]
    with pytest.raises(ToolProviderFailure, match="connection_revoked"):
        provider.invoke(request)
    assert len(requester.calls) == 3


def test_stateless_input_required_revokes_provider_without_completed_receipt() -> None:
    class _StatelessTransport(_Transport):
        def server_discover(self):
            self.initialized = True
            return {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 1000, "cacheScope": "private"}

        def call_tool(self, name, arguments, *, timeout_ms, execution_control, parameter_headers=None):
            raise MCPInputRequiredError("input")

    receipts = _Receipts()
    registry = ScopedCapabilityRegistry()
    connection = MCPHostConnection(
        config=MCPHostConnectionConfig(
            server_id="calendar-server", manifest_revision=1, endpoint_identity="endpoint-1",
            credential_subject_id="credential-subject-1", transport_generation=1, catalog_revision=1,
            protocol_profile="stateless_2026_07_28",
        ),
        transport=_StatelessTransport([{"tools": [_descriptor()], "ttlMs": 1000, "cacheScope": "private"}]),
        registry=registry, policies=(_policy(),), receipt_store=receipts,
    )
    definition = connection.connect()[0]
    _definition, provider = registry.resolve("calendar.search")
    with pytest.raises(ToolProviderFailure) as error:
        provider.invoke({
            "tool_call_id": "call-input", "turn_id": "turn-input", "operation_id": "operation-input",
            "arguments": {}, "execution_context": _Control(),
            "tool_contract": tool_contract_identity(definition.tool_definition),
        })
    assert error.value.error_code == "mcp.input_required"
    assert error.value.effect_certainty == "unknown"
    assert receipts.items == []
    assert connection.connected is False


def test_credentialed_stateless_request_state_remains_unknown_and_not_continuable() -> None:
    class _StatelessTransport(_Transport):
        def server_discover(self):
            self.initialized = True
            return {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 1000, "cacheScope": "private"}

        def call_tool(self, name, arguments, *, timeout_ms, execution_control, parameter_headers=None):
            raise MCPInputRequiredError("input", request_state=b"opaque-request-state")

    registry = ScopedCapabilityRegistry()
    connection = MCPHostConnection(
        config=MCPHostConnectionConfig(
            server_id="calendar-server", manifest_revision=1, endpoint_identity="endpoint-1",
            credential_subject_id="credential-subject-1", transport_generation=1, catalog_revision=1,
            protocol_profile="stateless_2026_07_28",
        ),
        transport=_StatelessTransport([{"tools": [_descriptor()], "ttlMs": 1000, "cacheScope": "private"}]),
        registry=registry, policies=(_policy(),), receipt_store=_Receipts(),
        request_state_continuation_allowed=False,
    )
    definition = connection.connect()[0]
    _definition, provider = registry.resolve("calendar.search")
    with pytest.raises(ToolProviderFailure) as error:
        provider.invoke({
            "tool_call_id": "credentialed-input", "turn_id": "credentialed-turn", "operation_id": "credentialed-operation",
            "arguments": {}, "execution_context": _Control(),
            "tool_contract": tool_contract_identity(definition.tool_definition),
        })
    assert error.value.error_code == "mcp.input_required"
    assert error.value.effect_certainty == "unknown"
    assert error.value.continuation_state is None


def test_request_state_continuation_rechecks_current_admission_before_wire_call() -> None:
    class _StatelessTransport(_Transport):
        def server_discover(self):
            self.initialized = True
            return {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 1000, "cacheScope": "private"}

        def continue_tool(self, *_args, **_kwargs):  # pragma: no cover - gate must reject first
            raise AssertionError("denied continuation must not reach transport")

    registry = ScopedCapabilityRegistry()
    connection = MCPHostConnection(
        config=MCPHostConnectionConfig(
            server_id="calendar-server", manifest_revision=1, endpoint_identity="endpoint-1",
            credential_subject_id="anonymous", transport_generation=1, catalog_revision=1,
            protocol_profile="stateless_2026_07_28",
        ),
        transport=_StatelessTransport([{"tools": [_descriptor()], "ttlMs": 1000, "cacheScope": "private"}]),
        registry=registry, policies=(_policy(),), receipt_store=_Receipts(),
        request_state_continuation_allowed=True,
    )
    definition = connection.connect()[0]
    _definition, provider = registry.resolve("calendar.search")
    # Equivalent to a reconnect whose current admission record now carries a
    # secret/credential fence: provider admission must be checked pre-wire.
    provider._request_state_continuation_allowed = False  # type: ignore[attr-defined]

    with pytest.raises(ToolProviderFailure, match="mcp.continuation_denied") as error:
        provider.continue_request_state(_request(definition, call_id="continue-denied"), b"opaque-state")

    assert error.value.effect_certainty == "confirmed_none"
    assert not any(call[0] == "continue_tool" for call in connection._transport.calls)  # type: ignore[attr-defined]


@pytest.mark.parametrize(("discover", "page"), [
    ({"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}}, {"tools": [], "ttlMs": 1, "cacheScope": "private"}),
    ({"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": -1, "cacheScope": "private"}, {"tools": [], "ttlMs": 1, "cacheScope": "private"}),
    ({"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 1, "cacheScope": "private"}, {"tools": []}),
])
def test_stateless_cacheable_discovery_and_catalog_require_valid_wire_bookkeeping(discover, page) -> None:
    class _StatelessTransport(_Transport):
        def server_discover(self):
            self.initialized = True
            return discover

    connection = MCPHostConnection(
        config=MCPHostConnectionConfig(
            server_id="calendar-server", manifest_revision=1, endpoint_identity="endpoint-1",
            credential_subject_id="credential-subject-1", transport_generation=1, catalog_revision=1,
            protocol_profile="stateless_2026_07_28",
        ),
        transport=_StatelessTransport([page]), registry=ScopedCapabilityRegistry(),
        policies=(_policy(),), receipt_store=_Receipts(),
    )
    with pytest.raises(MCPHostError, match="cache metadata"):
        connection.connect()


def test_catalog_pagination_rejects_duplicate_names_and_limits() -> None:
    duplicate = _Transport([
        {"tools": [_descriptor()], "nextCursor": "page-2"},
        {"tools": [_descriptor()]},
    ])
    with pytest.raises(MCPHostError, match="duplicate"):
        _connection(duplicate).connect()

    oversized = _Transport([{"tools": [_descriptor(description="x" * 500)]}])
    with pytest.raises(MCPHostError, match="description"):
        _connection(oversized, limits=MCPHostLimits(max_description_bytes=128)).connect()


def test_side_effecting_policy_cannot_disable_local_approval() -> None:
    values = {
        "tool_name": "calendar.delete",
        "tool_id": "calendar.delete",
        "effect": "delete",
        "operation_semantics": "receipt_required",
        "receipt_schema_uri": "crp://schemas/calendar-delete-receipt-v1",
        "execution_mode": "exclusive",
        "mutability": "irreversible",
        "requires_approval": False,
    }
    with pytest.raises(MCPHostError, match="requires approval"):
        replace(_policy(), **values)


def test_catalog_drift_requires_new_revision_and_releases_old_lease() -> None:
    registry = ScopedCapabilityRegistry()
    transport = _Transport([{"tools": [_descriptor()]}])
    connection = _connection(transport, registry=registry)
    connection.connect()
    transport.pages.append({"tools": [_descriptor("calendar.search", inputSchema={"type": "object", "properties": {"a": {"type": "string"}}})]})

    with pytest.raises(MCPHostError, match="drift"):
        connection.refresh(catalog_revision=1)
    assert connection.connected is False
    assert registry.get("calendar.search") is None

    transport = _Transport([{"tools": [_descriptor()]}])
    connection = _connection(transport, registry=registry)
    connection.connect()
    transport.pages.append({"tools": [_descriptor(), _descriptor("unlisted.new")]})
    refreshed = connection.refresh(catalog_revision=2)
    assert refreshed[0].tool_definition.connection_identity.catalog_revision == 2
    connection.close()
    assert registry.get("calendar.search") is None


def test_new_catalog_revision_cannot_bypass_reviewed_schema_revision() -> None:
    registry = ScopedCapabilityRegistry()
    transport = _Transport([{"tools": [_descriptor()]}])
    connection = _connection(transport, registry=registry)
    connection.connect()
    transport.pages.append({"tools": [
        _descriptor("calendar.search", inputSchema={
            "type": "object", "properties": {"changed": {"type": "string"}},
        }),
    ]})

    with pytest.raises(MCPHostError, match="reviewed tool schema drifted"):
        connection.refresh(catalog_revision=2)

    assert connection.connected is False
    assert registry.get("calendar.search") is None


def test_failed_refresh_fails_closed_after_invalidating_old_lease() -> None:
    registry = ScopedCapabilityRegistry()
    transport = _Transport([{"tools": [_descriptor()]}])
    connection = _connection(transport, registry=registry)
    connection.connect()
    transport.pages.append({"tools": [_descriptor("calendar.search", description="x" * 5000)]})

    with pytest.raises(MCPHostError, match="description"):
        connection.refresh(catalog_revision=2)

    assert connection.connected is False
    assert registry.get("calendar.search") is None


def test_provider_isolates_raw_result_and_records_metadata_only_receipt() -> None:
    transport = _Transport([{"tools": [_descriptor()]}])
    receipts = _Receipts()
    registry = ScopedCapabilityRegistry()
    connection = _connection(transport, registry=registry, receipts=receipts)
    connection.connect()
    resolved = registry.resolve("calendar.search")
    assert resolved is not None

    tool = resolved[0].tool_definition
    assert tool is not None
    result = resolved[1].invoke({
        "tool_call_id": "call-1",
        "turn_id": "turn-1", "operation_id": "operation-1",
        "arguments": {"query": "private"},
        "execution_context": _Control(),
        "tool_contract": tool_contract_identity(tool),
    })

    assert result["summary"] == "MCP tool completed; remote output is isolated"
    assert isinstance(result["receipt_ref"], str)
    assert result["evidence_refs"] == []
    assert "private remote result" not in str(result)
    receipt = receipts.items[0]
    assert receipt["raw_input_recorded"] is False
    assert receipt["raw_output_recorded"] is False
    assert "endpoint_identity" not in receipt and "credential_subject_id" not in receipt
    assert "private" not in str(receipt)
    assert receipt["remote_operation_id"] is None
    call = next(value for name, value in transport.calls if name == "call_tool")
    assert call[2] == 9_000 and call[3].__class__ is _Control


def test_provider_treats_transport_or_server_failure_as_unconfirmed() -> None:
    transport = _Transport([{"tools": [_descriptor()]}])
    registry = ScopedCapabilityRegistry()
    connection = _connection(transport, registry=registry)
    connection.connect()
    resolved = registry.resolve("calendar.search")
    assert resolved is not None
    transport.result = {"isError": True, "content": [{"type": "text", "text": "no details"}]}
    tool = resolved[0].tool_definition
    assert tool is not None

    with pytest.raises(ToolProviderFailure) as error:
        resolved[1].invoke({
            "tool_call_id": "call-2", "turn_id": "turn-2", "operation_id": "operation-2", "arguments": {},
            "execution_context": _Control(),
            "tool_contract": tool_contract_identity(tool),
        })
    assert error.value.error_code == "mcp.remote_error"
    assert error.value.effect_certainty == "unknown"


def test_provider_rejects_drifted_durable_tool_contract_before_transport() -> None:
    transport = _Transport([{"tools": [_descriptor()]}])
    registry = ScopedCapabilityRegistry()
    connection = _connection(transport, registry=registry)
    connection.connect()
    definition, provider = registry.resolve("calendar.search")
    contract = tool_contract_identity(definition.tool_definition)
    contract["connection_identity"]["transport_generation"] = 2

    with pytest.raises(ToolProviderFailure) as error:
        provider.invoke({
            "tool_call_id": "call-3", "turn_id": "turn-3", "operation_id": "operation-3", "arguments": {},
            "execution_context": _Control(), "tool_contract": contract,
        })

    assert error.value.error_code == "mcp.tool_contract_drift"
    assert not any(name == "call_tool" for name, _value in transport.calls)


def test_resolved_provider_cannot_call_after_connection_revocation() -> None:
    transport = _Transport([{"tools": [_descriptor()]}])
    registry = ScopedCapabilityRegistry()
    connection = _connection(transport, registry=registry)
    connection.connect()
    definition, provider = registry.resolve("calendar.search")
    contract = tool_contract_identity(definition.tool_definition)
    connection.close()

    with pytest.raises(ToolProviderFailure) as error:
        provider.invoke({
            "tool_call_id": "call-revoked", "turn_id": "turn-revoked",
            "operation_id": "operation-revoked", "arguments": {},
            "execution_context": _Control(), "tool_contract": contract,
        })

    assert error.value.error_code == "mcp.connection_revoked"
    assert not any(name == "call_tool" for name, _value in transport.calls)


def test_close_waits_for_an_authorized_remote_call_before_revoking_connection() -> None:
    call_started = Event()
    release_call = Event()
    close_attempted = Event()
    close_completed = Event()

    class _BlockingTransport(_Transport):
        def call_tool(self, name, arguments, *, timeout_ms, execution_control, parameter_headers=None):
            self.calls.append(("call_tool", (name, dict(arguments), timeout_ms, execution_control)))
            call_started.set()
            assert release_call.wait(2), "test did not release the authorized MCP call"
            return self.result

        def close(self):
            super().close()
            close_completed.set()

    transport = _BlockingTransport([{"tools": [_descriptor()]}])
    registry = ScopedCapabilityRegistry()
    connection = _connection(transport, registry=registry)
    connection.connect()
    definition, provider = registry.resolve("calendar.search")
    request = {
        "tool_call_id": "call-active", "turn_id": "turn-active",
        "operation_id": "operation-active", "arguments": {},
        "execution_context": _Control(),
        "tool_contract": tool_contract_identity(definition.tool_definition),
    }
    provider_errors: list[BaseException] = []

    def invoke_provider() -> None:
        try:
            provider.invoke(request)
        except BaseException as error:  # pragma: no cover - asserted below
            provider_errors.append(error)

    def close_connection() -> None:
        close_attempted.set()
        connection.close()

    invoke_thread = Thread(target=invoke_provider)
    close_thread = Thread(target=close_connection)
    invoke_thread.start()
    assert call_started.wait(2)
    close_thread.start()
    assert close_attempted.wait(2)
    assert not close_completed.wait(0.1)

    release_call.set()
    invoke_thread.join(2)
    close_thread.join(2)

    assert not invoke_thread.is_alive() and not close_thread.is_alive()
    assert provider_errors == []
    assert close_completed.is_set()
    assert [name for name, _value in transport.calls].index("call_tool") < [
        name for name, _value in transport.calls
    ].index("close")


def test_side_effect_requires_remote_operation_receipt_before_local_receipt() -> None:
    policy = replace(
        _policy(),
        tool_name="calendar.create",
        tool_id="calendar.create",
        display_name="Create calendar event",
        effect="write",
        receipt_schema_uri="crp://schemas/calendar-create-receipt-v1",
        operation_semantics="receipt_required",
        execution_mode="exclusive",
        idempotency="never_retry",
        retry_policy=ToolRetryPolicy(1, 0, ()),
        mutability="irreversible",
        requires_approval=True,
        remote_receipt_field="operation_id",
        reviewed_receipt_schema={
            "type": "object",
            "additionalProperties": True,
            "required": ["operation_id", "invocation_id", "tool_name", "server_id"],
            "properties": {
                "operation_id": {"type": "string"},
                "invocation_id": {"type": "string"},
                "tool_name": {"type": "string"},
                "server_id": {"type": "string"},
            },
        },
    )
    transport = _Transport([{"tools": [_descriptor("calendar.create")]}])
    receipts = _Receipts()
    registry = ScopedCapabilityRegistry()
    connection = _connection(
        transport, policies=(policy,), registry=registry, receipts=receipts,
    )
    connection.connect()
    definition, provider = registry.resolve("calendar.create")
    contract = tool_contract_identity(definition.tool_definition)
    request = {
        "tool_call_id": "call-4", "turn_id": "turn-4", "operation_id": "operation-4",
        "idempotency_key": "operation-4:call-4", "attempt": 1,
        "arguments": {"title": "private"},
        "execution_context": _Control(), "tool_contract": contract,
        "capability_id": "calendar.create", "capability_version": 1,
        "authorization_facts_ref": "crp://session/turn-4/tool-authorization/call-4",
        "authorization_facts_revision": "authorization-v4",
        "approval_fact_ref": "crp://session/turn-4/tool-approval/call-4",
        "execution_mode": "exclusive", "resource_locks": ["calendar:write"],
        "timeout_ms": 9_000,
    }

    with pytest.raises(ToolProviderFailure) as error:
        provider.invoke(request)
    assert error.value.error_code == "mcp.remote_receipt_unconfirmed"
    assert error.value.effect_certainty == "unknown"
    assert receipts.items == []

    with pytest.raises(ToolProviderFailure) as retry_error:
        provider.invoke(request)
    assert retry_error.value.error_code == "mcp.side_effect_indeterminate"
    assert retry_error.value.effect_certainty == "unknown"

    transport.result = {
        "content": [{"type": "text", "text": "private remote result"}],
        "structuredContent": {
            "operation_id": "operation-4",
            "invocation_id": "call-4",
            "tool_name": "calendar.create",
            "server_id": "calendar-server",
            "private": "hidden",
        },
    }
    completed_request = dict(request)
    completed_request["tool_call_id"] = "call-5"
    completed_request["operation_id"] = "operation-5"
    completed_request["idempotency_key"] = "operation-5:call-5"
    transport.result = {
        **transport.result,
        "structuredContent": {
            **transport.result["structuredContent"],
            "operation_id": "operation-5",
            "invocation_id": "call-5",
        },
    }
    result = provider.invoke(completed_request)

    assert result["receipt_ref"].startswith("crp://default/mcp-receipts/")
    assert receipts.intents[("turn-4", "call-5")] == {
        "schema_version": "1.0.0", "turn_id": "turn-4",
        "invocation_id": "call-5", "operation_id": "operation-5",
        "idempotency_key": "operation-5:call-5", "attempt": 1,
        "server_id": "calendar-server", "tool_id": "calendar.create",
        "tool_name": "calendar.create", "protocol_version": "2025-11-25",
        "capability_id": "calendar.create", "capability_version": 1,
        "authorization_facts_ref": "crp://session/turn-4/tool-authorization/call-4",
        "authorization_facts_revision": "authorization-v4",
        "approval_fact_ref": "crp://session/turn-4/tool-approval/call-4",
        "execution_mode": "exclusive", "resource_locks": ["calendar:write"],
        "timeout_ms": 9_000, "tool_contract": contract,
        "lease_ttl_seconds": 14,
    }
    assert receipts.items[0]["remote_operation_id"] == "operation-5"
    assert "hidden" not in str(receipts.items[0])
    wire_calls = [call for call in transport.calls if call[0] == "call_tool"]
    assert wire_calls[-1][1][5] == {
        "turn_id": "turn-4",
        "invocation_id": "call-5",
        "operation_id": "operation-5",
        "idempotency_key": "operation-5:call-5",
        "attempt": 1,
    }

    recovered = provider.invoke(completed_request)

    assert recovered["receipt_ref"] == result["receipt_ref"]
    assert recovered["summary"] == "MCP tool completion recovered from immutable receipt"
    assert len([call for call in transport.calls if call[0] == "call_tool"]) == len(wire_calls)

    recovered_after_restart = provider.recover_completed_invocation(completed_request)
    assert recovered_after_restart == recovered
    assert len([call for call in transport.calls if call[0] == "call_tool"]) == len(wire_calls)

    receipts.items[0]["operation_id"] = "operation-drifted"
    with pytest.raises(ToolProviderFailure) as drift_error:
        provider.invoke(completed_request)
    assert drift_error.value.error_code == "mcp.receipt_identity_drift"
    assert drift_error.value.effect_certainty == "unknown"
    assert len([call for call in transport.calls if call[0] == "call_tool"]) == len(wire_calls)

    receipts.items[0]["operation_id"] = "operation-5"
    failed_receipt_request = dict(request)
    failed_receipt_request.update({
        "tool_call_id": "call-6",
        "operation_id": "operation-6",
        "idempotency_key": "operation-6:call-6",
    })
    transport.result = {
        **transport.result,
        "structuredContent": {
            **transport.result["structuredContent"],
            "operation_id": "operation-6",
            "invocation_id": "call-6",
        },
    }
    receipts.write_error = RuntimeError("simulated receipt outage")
    with pytest.raises(ToolProviderFailure) as receipt_error:
        provider.invoke(failed_receipt_request)
    assert receipt_error.value.error_code == "mcp.receipt_unavailable"
    assert receipt_error.value.effect_certainty == "unknown"
    failed_receipt_wire_count = len(
        [call for call in transport.calls if call[0] == "call_tool"]
    )

    receipts.write_error = None
    with pytest.raises(ToolProviderFailure) as failed_receipt_retry:
        provider.invoke(failed_receipt_request)
    assert failed_receipt_retry.value.error_code == "mcp.side_effect_indeterminate"
    assert len([call for call in transport.calls if call[0] == "call_tool"]) == failed_receipt_wire_count


def test_remote_status_tool_is_reviewed_but_hidden_from_model_registry() -> None:
    target, status = _remote_status_policies()
    transport = _Transport([{"tools": [
        _descriptor(target.tool_name), _remote_status_descriptor(status),
    ]}])
    registry = ScopedCapabilityRegistry()
    connection = _connection(transport, policies=(target, status), registry=registry)

    definitions = connection.connect()

    assert [item.capability_id for item in definitions] == [target.tool_id]
    assert registry.resolve(target.tool_id) is not None
    assert registry.resolve(status.tool_id) is None


@pytest.mark.parametrize("mutation", ["missing", "catalog_missing", "side_effect", "approval", "schema_drift"])
def test_invalid_remote_status_policy_or_descriptor_fails_closed(mutation: str) -> None:
    target, status = _remote_status_policies()
    if mutation == "missing":
        policies = (target,)
        descriptors = [_descriptor(target.tool_name)]
    elif mutation == "catalog_missing":
        policies = (target, status)
        descriptors = [_descriptor(target.tool_name)]
    elif mutation == "side_effect":
        status = replace(
            status,
            effect="write",
            receipt_schema_uri="crp://status-receipt",
            operation_semantics="receipt_required",
            requires_approval=True,
            remote_receipt_field="operation_id",
            reviewed_receipt_schema=target.reviewed_receipt_schema,
        )
        policies = (target, status)
        descriptors = [_descriptor(target.tool_name), _remote_status_descriptor(status)]
    elif mutation == "approval":
        status = replace(status, requires_approval=True)
        policies = (target, status)
        descriptors = [_descriptor(target.tool_name), _remote_status_descriptor(status)]
    else:
        policies = (target, status)
        descriptors = [
            _descriptor(target.tool_name),
            _descriptor(status.tool_name, inputSchema=status.reviewed_input_schema,
                        outputSchema={"type": "object"}),
        ]
    transport = _Transport([{"tools": descriptors}])
    with pytest.raises(MCPHostError):
        connection = _connection(transport, policies=policies)
        connection.connect()


def test_remote_status_applied_recovers_receipt_without_target_rewire() -> None:
    target, status_policy = _remote_status_policies()
    status_policy = replace(status_policy, timeout_ms=1_234)
    transport = _Transport([{"tools": [
        _descriptor(target.tool_name), _remote_status_descriptor(status_policy),
    ]}])
    receipts = _Receipts()
    receipts.intents[("turn-remote", "call-remote")] = {
        "schema_version": "1.0.0", "turn_id": "turn-remote",
        "invocation_id": "call-remote", "operation_id": "operation-remote",
        "idempotency_key": "operation-remote:call-remote", "attempt": 1,
        "server_id": "calendar-server", "tool_id": target.tool_id,
        "tool_name": target.tool_name, "protocol_version": "2025-11-25",
        "lease_ttl_seconds": 14,
    }
    registry = ScopedCapabilityRegistry()
    connection = _connection(
        transport, policies=(target, status_policy), registry=registry, receipts=receipts,
    )
    connection.connect()
    definition, provider = registry.resolve(target.tool_id)
    request = {
        "tool_call_id": "call-remote", "turn_id": "turn-remote",
        "operation_id": "operation-remote",
        "idempotency_key": "operation-remote:call-remote", "attempt": 1,
        "arguments": {"title": "private"}, "execution_context": _Control(),
        "tool_contract": tool_contract_identity(definition.tool_definition),
    }
    status = {
        "schema_version": "1.0.0", "server_id": "calendar-server",
        "tool_name": target.tool_name, "tool_id": target.tool_id,
        "turn_id": "turn-remote", "invocation_id": "call-remote",
        "operation_id": "operation-remote",
        "idempotency_key": "operation-remote:call-remote",
        "effect_certainty": "confirmed_applied",
        "remote_receipt": {
            "operation_id": "operation-remote", "invocation_id": "call-remote",
            "tool_name": target.tool_name, "server_id": "calendar-server",
        },
    }
    transport.result = {"structuredContent": status}

    recovered = provider.recover_completed_invocation(request)

    assert recovered["receipt_ref"].endswith("mcp-tool-receipt-call-remote")
    calls = [value for name, value in transport.calls if name == "call_tool"]
    assert len(calls) == 1
    assert calls[0][0] == status_policy.tool_name
    assert calls[0][2] == 1_234
    assert calls[0][1] == {key: status[key] for key in (
        "schema_version", "server_id", "tool_name", "tool_id", "turn_id",
        "invocation_id", "operation_id", "idempotency_key",
    )}
    assert calls[0][5] == {}


def test_remote_status_probe_records_confirmed_none_without_target_replay() -> None:
    target, status_policy = _remote_status_policies()
    transport = _Transport([{"tools": [
        _descriptor(target.tool_name), _remote_status_descriptor(status_policy),
    ]}])
    receipts = _Receipts()
    registry = ScopedCapabilityRegistry()
    connection = _connection(
        transport, policies=(target, status_policy), registry=registry, receipts=receipts,
    )
    connection.connect()
    definition, provider = registry.resolve(target.tool_id)
    request = {
        "tool_call_id": "call-probe-none", "turn_id": "turn-probe-none",
        "operation_id": "operation-probe-none",
        "idempotency_key": "operation-probe-none:call-probe-none",
        "attempt": 1, "arguments": {"title": "private"},
        "execution_context": _Control(),
        "tool_contract": tool_contract_identity(definition.tool_definition),
    }
    status = {
        "schema_version": "1.0.0", "server_id": "calendar-server",
        "tool_name": target.tool_name, "tool_id": target.tool_id,
        "turn_id": "turn-probe-none", "invocation_id": "call-probe-none",
        "operation_id": "operation-probe-none",
        "idempotency_key": "operation-probe-none:call-probe-none",
        "effect_certainty": "confirmed_none",
    }
    transport.result = {"structuredContent": status}

    result = provider.probe_completed_invocation(request)

    assert result == {
        "summary": "MCP remote status confirmed no target effect",
        "effect_certainty": "confirmed_none",
        "probe_ref": "crp://default/mcp-probes/call-probe-none",
    }
    calls = [value for name, value in transport.calls if name == "call_tool"]
    assert [call[0] for call in calls] == [status_policy.tool_name]
    assert receipts.replay_claims == {}
    assert receipts.probe_claims[("turn-probe-none", "call-probe-none")] == {
        **{key: status[key] for key in (
            "schema_version", "server_id", "tool_name", "tool_id", "turn_id",
            "invocation_id", "operation_id", "idempotency_key", "effect_certainty",
        )},
        "attempt": 1,
    }


@pytest.mark.parametrize("certainty", ["confirmed_none", "confirmed_none_claimed", "unknown"])
def test_remote_status_only_rewires_same_envelope_after_exact_confirmed_none(certainty: str) -> None:
    target, status_policy = _remote_status_policies()
    transport = _Transport([{"tools": [
        _descriptor(target.tool_name), _remote_status_descriptor(status_policy),
    ]}])
    receipts = _Receipts()
    registry = ScopedCapabilityRegistry()
    connection = _connection(
        transport, policies=(target, status_policy), registry=registry, receipts=receipts,
    )
    connection.connect()
    definition, provider = registry.resolve(target.tool_id)
    request = {
        "tool_call_id": "call-none", "turn_id": "turn-none",
        "operation_id": "operation-none", "idempotency_key": "operation-none:call-none",
        "attempt": 1, "arguments": {"title": "private"},
        "execution_context": _Control(),
        "tool_contract": tool_contract_identity(definition.tool_definition),
    }
    intent = {
        "schema_version": "1.0.0", "turn_id": "turn-none",
        "invocation_id": "call-none", "operation_id": "operation-none",
        "idempotency_key": "operation-none:call-none", "attempt": 1,
        "server_id": "calendar-server", "tool_id": target.tool_id,
        "tool_name": target.tool_name, "protocol_version": "2025-11-25",
        "capability_id": None, "capability_version": None,
        "authorization_facts_ref": None, "authorization_facts_revision": None,
        "approval_fact_ref": None, "execution_mode": None,
        "resource_locks": [], "timeout_ms": None,
        "tool_contract": tool_contract_identity(definition.tool_definition),
        "lease_ttl_seconds": 14,
    }
    receipts.intents[("turn-none", "call-none")] = intent
    if certainty == "confirmed_none_claimed":
        receipts.replay_claims[("turn-none", "call-none")] = {
            "schema_version": "1.0.0", "server_id": "calendar-server",
            "tool_name": target.tool_name, "tool_id": target.tool_id,
            "turn_id": "turn-none", "invocation_id": "call-none",
            "operation_id": "operation-none",
            "idempotency_key": "operation-none:call-none",
            "effect_certainty": "confirmed_none", "attempt": 1,
        }
    status = {
        "schema_version": "1.0.0", "server_id": "calendar-server",
        "tool_name": target.tool_name, "tool_id": target.tool_id,
        "turn_id": "turn-none", "invocation_id": "call-none",
        "operation_id": "operation-none", "idempotency_key": "operation-none:call-none",
        "effect_certainty": "confirmed_none" if certainty == "confirmed_none_claimed" else certainty,
    }
    target_receipt = {
        "operation_id": "operation-none", "invocation_id": "call-none",
        "tool_name": target.tool_name, "server_id": "calendar-server",
    }
    results = iter([
        {"structuredContent": status},
        {"structuredContent": target_receipt},
    ])

    def call_tool(name, arguments, **kwargs):
        transport.calls.append(("call_tool", (
            name, dict(arguments), kwargs["timeout_ms"], kwargs["execution_control"],
            dict(kwargs.get("parameter_headers") or {}),
            dict(kwargs.get("invocation_envelope") or {}),
        )))
        return next(results)

    transport.call_tool = call_tool
    result = provider.recover_completed_invocation(request)
    calls = [value for name, value in transport.calls if name == "call_tool"]
    if certainty in {"unknown", "confirmed_none_claimed"}:
        assert result is None
        assert len(calls) == 1
    else:
        assert result["receipt_ref"].endswith("mcp-tool-receipt-call-none")
        assert len(calls) == 2
        assert calls[1][0] == target.tool_name
        assert calls[1][5] == {
            "turn_id": "turn-none", "invocation_id": "call-none",
            "operation_id": "operation-none",
            "idempotency_key": "operation-none:call-none", "attempt": 1,
        }


def test_response_loss_is_recovered_after_host_restart_from_durable_remote_status() -> None:
    target, status_policy = _remote_status_policies()
    descriptors = [_descriptor(target.tool_name), _remote_status_descriptor(status_policy)]
    remote: dict[str, object] = {"effects": 0, "receipts": {}}

    class DurableRemoteTransport(_Transport):
        def __init__(self, *, lose_target_response: bool) -> None:
            super().__init__([{"tools": descriptors}])
            self.lose_target_response = lose_target_response

        def call_tool(self, name, arguments, **kwargs):
            envelope = dict(kwargs.get("invocation_envelope") or {})
            self.calls.append(("call_tool", (
                name, dict(arguments), kwargs["timeout_ms"], kwargs["execution_control"],
                dict(kwargs.get("parameter_headers") or {}), envelope,
            )))
            receipts = remote["receipts"]
            if name == target.tool_name:
                key = envelope["idempotency_key"]
                if key not in receipts:
                    remote["effects"] += 1
                    receipts[key] = {
                        "operation_id": envelope["operation_id"],
                        "invocation_id": envelope["invocation_id"],
                        "tool_name": target.tool_name,
                        "server_id": "calendar-server",
                    }
                if self.lose_target_response:
                    raise TimeoutError("response lost after durable remote apply")
                return {"structuredContent": receipts[key]}
            key = arguments["idempotency_key"]
            receipt = receipts.get(key)
            return {"structuredContent": {
                **dict(arguments),
                "effect_certainty": "confirmed_applied" if receipt is not None else "confirmed_none",
                **({"remote_receipt": receipt} if receipt is not None else {}),
            }}

    receipts = _Receipts()
    first_registry = ScopedCapabilityRegistry()
    first_transport = DurableRemoteTransport(lose_target_response=True)
    first = _connection(
        first_transport, policies=(target, status_policy),
        registry=first_registry, receipts=receipts,
    )
    first.connect()
    definition, provider = first_registry.resolve(target.tool_id)
    request = {
        "tool_call_id": "call-loss", "turn_id": "turn-loss",
        "operation_id": "operation-loss", "idempotency_key": "operation-loss:call-loss",
        "attempt": 1, "arguments": {"title": "private"},
        "execution_context": _Control(),
        "tool_contract": tool_contract_identity(definition.tool_definition),
    }
    with pytest.raises(ToolProviderFailure) as lost:
        provider.invoke(request)
    assert lost.value.effect_certainty == "unknown"
    assert remote["effects"] == 1
    assert receipts.items == []

    restarted_registry = ScopedCapabilityRegistry()
    restarted_transport = DurableRemoteTransport(lose_target_response=False)
    restarted = _connection(
        restarted_transport, policies=(target, status_policy),
        registry=restarted_registry, receipts=receipts,
    )
    restarted.connect()
    restarted_definition, restarted_provider = restarted_registry.resolve(target.tool_id)
    restarted_request = {
        **request,
        "tool_contract": tool_contract_identity(restarted_definition.tool_definition),
    }

    recovered = restarted_provider.recover_completed_invocation(restarted_request)

    assert recovered["receipt_ref"].endswith("mcp-tool-receipt-call-loss")
    assert remote["effects"] == 1
    assert [call[1][0] for call in restarted_transport.calls if call[0] == "call_tool"] == [
        status_policy.tool_name,
    ]


def test_provider_preserves_dispatch_deadline_signal() -> None:
    transport = _Transport([{"tools": [_descriptor()]}])
    registry = ScopedCapabilityRegistry()
    connection = _connection(transport, registry=registry)
    connection.connect()
    definition, provider = registry.resolve("calendar.search")
    transport.call_error = ToolDispatchDeadlineExceeded(provider_started=True)

    with pytest.raises(ToolDispatchDeadlineExceeded):
        provider.invoke({
            "tool_call_id": "call-deadline", "turn_id": "turn-deadline",
            "operation_id": "operation-deadline", "arguments": {},
            "execution_context": _Control(),
            "tool_contract": tool_contract_identity(definition.tool_definition),
        })


def test_runtime_dispatches_mcp_and_keeps_receipt_out_of_planner_scope() -> None:
    transport = _Transport([{"tools": [_descriptor()]}])
    registry = ScopedCapabilityRegistry()
    payloads = InMemoryTurnPayloadStore()
    connection = _connection(
        transport,
        registry=registry,
        receipts=TurnPayloadMCPReceiptStore(payloads),
    )
    connection.connect()
    planner = _MCPPlanner()
    runtime = SynchronousAIRuntime(
        planner=planner,
        registry=registry,
        events=InMemoryTurnEventStore(),
        payloads=payloads,
    )
    request = json.loads(
        (ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(
            encoding="utf-8"
        )
    )
    request["capability_policy"] = {
        "allowed": ["calendar.search"], "denied": [], "require_approval": [],
    }

    completed = runtime.submit_turn(request)

    assert completed.status == "completed"
    assert planner.receipt_was_scoped_out is True
    events = tuple(runtime.events_after(completed.turn_id))
    tool_completed = next(event for event in events if event["type"] == "tool.completed")
    assert tool_completed["data"]["payload_ref"] is None
    receipt_ref = tool_completed["data"]["receipt_ref"]
    receipt = payloads.get(receipt_ref)
    assert receipt["raw_input_recorded"] is False
    assert receipt["raw_output_recorded"] is False
    assert "private remote result" not in str(receipt)


class _MCPPlanner:
    def __init__(self) -> None:
        self.receipt_was_scoped_out = False

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        completed = next((event for event in reversed(events) if event["type"] == "tool.completed"), None)
        if completed is None:
            return {"type": "tool", "capability_id": "calendar.search", "arguments": {"query": "private"}}
        try:
            payloads.get(completed["data"]["receipt_ref"])
        except Exception:
            self.receipt_was_scoped_out = True
        return {"type": "complete", "summary": "MCP call completed"}
