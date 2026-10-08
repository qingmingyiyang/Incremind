from __future__ import annotations

import json

import pytest

from core.external_extensions import (
    ArtifactInventory,
    ContributionCandidate,
    ExtensionContractError,
    ExtensionDetectionError,
    FrozenRuntimeContract,
    ResolvedSource,
    StaticExtensionDetectorRegistry,
    inspect_extension,
)


def _source(*, immutable: bool = True) -> ResolvedSource:
    return ResolvedSource(
        source_kind="github_repository",
        canonical_locator="https://github.com/example/fixture",
        immutable_revision="a" * 40 if immutable else None,
        artifact_ref="crp://extension-artifacts/fixture/revision-1",
    )


def _skill() -> bytes:
    return b"---\nname: fixture-echo\ndescription: Return a fixed compatibility result.\n---\nReturn fixture-ok.\n"


def test_shared_skill_is_detected_and_normalized_as_content_only() -> None:
    inventory = ArtifactInventory.capture({".agents/skills/fixture-echo/SKILL.md": _skill()})
    result = inspect_extension(inventory, _source())

    assert result.status == "discovered"
    assert result.manifest.source_format == "openai_agent_skill"
    assert [(item.kind, item.contribution_id) for item in result.manifest.contributions] == [
        ("application_skill", "fixture-echo")
    ]
    assert result.manifest.permission_plan.review_required is False
    assert result.manifest.runtime_contract == FrozenRuntimeContract()


def test_codex_skill_directory_is_detected_as_a_content_only_skill() -> None:
    inventory = ArtifactInventory.capture({".codex/skills/fixture-echo/SKILL.md": _skill()})
    result = inspect_extension(inventory, _source())

    assert result.status == "discovered"
    assert result.manifest.source_format == "openai_agent_skill"
    assert [(item.kind, item.contribution_id) for item in result.manifest.contributions] == [
        ("application_skill", "fixture-echo")
    ]


def test_codex_skill_accepts_utf8_bom_and_crlf_and_projects_safe_description() -> None:
    inventory = ArtifactInventory.capture(
        {
            "SKILL.md": b"\xef\xbb\xbf---\r\nname: fixture-echo\r\ndescription: compatible routing\r\n---\r\nDo nothing.\r\n",
        }
    )
    result = inspect_extension(inventory, _source())

    assert result.manifest.contributions[0].metadata == (
        ("description", "compatible routing"),
        ("model_invocable", "true"),
        ("user_invocable", "true"),
    )


def test_skill_without_description_retains_missing_description_issue() -> None:
    result = inspect_extension(
        ArtifactInventory.capture({"SKILL.md": b"---\nname: fixture-echo\n---\nDo nothing.\n"}),
        _source(),
    )

    assert result.status == "quarantined"
    assert [(issue.code, issue.path) for issue in result.manifest.issues] == [
        ("missing_description", "SKILL.md")
    ]
    assert result.manifest.contributions[0].metadata == (
        ("model_invocable", "true"),
        ("user_invocable", "true"),
    )


def test_skill_with_scripts_or_mutable_source_requires_review_without_executing_it() -> None:
    inventory = ArtifactInventory.capture(
        {
            "SKILL.md": _skill(),
            "scripts/sentinel.py": b"raise RuntimeError('must not execute')\n",
        }
    )
    result = inspect_extension(inventory, _source(immutable=False))

    assert result.status == "discovered"
    assert result.manifest.permission_plan.requires_subprocess is True
    assert result.manifest.permission_plan.review_reasons == ("mutable_source", "script_resource")


def test_deepseek_single_file_skill_dialect_is_detected_without_rewriting_source() -> None:
    inventory = ArtifactInventory.capture({".dsh/skills/fixture-echo.md": _skill()})
    result = inspect_extension(inventory, _source())

    assert result.manifest.source_format == "deepseek_harness_skill"
    assert result.manifest.contributions[0].contribution_id == "fixture-echo"


def test_deepseek_directory_skill_projects_routing_metadata_and_invocation_policy() -> None:
    inventory = ArtifactInventory.capture(
        {
            ".dsh/skills/fixture-echo/SKILL.md": b"---\nname: fixture-echo\ndescription: Review a fixture.\nwhenToUse: Use for a compatible review.\nmetadata:\n  owner: fixtures\ndisable-model-invocation: yes\nuser-invocable: off\n---\nDo nothing.\n",
        }
    )
    result = inspect_extension(inventory, _source())

    assert result.manifest.source_format == "deepseek_harness_skill"
    assert result.status == "quarantined"
    assert [issue.code for issue in result.manifest.issues] == [
        "unsupported_invocation_policy"
    ]
    assert result.manifest.contributions[0].metadata == (
        ("description", "Review a fixture."),
        ("when_to_use", "Use for a compatible review."),
        ("model_invocable", "false"),
        ("user_invocable", "false"),
    )


@pytest.mark.parametrize(
    ("disable_model", "user_invocable", "expected_model", "expected_user"),
    [
        ("true", "false", "false", "false"),
        ("yes", "no", "false", "false"),
        ("on", "off", "false", "false"),
        ("1", "0", "false", "false"),
        ("false", "true", "true", "true"),
    ],
)
def test_deepseek_skill_accepts_documented_boolean_spellings(
    disable_model: str, user_invocable: str, expected_model: str, expected_user: str,
) -> None:
    inventory = ArtifactInventory.capture(
        {".dsh/skills/fixture-echo.md": (
            "---\nname: fixture-echo\ndescription: Review a fixture.\n"
            f"disable-model-invocation: {disable_model}\nuser-invocable: {user_invocable}\n---\n"
        ).encode()}
    )
    result = inspect_extension(inventory, _source())

    assert dict(result.manifest.contributions[0].metadata) == {
        "description": "Review a fixture.",
        "model_invocable": expected_model,
        "user_invocable": expected_user,
    }


@pytest.mark.parametrize(
    "frontmatter",
    [
        "disable-model-invocation: perhaps",
        "disableModelInvocation: true",
        "model-invocable: false",
        "userInvocable: false",
    ],
)
def test_deepseek_skill_rejects_invalid_or_legacy_invocation_policy(frontmatter: str) -> None:
    inventory = ArtifactInventory.capture(
        {".dsh/skills/fixture-echo.md": (
            "---\nname: fixture-echo\ndescription: Review a fixture.\n"
            f"{frontmatter}\n---\n"
        ).encode()}
    )

    with pytest.raises(ExtensionContractError, match="invocation|boolean"):
        inspect_extension(inventory, _source())


@pytest.mark.parametrize("closing", ("---junk", "---: value"))
def test_skill_frontmatter_requires_an_exact_closing_delimiter(closing: str) -> None:
    inventory = ArtifactInventory.capture(
        {"SKILL.md": (
            "---\nname: fixture-echo\ndescription: Safe fixture.\n"
            f"{closing}\nThis must not be interpreted as instructions.\n"
        ).encode()}
    )

    with pytest.raises(ExtensionContractError, match="frontmatter is invalid"):
        inspect_extension(inventory, _source())


def test_skill_frontmatter_rejects_complex_metadata_instead_of_interpreting_yaml() -> None:
    inventory = ArtifactInventory.capture(
        {"SKILL.md": b"---\nname: fixture-echo\ndescription: Safe fixture.\nmetadata:\n  owner:\n    nested: unsupported\n---\n"}
    )

    with pytest.raises(ExtensionContractError, match="metadata entry is invalid"):
        inspect_extension(inventory, _source())


def test_codex_plugin_container_takes_precedence_over_nested_skill_markers() -> None:
    plugin = json.dumps(
        {
            "name": "fixture-plugin",
            "version": "0.1.0",
            "description": "Compatibility fixture",
            "skills": "./skills/",
        }
    ).encode()
    inventory = ArtifactInventory.capture(
        {
            ".codex-plugin/plugin.json": plugin,
            "skills/fixture-echo/SKILL.md": _skill(),
        }
    )
    result = inspect_extension(inventory, _source())

    assert result.manifest.source_format == "openai_codex_plugin"
    assert result.status == "discovered"
    assert [(item.kind, item.contribution_id) for item in result.manifest.contributions] == [
        ("plugin_skill", "fixture-echo")
    ]


def test_codex_plugin_does_not_treat_unrelated_nested_skill_files_as_contributions() -> None:
    inventory = ArtifactInventory.capture(
        {
            ".codex-plugin/plugin.json": b'{"name":"fixture-plugin","version":"0.1.0"}',
            "docs/foreign/SKILL.md": _skill(),
        }
    )
    result = inspect_extension(inventory, _source())

    assert result.manifest.contributions == ()


def test_codex_plugin_unknown_fields_are_quarantined_not_silently_dropped() -> None:
    inventory = ArtifactInventory.capture(
        {
            ".codex-plugin/plugin.json": json.dumps(
                {"name": "fixture-plugin", "version": "0.1.0", "runWithoutReview": True}
            ).encode()
        }
    )
    result = inspect_extension(inventory, _source())

    assert result.status == "quarantined"
    assert [(issue.code, issue.path) for issue in result.manifest.issues] == [
        ("unsupported_manifest_field", ".codex-plugin/plugin.json#runWithoutReview")
    ]


def test_dsh_plugin_is_a_reviewed_hand_candidate_and_lifecycle_scripts_raise_risk() -> None:
    inventory = ArtifactInventory.capture(
        {
            "package.json": json.dumps(
                {
                    "name": "dsh-fixture",
                    "version": "0.1.0",
                    "scripts": {"postinstall": "node install.js"},
                    "dsh": {"bundle": {"patch": "./cordis.patch.yml"}},
                }
            ).encode(),
            "cordis.patch.yml": b"- insert:\n  - id: fixture\n",
            "install.js": b"throw new Error('must not execute')\n",
        }
    )
    result = inspect_extension(inventory, _source())

    assert result.manifest.source_format == "deepseek_harness_plugin"
    assert result.manifest.permission_plan.requires_subprocess is True
    assert result.manifest.permission_plan.requires_native_build is True
    assert "package_lifecycle_script" in result.manifest.permission_plan.review_reasons


def test_mcp_config_extracts_only_transport_and_hostname_not_command_or_headers() -> None:
    inventory = ArtifactInventory.capture(
        {
            ".mcp.json": json.dumps(
                {
                    "mcpServers": {
                        "local": {"command": "sentinel-command", "args": ["--stdio"]},
                        "remote": {
                            "url": "https://mcp.example.test/rpc",
                            "http_headers": {"Authorization": "must-not-survive"},
                        },
                    }
                }
            ).encode()
        }
    )
    result = inspect_extension(inventory, _source())

    manifest = result.manifest
    assert manifest.permission_plan.network_destinations == ("mcp.example.test",)
    assert manifest.permission_plan.requires_subprocess is True
    assert "environment_or_credential_declaration" in manifest.permission_plan.review_reasons
    assert all("sentinel" not in repr(item) and "must-not-survive" not in repr(item) for item in manifest.contributions)


def test_codex_mcp_toml_records_oauth_as_review_requirement_without_token_material() -> None:
    inventory = ArtifactInventory.capture(
        {
            ".codex/config.toml": b"""
[mcp_servers.fixture]
url = "https://mcp.example.test/rpc"
auth = "oauth"
enabled = true
"""
        }
    )
    result = inspect_extension(inventory, _source())

    assert result.manifest.permission_plan.requires_oauth is True
    assert "oauth_authorization" in result.manifest.permission_plan.review_reasons
    assert result.manifest.permission_plan.network_destinations == ("mcp.example.test",)


def test_plain_http_mcp_is_quarantined_even_when_hostname_is_valid() -> None:
    inventory = ArtifactInventory.capture(
        {".mcp.json": b'{"mcpServers":{"fixture":{"url":"http://mcp.example.test/rpc"}}}'}
    )
    result = inspect_extension(inventory, _source())

    assert result.status == "quarantined"
    assert [(issue.code, issue.path) for issue in result.manifest.issues] == [
        ("insecure_mcp_endpoint", ".mcp.json#fixture")
    ]


def test_codex_marketplace_is_a_catalog_candidate_not_an_active_plugin() -> None:
    inventory = ArtifactInventory.capture(
        {
            ".agents/plugins/marketplace.json": json.dumps(
                {
                    "name": "fixture-marketplace",
                    "plugins": [
                        {
                            "name": "fixture-plugin",
                            "source": {"source": "local", "path": "./plugins/fixture-plugin"},
                        }
                    ],
                }
            ).encode()
        }
    )
    result = inspect_extension(inventory, _source())

    assert result.manifest.source_format == "codex_marketplace"
    assert [(item.kind, item.contribution_id) for item in result.manifest.contributions] == [
        ("repository_artifact", "fixture-plugin")
    ]


def test_hook_config_maps_reviewed_events_and_quarantines_unknown_events() -> None:
    inventory = ArtifactInventory.capture(
        {
            ".codex/hooks.json": json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [{"matcher": "^Bash$", "hooks": [{"type": "command", "command": "sentinel"}]}],
                        "FutureEvent": [],
                    }
                }
            ).encode()
        }
    )
    result = inspect_extension(inventory, _source())

    assert result.status == "quarantined"
    assert result.manifest.permission_plan.hook_events == ("PreToolUse",)
    assert result.manifest.permission_plan.review_reasons == ("hook_execution",)
    assert "sentinel" not in repr(result.manifest)


def test_unknown_and_ambiguous_formats_fail_closed() -> None:
    registry = StaticExtensionDetectorRegistry()
    with pytest.raises(ExtensionDetectionError, match="unknown"):
        registry.detect_exactly_one(ArtifactInventory.capture({"README.md": b"nothing executable"}))

    ambiguous = ArtifactInventory.capture(
        {
            "SKILL.md": _skill(),
            ".mcp.json": b'{"mcpServers":{"fixture":{"command":"x"}}}',
        }
    )
    with pytest.raises(ExtensionDetectionError, match="ambiguous"):
        registry.detect_exactly_one(ambiguous)

    mixed_skills = ArtifactInventory.capture(
        {
            "SKILL.md": _skill(),
            ".dsh/skills/dsh-fixture.md": _skill().replace(b"fixture-echo", b"dsh-fixture"),
        }
    )
    with pytest.raises(ExtensionDetectionError, match="ambiguous"):
        registry.detect_exactly_one(mixed_skills)


@pytest.mark.parametrize(
    "path",
    ["../escape", "/absolute", "C:/drive", "folder\\child", "folder/trailing. ", "folder/../escape"],
)
def test_inventory_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ExtensionDetectionError, match="path"):
        ArtifactInventory.capture({path: b"x"})


def test_duplicate_json_keys_and_secret_bearing_source_urls_fail_closed() -> None:
    inventory = ArtifactInventory.capture(
        {".codex-plugin/plugin.json": b'{"name":"one","name":"two","version":"1.0.0"}'}
    )
    with pytest.raises(ExtensionContractError, match="duplicate JSON key"):
        inspect_extension(inventory, _source())

    with pytest.raises(ExtensionContractError, match="source locator"):
        ResolvedSource(
            source_kind="github_repository",
            canonical_locator="https://token:secret@github.com/example/fixture",
            immutable_revision="a" * 40,
            artifact_ref="crp://extension-artifacts/fixture/revision-1",
        )


@pytest.mark.parametrize("path", ["C:\\escape", "C:relative", "CON", "folder/NUL.txt"])
def test_contribution_candidate_rejects_windows_drive_and_device_paths(path: str) -> None:
    with pytest.raises(ExtensionContractError, match="source path"):
        ContributionCandidate("application_skill", "fixture-skill", path)


def test_public_inventory_constructor_copies_input_and_detection_marker_sequences_are_frozen() -> None:
    raw = {"SKILL.md": _skill()}
    inventory = ArtifactInventory(raw)
    raw["SKILL.md"] = b"mutated"
    raw["other.txt"] = b"new"

    assert inventory.read_bytes("SKILL.md") == _skill()
    assert inventory.paths == ("SKILL.md",)
    detection = StaticExtensionDetectorRegistry().detect_exactly_one(inventory)
    assert isinstance(detection.marker_paths, tuple)
