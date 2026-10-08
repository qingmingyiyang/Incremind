from __future__ import annotations

import json

from core.external_extensions import (
    ArtifactInventory,
    ResolvedSource,
    derive_review_plan,
    inspect_extension,
)


def _source(*, immutable: bool = True, trust_tier: str = "reviewed_source") -> ResolvedSource:
    return ResolvedSource(
        source_kind="github_repository",
        canonical_locator="https://github.com/example/fixture",
        immutable_revision="a" * 40 if immutable else None,
        artifact_ref="crp://extension-artifacts/fixture/revision-1",
        trust_tier=trust_tier,
    )


def test_pinned_content_only_skill_needs_no_redundant_confirmation() -> None:
    inventory = ArtifactInventory.capture(
        {"SKILL.md": b"---\nname: safe-skill\ndescription: Safe content skill.\n---\nReturn safe.\n"}
    )
    plan = derive_review_plan(inspect_extension(inventory, _source()).manifest)

    assert plan.disposition == "AUTO_WITH_NOTICE"
    assert plan.install_projection == "ready_content_skill"
    assert plan.activation_routes == ("application_skill_import",)
    assert plan.confirmation_ids == ()
    assert plan.rollback_action == "disable_content_projection"


def test_mutable_or_script_skill_is_installed_disabled_and_asks_once_for_exact_risks() -> None:
    inventory = ArtifactInventory.capture(
        {
            "SKILL.md": b"---\nname: scripted-skill\ndescription: Scripted skill.\n---\nUse script.\n",
            "scripts/run.py": b"raise RuntimeError('never run during preview')\n",
        }
    )
    plan = derive_review_plan(inspect_extension(inventory, _source(immutable=False)).manifest)

    assert plan.disposition == "ASK"
    assert plan.install_projection == "installed_disabled"
    assert plan.confirmation_ids == ("activate_external_extension", "allow_subprocess")
    assert plan.risk_codes == ("mutable_source", "script_resource")


def test_first_untrusted_content_skill_requires_one_content_activation_confirmation() -> None:
    inventory = ArtifactInventory.capture(
        {"SKILL.md": b"---\nname: first-skill\ndescription: Untrusted content.\n---\nDo a thing.\n"}
    )
    plan = derive_review_plan(
        inspect_extension(inventory, _source(trust_tier="untrusted")).manifest
    )

    assert plan.disposition == "ASK"
    assert plan.install_projection == "installed_disabled"
    assert plan.confirmation_ids == ("activate_external_extension",)
    assert plan.risk_codes == ("external_activation",)


def test_mcp_review_collects_network_oauth_and_subprocess_confirmations_without_values() -> None:
    inventory = ArtifactInventory.capture(
        {
            ".mcp.json": json.dumps(
                {
                    "mcpServers": {
                        "stdio": {"command": "fixture"},
                        "remote": {"url": "https://mcp.example.test/rpc", "oauth": {}},
                    }
                }
            ).encode()
        }
    )
    plan = derive_review_plan(inspect_extension(inventory, _source()).manifest)

    assert plan.disposition == "ASK"
    assert plan.activation_routes == ("mcp_server_review",)
    assert plan.confirmation_ids == (
        "activate_external_extension",
        "allow_network_destinations",
        "allow_oauth",
        "allow_subprocess",
    )
    assert "mcp_initialize_probe" in plan.health_checks


def test_quarantined_manifest_cannot_be_approved_into_disabled_install() -> None:
    inventory = ArtifactInventory.capture(
        {
            ".codex-plugin/plugin.json": b'{"name":"fixture-plugin","version":"1.0.0","unknown":true}'
        }
    )
    plan = derive_review_plan(inspect_extension(inventory, _source()).manifest)

    assert plan.disposition == "REJECT"
    assert plan.install_projection == "quarantined"
    assert plan.confirmation_ids == ()
    assert plan.activation_routes == ("plugin_review",)


def test_composite_plugin_retains_hook_mcp_and_plugin_health_routes() -> None:
    inventory = ArtifactInventory.capture(
        {
            ".codex-plugin/plugin.json": b'{"name":"composite-plugin","version":"1.0.0"}',
            "hooks/hooks.json": b'{"hooks":{"PreToolUse":[]}}',
            "mcp/server-ref.json": b"{}",
            "hands/runner/descriptor.json": b"{}",
        }
    )
    plan = derive_review_plan(inspect_extension(inventory, _source()).manifest)

    assert plan.activation_routes == ("hook_review", "mcp_server_review", "plugin_review")
    assert {"hook_dry_event", "mcp_initialize_probe", "plugin_manifest_health"} <= set(plan.health_checks)


def test_mcp_credential_references_require_confirmation_and_literal_values_quarantine() -> None:
    referenced = ArtifactInventory.capture(
        {
            ".mcp.json": b'{"mcpServers":{"remote":{"url":"https://mcp.example.test/rpc","bearer_token_env_var":"MCP_TOKEN"}}}'
        }
    )
    referenced_plan = derive_review_plan(inspect_extension(referenced, _source()).manifest)
    assert "allow_credential_references" in referenced_plan.confirmation_ids

    literal = ArtifactInventory.capture(
        {
            ".mcp.json": b'{"mcpServers":{"remote":{"url":"https://mcp.example.test/rpc","http_headers":{"Authorization":"Bearer leak"}}}}'
        }
    )
    literal_plan = derive_review_plan(inspect_extension(literal, _source()).manifest)
    assert literal_plan.disposition == "REJECT"
    assert "literal_header_value" in literal_plan.risk_codes
