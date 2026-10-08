from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "src" / "core" / "ai_tooling" / "project_profile.py"


def test_project_capability_contract_is_strict_and_does_not_duplicate_grants() -> None:
    schema = json.loads(
        (ROOT / "core-contracts" / "ai" / "project-capability-profile.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    assert "boundary_profile_id" in schema["required"]
    assert "boundary_profile_revision" in schema["required"]
    assert schema["properties"]["schema_version"]["const"] == "1.3.0"
    assert schema["properties"]["max_tools"]["maximum"] == 12
    assert schema["properties"]["tool_discovery_policy"]["enum"] == [
        "auto_discover", "confirm_new", "disabled",
    ]
    binding = schema["properties"]["tool_selection_bindings"]["items"]
    assert binding["additionalProperties"] is False
    assert binding["required"] == ["stable_id", "contract_identity"]
    mcp_binding = schema["properties"]["mcp_server_selection_bindings"]["items"]
    assert mcp_binding["additionalProperties"] is False
    assert mcp_binding["required"] == [
        "server_id", "protocol_profile", "manifest_revision",
        "endpoint_identity", "credential_subject_id", "transport_generation",
    ]
    assert "persistent_grants" not in schema["properties"]


def test_effective_tool_resolution_is_framework_model_and_provider_neutral() -> None:
    text = MODULE.read_text(encoding="utf-8")
    for forbidden in (
        "fastapi",
        "backend.",
        "LiteLLM",
        "ModelGateway",
        "httpx",
        "ProviderRegistry",
        "secret_store",
    ):
        assert forbidden not in text
    assert "turn_restricted" in text
    assert "plugin_disabled" in text
    assert "mcp_disabled" in text
    assert "mcp_authority_unbound" in text
    assert "mcp_authority_drift" in text
    assert "descriptor_budget" in text
