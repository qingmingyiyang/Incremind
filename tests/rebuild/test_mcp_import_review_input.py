from __future__ import annotations

import json

import pytest

from core.external_extensions import (
    ArtifactInventory,
    MCPImportReviewInputError,
    ResolvedSource,
    derive_mcp_import_review_inputs,
    derive_review_plan,
    inspect_extension,
)


def _source() -> ResolvedSource:
    return ResolvedSource(
        source_kind="github_repository",
        canonical_locator="https://github.com/example/mcp-package",
        immutable_revision="0123456789abcdef0123456789abcdef01234567",
        artifact_ref="crp://external-extension-artifacts/mcp-fixture",
        trust_tier="reviewed_source",
    )


@pytest.mark.parametrize(
    ("path", "payload"),
    (
        (
            ".mcp.json",
            json.dumps({"mcpServers": {"fixture": {"url": "https://mcp.example.test/rpc"}}}).encode(),
        ),
        (
            "mcp.json",
            json.dumps({"mcp_servers": {"fixture": {"url": "https://mcp.example.test/rpc"}}}).encode(),
        ),
        (
            ".codex/config.toml",
            b'[mcp_servers.fixture]\nurl = "https://mcp.example.test/rpc"\n',
        ),
    ),
)
def test_mcp_dialects_project_the_same_non_executable_review_input(
    path: str, payload: bytes,
) -> None:
    inspected = inspect_extension(ArtifactInventory.capture({path: payload}), _source())
    plan = derive_review_plan(inspected.manifest)

    assert derive_mcp_import_review_inputs(inspected.manifest, plan) == (
        derive_mcp_import_review_inputs(inspected.manifest, plan)[0],
    )
    projected = derive_mcp_import_review_inputs(inspected.manifest, plan)[0]
    assert projected.contribution_id == "fixture"
    assert projected.transport == "http"
    assert projected.activation_route == "mcp_server_review"
    assert "activate_external_extension" in projected.confirmation_ids
    assert "allow_network_destinations" in projected.confirmation_ids
    assert "mcp_initialize_probe" in projected.health_checks
    assert projected.missing_approval_fields == (
        "approval_revision",
        "connection_or_launch_manifest",
        "credential_subject_id",
        "endpoint_identity",
        "tool_policies",
    )
    assert "mcp.example.test" not in repr(projected)


def test_mcp_review_input_retains_no_stdio_command_or_literal_secret() -> None:
    inspected = inspect_extension(
        ArtifactInventory.capture(
            {
                ".mcp.json": json.dumps(
                    {"mcpServers": {"fixture": {"command": "secret-command", "args": ["--token"]}}}
                ).encode()
            }
        ),
        _source(),
    )
    plan = derive_review_plan(inspected.manifest)
    projected = derive_mcp_import_review_inputs(inspected.manifest, plan)[0]

    assert projected.transport == "stdio"
    assert "allow_subprocess" in projected.confirmation_ids
    assert "secret-command" not in repr(projected)
    assert "--token" not in repr(projected)


def test_quarantined_mcp_config_cannot_be_projected_as_approval_work() -> None:
    inspected = inspect_extension(
        ArtifactInventory.capture(
            {".mcp.json": b'{"mcpServers":{"fixture":{"url":"http://mcp.example.test/rpc"}}}'},
        ),
        _source(),
    )

    with pytest.raises(MCPImportReviewInputError, match="not reviewable"):
        derive_mcp_import_review_inputs(inspected.manifest, derive_review_plan(inspected.manifest))
