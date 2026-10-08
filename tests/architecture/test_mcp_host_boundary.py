from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / "src" / "core" / "mcp_host" / "host.py"
STDIO = ROOT / "src" / "core" / "mcp_host" / "stdio_transport.py"
STDIO_CONFIG = ROOT / "src" / "core" / "mcp_host" / "stdio_config.py"


def test_mcp_host_core_is_transport_and_product_neutral() -> None:
    text = HOST.read_text(encoding="utf-8")
    for required in (
        "class MCPTransportPort",
        "class MCPHostConnection",
        "class MCPToolPolicy",
        "class TurnPayloadMCPReceiptStore",
        "tool_matches_contract_identity",
        "execution_control=context",
    ):
        assert required in text
    for forbidden in (
        "backend.api",
        "fastapi",
        "httpx",
        "subprocess",
        "secret_store",
        "ProviderRegistry",
    ):
        assert forbidden not in text


def test_mcp_host_never_projects_remote_body_as_generic_tool_result() -> None:
    text = HOST.read_text(encoding="utf-8")
    provider = text[text.index("class _MCPToolProvider") : text.index("def _policy_map")]
    assert '"raw_input_recorded": False' in provider
    assert '"raw_output_recorded": False' in provider
    assert '"result": result' not in provider
    assert '"payload_ref"' not in provider
    assert 'MCP tool completed; remote output is isolated' in provider

    boundary_adapter = (
        ROOT / "src" / "backend" / "security" / "turn_boundary_adapter.py"
    ).read_text(encoding="utf-8")
    assert "tool_boundary_target_identity(tool, capability_id)" in boundary_adapter


def test_tool_contract_schemas_accept_connection_identity_without_secrets() -> None:
    for name in ("tool-definition.schema.json", "tool-invocation-intent.schema.json"):
        schema = json.loads((ROOT / "core-contracts" / "ai" / name).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False
        serialized = json.dumps(schema)
        assert "connection_identity" in serialized
        for forbidden in ("endpoint_url", "command", "api_key", "access_token", "credential_value"):
            assert forbidden not in serialized


def test_stdio_transport_keeps_process_and_secret_details_out_of_diagnostics() -> None:
    transport = STDIO.read_text(encoding="utf-8")
    config = STDIO_CONFIG.read_text(encoding="utf-8")
    assert "shell=False" in transport
    assert "stderr=subprocess.DEVNULL" in transport
    assert "queue.Queue(maxsize=" in transport
    assert "raise MCPStdioTransportError(message) from None" in transport
    assert "field(default=None, repr=False)" in config
    assert "MCP stdio launch requires manifest authority" in config
    assert "MCP constant environment is not allowlisted" in config
    assert "config.assert_matches(host_connection)" in transport
    for forbidden in ("stderr.read", "stdout.decode", "logger.", "logging.", "shell=True"):
        assert forbidden not in transport
