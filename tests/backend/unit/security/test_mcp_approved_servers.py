from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from backend.security.mcp_approved_servers import (
    JsonMCPApprovedServerStore,
    MCPApprovedServerStoreError,
)


def test_missing_file_returns_immutable_empty_snapshot(tmp_path: Path) -> None:
    snapshot = JsonMCPApprovedServerStore(tmp_path).snapshot()
    assert snapshot.enabled_servers == ()
    assert snapshot.get_approved("calendar-server") is None


def test_snapshot_loads_read_only_approved_server_once_and_is_immutable(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path, _payload())
    store = JsonMCPApprovedServerStore(tmp_path)
    calls = 0
    original = Path.read_bytes

    def read_once(path):
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_once)
    snapshot = store.snapshot()
    manifest = snapshot.get_approved("calendar-server")
    assert calls == 1
    assert manifest is not None and manifest.server_id == "calendar-server"
    assert len(snapshot.enabled_servers) == 1
    assert snapshot.enabled_servers[0].tool_policies[0].effect == "read"
    with pytest.raises(TypeError):
        snapshot._records["other"] = snapshot.enabled_servers[0]  # type: ignore[index]


def test_unified_authority_loads_reviewed_streamable_http_without_exposing_stdio(tmp_path: Path) -> None:
    legacy = _payload()["servers"][0]
    payload = {
        "schema_version": "1.1.0",
        "servers": [{
            "server_id": legacy["server_id"], "enabled": True,
            "approval_status": "approved", "approval_revision": 1,
            "transport_kind": "streamable_http",
            "host_connection": legacy["host_connection"],
            "connection_manifest": {
                "server_id": "calendar-server", "manifest_revision": 1,
                "endpoint_identity": "calendar-endpoint", "credential_subject_id": "calendar-user",
                "transport_generation": 1, "approval_revision": 1, "approval_status": "approved",
                "endpoint_url": "https://mcp.example.test/rpc", "headers": {"X-Client": "chriptmas"},
                "secret_header_refs": {"Authorization": "mcp:calendar-server:token"},
                "timeout_seconds": 20, "max_response_bytes": 4194304, "max_sse_events": 256,
            },
            "tool_policies": legacy["tool_policies"],
        }],
    }
    path = tmp_path / ".rebuild-data" / "security" / "mcp-approved-servers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    snapshot = JsonMCPApprovedServerStore(tmp_path).snapshot()
    assert snapshot.get_approved("calendar-server") is None
    assert snapshot.get_approved_http("calendar-server").endpoint_url == "https://mcp.example.test/rpc"
    assert snapshot.enabled_servers[0].transport_kind == "streamable_http"


def test_profiled_authority_requires_matching_immutable_2026_profile(tmp_path: Path) -> None:
    legacy = _payload()["servers"][0]
    record = {
        "server_id": legacy["server_id"], "enabled": True,
        "approval_status": "approved", "approval_revision": 1,
        "transport_kind": "stdio", "protocol_profile": "stateless_2026_07_28",
        "host_connection": {**legacy["host_connection"], "protocol_profile": "stateless_2026_07_28"},
        "connection_manifest": {**legacy["launch_manifest"], "protocol_profile": "stateless_2026_07_28"},
        "tool_policies": legacy["tool_policies"],
    }
    payload = {"schema_version": "1.2.0", "servers": [record]}
    path = tmp_path / ".rebuild-data" / "security" / "mcp-approved-servers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    snapshot = JsonMCPApprovedServerStore(tmp_path).snapshot()
    assert snapshot.enabled_servers[0].host_connection.protocol_profile == "stateless_2026_07_28"
    assert snapshot.enabled_servers[0].launch_manifest.protocol_profile == "stateless_2026_07_28"

    record["connection_manifest"]["protocol_profile"] = "legacy_2025_11_25"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(MCPApprovedServerStoreError, match="protocol profile drifted"):
        JsonMCPApprovedServerStore(tmp_path).snapshot()

    record["connection_manifest"]["protocol_profile"] = "stateless_2026_07_28"
    record["tool_policies"][0]["reviewed_input_schema"] = {
        "type": "object", "properties": {"unsafe": {"type": "string", "x-mcp-header": "X-Unsafe"}},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    snapshot = JsonMCPApprovedServerStore(tmp_path).snapshot()
    # Stateless stdio has no HTTP field channel.  It retains the reviewed
    # policy and ignores annotations rather than excluding the tool.
    assert snapshot.enabled_servers[0].tool_policies[0].parameter_headers({"unsafe": "ok"}) == {}


def test_stateless_authority_excludes_only_policy_with_invalid_header_projection(tmp_path: Path) -> None:
    legacy = _payload()["servers"][0]
    invalid = dict(legacy["tool_policies"][0])
    invalid["tool_name"] = "calendar.invalid"
    invalid["tool_id"] = "calendar.invalid"
    invalid["reviewed_input_schema"] = {
        "type": "object", "items": {"type": "string", "x-mcp-header": "Bad"},
    }
    valid = dict(legacy["tool_policies"][0])
    valid["tool_name"] = "calendar.valid"
    valid["tool_id"] = "calendar.valid"
    valid["reviewed_input_schema"] = {
        "type": "object", "properties": {"count": {"type": "integer", "x-mcp-header": "Count"}},
    }
    record = {
        "server_id": legacy["server_id"], "enabled": True, "approval_status": "approved", "approval_revision": 1,
        "transport_kind": "streamable_http", "protocol_profile": "stateless_2026_07_28",
        "host_connection": {**legacy["host_connection"], "protocol_profile": "stateless_2026_07_28"},
        "connection_manifest": {
            "server_id": "calendar-server", "manifest_revision": 1,
            "endpoint_identity": "calendar-endpoint", "credential_subject_id": "calendar-user",
            "transport_generation": 1, "approval_revision": 1, "approval_status": "approved",
            "endpoint_url": "https://mcp.example.test/rpc", "headers": None, "secret_header_refs": None,
            "timeout_seconds": 20, "max_response_bytes": 4194304, "max_sse_events": 256,
            "protocol_profile": "stateless_2026_07_28",
        },
        "tool_policies": [invalid, valid],
    }
    path = tmp_path / ".rebuild-data" / "security" / "mcp-approved-servers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": "1.2.0", "servers": [record]}), encoding="utf-8")
    snapshot = JsonMCPApprovedServerStore(tmp_path).snapshot()
    assert [policy.tool_name for policy in snapshot.enabled_servers[0].tool_policies] == ["calendar.valid"]
    # The rejected policy stays anonymous even inside the authority snapshot.
    assert snapshot.enabled_servers[0].header_policy_rejected_count == 1


def test_stateless_http_rejects_duplicate_policy_identity_before_header_filtering(tmp_path: Path) -> None:
    legacy = _payload()["servers"][0]
    invalid = dict(legacy["tool_policies"][0])
    invalid["reviewed_input_schema"] = {"type": "object", "items": {"x-mcp-header": "Bad"}}
    valid = dict(legacy["tool_policies"][0])
    valid["reviewed_input_schema"] = {"type": "object"}
    record = {
        "server_id": legacy["server_id"], "enabled": True, "approval_status": "approved", "approval_revision": 1,
        "transport_kind": "streamable_http", "protocol_profile": "stateless_2026_07_28",
        "host_connection": {**legacy["host_connection"], "protocol_profile": "stateless_2026_07_28"},
        "connection_manifest": {
            "server_id": "calendar-server", "manifest_revision": 1, "endpoint_identity": "calendar-endpoint",
            "credential_subject_id": "calendar-user", "transport_generation": 1, "approval_revision": 1,
            "approval_status": "approved", "endpoint_url": "https://mcp.example.test/rpc", "headers": None,
            "secret_header_refs": None, "timeout_seconds": 20, "max_response_bytes": 4194304,
            "max_sse_events": 256, "protocol_profile": "stateless_2026_07_28",
        }, "tool_policies": [invalid, valid],
    }
    path = tmp_path / ".rebuild-data" / "security" / "mcp-approved-servers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": "1.2.0", "servers": [record]}), encoding="utf-8")
    with pytest.raises(MCPApprovedServerStoreError, match="duplicated"):
        JsonMCPApprovedServerStore(tmp_path).snapshot()


def test_authority_migration_fails_closed_while_legacy_and_unified_files_coexist(tmp_path: Path) -> None:
    _write(tmp_path, _payload())
    unified = tmp_path / ".rebuild-data" / "security" / "mcp-approved-servers.json"
    unified.write_text(json.dumps({"schema_version": "1.1.0", "servers": []}), encoding="utf-8")
    with pytest.raises(MCPApprovedServerStoreError, match="migration is incomplete"):
        JsonMCPApprovedServerStore(tmp_path).snapshot()


@pytest.mark.parametrize("mutate", [
    lambda value: value.update({"extra": True}),
    lambda value: value["servers"].append(dict(value["servers"][0])),
    lambda value: value["servers"][0].update({"approval_status": "pending"}),
    lambda value: value["servers"][0]["host_connection"].update({"endpoint_identity": "drifted"}),
    lambda value: value["servers"][0]["launch_manifest"].update({"environment": {"API_KEY": "private"}}),
    lambda value: value["servers"][0]["tool_policies"][0].update({"reviewed_input_schema": {"type": "array"}}),
])
def test_invalid_authority_records_fail_closed_without_leaking_details(tmp_path: Path, mutate) -> None:
    payload = _payload()
    mutate(payload)
    _write(tmp_path, payload)
    with pytest.raises(MCPApprovedServerStoreError) as error:
        JsonMCPApprovedServerStore(tmp_path).load()
    message = str(error.value)
    assert str(Path(sys.executable)) not in message
    assert "private" not in message and "API_KEY" not in message


def test_rejects_secret_like_launch_value_invalid_schema_and_oversized_file(tmp_path: Path) -> None:
    payload = _payload()
    # Secret references are identifiers, not secret literals.  A malformed
    # value must fail before it can become process environment material.
    payload["servers"][0]["launch_manifest"]["secret_env_refs"] = {"MCP_TOKEN": "literal secret value"}
    _write(tmp_path, payload)
    with pytest.raises(MCPApprovedServerStoreError):
        JsonMCPApprovedServerStore(tmp_path).snapshot()

    payload = _payload()
    payload["servers"][0]["launch_manifest"]["secret_env_refs"] = {
        "MCP_TOKEN": "mcp:calendar-server:",
    }
    _write(tmp_path, payload)
    with pytest.raises(MCPApprovedServerStoreError, match="identity drifted"):
        JsonMCPApprovedServerStore(tmp_path).snapshot()

    path = _path(tmp_path)
    path.write_bytes(b"{" + b"x" * (513 * 1024))
    with pytest.raises(MCPApprovedServerStoreError, match="exceeds limits"):
        JsonMCPApprovedServerStore(tmp_path).snapshot()


@pytest.mark.parametrize(("marker", "duplicate"), [
    ('"schema_version":"1.0.0"', '"schema_version":"reviewed","schema_version":"1.0.0"'),
    ('"enabled":true', '"enabled":false,"enabled":true'),
    ('"catalog_revision":1', '"catalog_revision":9,"catalog_revision":1'),
    ('"executable":', '"executable":"private-command","executable":'),
    ('"display_name":"Search calendar"', '"display_name":"private-label","display_name":"Search calendar"'),
])
def test_rejects_duplicate_json_fields_at_every_authority_level(
    tmp_path: Path,
    marker: str,
    duplicate: str,
) -> None:
    serialized = json.dumps(_payload(), separators=(",", ":"))
    assert marker in serialized
    path = _path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized.replace(marker, duplicate, 1), encoding="utf-8")

    with pytest.raises(MCPApprovedServerStoreError) as error:
        JsonMCPApprovedServerStore(tmp_path).snapshot()

    message = str(error.value)
    assert message == "MCP approved server authority contains duplicate fields"
    assert "private" not in message


def _payload() -> dict[str, object]:
    return {
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
                "transport_generation": 1, "approval_revision": 1, "approval_status": "approved",
                "executable": str(Path(sys.executable).resolve()), "argv": [], "cwd": None,
                "environment": {"NO_COLOR": "1"},
                "secret_env_refs": {"MCP_TOKEN": "mcp:calendar-server:token"},
            },
            "tool_policies": [{
                "tool_name": "calendar.search", "tool_id": "calendar.search", "version": 1,
                "display_name": "Search calendar", "description": "Reviewed search",
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


def test_rejects_dangling_approved_verification_tool_policy(tmp_path: Path) -> None:
    payload = _payload()
    policy = payload["servers"][0]["tool_policies"][0]
    policy["idempotency"] = "verify_before_retry"
    policy["verification_tool_id"] = "calendar.effect-status"
    _write(tmp_path, payload)

    with pytest.raises(MCPApprovedServerStoreError) as error:
        JsonMCPApprovedServerStore(tmp_path).snapshot()

    assert str(error.value) == "MCP approved verification tool policy is invalid"


def _path(root: Path) -> Path:
    return root / ".rebuild-data" / "security" / "mcp-approved-stdio.json"


def _write(root: Path, value: object) -> None:
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
