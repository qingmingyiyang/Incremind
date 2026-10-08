from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from core.plugin_host import (
    PluginPackageIntake,
    PluginPackageIntakeConflict,
    PluginPackageIntakeError,
)
from core.plugin_host.package_intake import candidate_stage_receipt_object_id
from core.storage_provider import SQLiteStructuredRecordStore


def _package(root: Path, *, extra_manifest: dict[str, object] | None = None, version: str = "1.2.3", directory: str = "example-plugin") -> Path:
    package = root / directory
    metadata = package / ".codex-plugin"
    skill = package / "skills" / "summarize"
    metadata.mkdir(parents=True)
    skill.mkdir(parents=True)
    manifest: dict[str, object] = {
        "name": "example-plugin",
        "version": version,
        "description": "Example package",
    }
    manifest.update(extra_manifest or {})
    (metadata / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (skill / "SKILL.md").write_bytes(b"# Exact package bytes\r\n")
    return package


def _intake(root: Path, *, now: str = "2026-08-26T00:00:00Z") -> PluginPackageIntake:
    return PluginPackageIntake(
        SQLiteStructuredRecordStore(root / "jobs.sqlite3"),
        now=now,
        source_root=root / "source",
    )


def test_discover_install_disabled_and_restart_snapshot_preserve_exact_bytes(tmp_path: Path) -> None:
    package = _package(tmp_path / "source")
    result = _intake(tmp_path).discover(str(package), command_id="discover-0001")

    assert result["state"]["status"] == "discovered"
    assert result["state"]["enabled"] is False
    assert result["normalized_manifest"]["core_api"] == "1"
    assert result["normalized_manifest"]["runtime_contract"] == {
        "execution_state_owner": "core_effect_log",
        "recovery_owner": "core_reaper",
        "secret_access": "lease_reference_only",
        "memory_write": "proposal_only",
        "document_write": "draft_only",
        "policy_predicates": "closed_core_set",
        "effect_semantics_source": "reviewed_contribution_descriptors",
    }
    raw = SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3").read(
        "plugin_raw_packages", "example-plugin~1.2.3"
    )
    assert raw is not None
    skill_file = next(item for item in raw.payload["files"] if item["relative_path"] == "skills/summarize/SKILL.md")
    assert base64.b64decode(skill_file["content_base64"]) == b"# Exact package bytes\r\n"

    installed = _intake(tmp_path, now="2026-08-26T00:01:00Z").install_disabled(
        "example-plugin",
        expected_state_revision=result["state_revision"],
        command_id="install-0001",
        confirm=True,
    )
    assert installed["state"]["status"] == "installed_disabled"
    assert installed["state"]["enabled"] is False

    restarted = _intake(tmp_path, now="2026-08-26T00:02:00Z").snapshot()
    assert restarted["packages"][0]["state"]["status"] == "installed_disabled"
    assert restarted["packages"][0]["state"]["enabled"] is False


def test_upgrade_candidate_staging_freezes_new_bytes_without_switching_current_state(tmp_path: Path) -> None:
    source = tmp_path / "source"
    current_path = _package(source)
    intake = _intake(tmp_path)
    discovered = intake.discover(str(current_path), command_id="discover-0001")
    installed = intake.install_disabled("example-plugin", expected_state_revision=discovered["state_revision"], command_id="install-0001", confirm=True)
    candidate_path = _package(source, version="2.0.0", directory="candidate-plugin")
    staged = intake.stage_upgrade_candidate(
        str(candidate_path), plugin_id="example-plugin",
        expected_state_revision=installed["state_revision"],
        expected_package_record_id="example-plugin~1.2.3",
        command_id="upgrade-stage-0001", confirm=True,
    )
    assert staged["candidate_package_record_id"] == "example-plugin~2.0.0"
    assert intake.snapshot()["packages"][0]["state"]["package_record_id"] == "example-plugin~1.2.3"
    assert SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3").read("plugin_raw_packages", "example-plugin~2.0.0") is not None
    receipt = SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3").read(
        "plugin_upgrade_stage_receipts",
        candidate_stage_receipt_object_id("example-plugin", "example-plugin~2.0.0"),
    )
    assert receipt is not None
    assert dict(receipt.payload) == {
        "schema": "1.0.0",
        "plugin": "example-plugin",
        "old": "example-plugin~1.2.3",
        "candidate": "example-plugin~2.0.0",
        "state_revision": installed["state_revision"],
        "command_id": "upgrade-stage-0001",
    }
    replay = intake.stage_upgrade_candidate(
        str(candidate_path), plugin_id="example-plugin",
        expected_state_revision=installed["state_revision"],
        expected_package_record_id="example-plugin~1.2.3",
        command_id="upgrade-stage-0001", confirm=True,
    )
    assert replay["replayed"] is True
    (candidate_path / "skills" / "summarize" / "SKILL.md").write_bytes(b"mutated candidate")
    with pytest.raises(PluginPackageIntakeConflict, match="source bytes drifted"):
        intake.stage_upgrade_candidate(
            str(candidate_path), plugin_id="example-plugin",
            expected_state_revision=installed["state_revision"],
            expected_package_record_id="example-plugin~1.2.3",
            command_id="upgrade-stage-0001", confirm=True,
        )


def test_upgrade_candidate_staging_rejects_second_command_for_same_candidate(tmp_path: Path) -> None:
    source = tmp_path / "source"
    current_path = _package(source)
    intake = _intake(tmp_path)
    discovered = intake.discover(str(current_path), command_id="discover-0001")
    installed = intake.install_disabled(
        "example-plugin", expected_state_revision=discovered["state_revision"],
        command_id="install-0001", confirm=True,
    )
    candidate_path = _package(source, version="2.0.0", directory="candidate-plugin")
    intake.stage_upgrade_candidate(
        str(candidate_path), plugin_id="example-plugin",
        expected_state_revision=installed["state_revision"],
        expected_package_record_id="example-plugin~1.2.3",
        command_id="upgrade-stage-0001", confirm=True,
    )

    with pytest.raises(PluginPackageIntakeConflict, match="already staged"):
        intake.stage_upgrade_candidate(
            str(candidate_path), plugin_id="example-plugin",
            expected_state_revision=installed["state_revision"],
            expected_package_record_id="example-plugin~1.2.3",
            command_id="upgrade-stage-0002", confirm=True,
        )


def test_upgrade_candidate_replay_rejects_missing_or_drifted_receipt(tmp_path: Path) -> None:
    source = tmp_path / "source"
    current_path = _package(source)
    intake = _intake(tmp_path)
    discovered = intake.discover(str(current_path), command_id="discover-0001")
    installed = intake.install_disabled(
        "example-plugin", expected_state_revision=discovered["state_revision"],
        command_id="install-0001", confirm=True,
    )
    candidate_path = _package(source, version="2.0.0", directory="candidate-plugin")
    intake.stage_upgrade_candidate(
        str(candidate_path), plugin_id="example-plugin",
        expected_state_revision=installed["state_revision"],
        expected_package_record_id="example-plugin~1.2.3",
        command_id="upgrade-stage-0001", confirm=True,
    )
    store = SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3")
    receipt_id = candidate_stage_receipt_object_id("example-plugin", "example-plugin~2.0.0")
    with store.begin() as uow:
        receipt = uow.read("plugin_upgrade_stage_receipts", receipt_id)
        assert receipt is not None
        uow.put(
            "plugin_upgrade_stage_receipts", receipt_id,
            {**receipt.payload, "command_id": "upgrade-stage-9999"},
            expected_revision=receipt.revision,
        )
        uow.commit()

    with pytest.raises(PluginPackageIntakeConflict, match="receipt drifted"):
        intake.stage_upgrade_candidate(
            str(candidate_path), plugin_id="example-plugin",
            expected_state_revision=installed["state_revision"],
            expected_package_record_id="example-plugin~1.2.3",
            command_id="upgrade-stage-0001", confirm=True,
        )


def test_upgrade_candidate_staging_rejects_current_pointer_drift(tmp_path: Path) -> None:
    source = tmp_path / "source"
    current_path = _package(source)
    intake = _intake(tmp_path)
    discovered = intake.discover(str(current_path), command_id="discover-0001")
    candidate_path = _package(source, version="2.0.0", directory="candidate-plugin")
    with pytest.raises(PluginPackageIntakeConflict, match="current package binding drifted"):
        intake.stage_upgrade_candidate(
            str(candidate_path), plugin_id="example-plugin",
            expected_state_revision=discovered["state_revision"] + 1,
            expected_package_record_id="example-plugin~1.2.3",
            command_id="upgrade-stage-0001", confirm=True,
        )
    assert SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3").read("plugin_raw_packages", "example-plugin~2.0.0") is None


def test_discover_replay_rejects_source_mutation_without_changing_raw(tmp_path: Path) -> None:
    package = _package(tmp_path / "source")
    intake = _intake(tmp_path)
    intake.discover(str(package), command_id="discover-0001")
    skill_path = package / "skills" / "summarize" / "SKILL.md"
    skill_path.write_bytes(b"changed")

    with pytest.raises(PluginPackageIntakeConflict, match="source bytes drifted"):
        intake.discover(str(package), command_id="discover-0001")

    raw = SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3").read(
        "plugin_raw_packages", "example-plugin~1.2.3"
    )
    assert raw is not None
    skill_file = next(item for item in raw.payload["files"] if item["relative_path"] == "skills/summarize/SKILL.md")
    assert base64.b64decode(skill_file["content_base64"]) == b"# Exact package bytes\r\n"


def test_same_version_different_bytes_conflicts_and_rolls_back_command(tmp_path: Path) -> None:
    package = _package(tmp_path / "source")
    intake = _intake(tmp_path)
    intake.discover(str(package), command_id="discover-0001")
    (package / "skills" / "summarize" / "SKILL.md").write_bytes(b"changed")

    with pytest.raises(PluginPackageIntakeConflict, match="identity drifted"):
        intake.discover(str(package), command_id="discover-0002")

    store = SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3")
    assert store.read("plugin_package_commands", "discover-0002") is None
    assert store.read("plugin_package_states", "example-plugin").revision == 1


def test_unknown_manifest_field_is_quarantined_and_cannot_install(tmp_path: Path) -> None:
    package = _package(tmp_path / "source", extra_manifest={"automatic_activation": True})
    intake = _intake(tmp_path)
    result = intake.discover(str(package), command_id="discover-0001")

    assert result["state"]["status"] == "quarantined"
    assert result["compatibility_report"]["issues"] == [
        {
            "code": "unsupported_manifest_field",
            "path": ".codex-plugin/plugin.json#automatic_activation",
        }
    ]
    with pytest.raises(PluginPackageIntakeError, match="quarantined"):
        intake.install_disabled(
            "example-plugin",
            expected_state_revision=result["state_revision"],
            command_id="install-0001",
            confirm=True,
        )


def test_install_requires_confirmation_and_state_revision(tmp_path: Path) -> None:
    package = _package(tmp_path / "source")
    intake = _intake(tmp_path)
    result = intake.discover(str(package), command_id="discover-0001")

    with pytest.raises(PluginPackageIntakeError, match="explicit confirmation"):
        intake.install_disabled(
            "example-plugin",
            expected_state_revision=result["state_revision"],
            command_id="install-0001",
            confirm=False,
        )
    with pytest.raises(PluginPackageIntakeConflict, match="expected state revision"):
        intake.install_disabled(
            "example-plugin",
            expected_state_revision=result["state_revision"] + 1,
            command_id="install-0002",
            confirm=True,
        )


def test_source_must_be_direct_child_of_governed_inbox(tmp_path: Path) -> None:
    outside = _package(tmp_path / "outside")
    with pytest.raises(PluginPackageIntakeError, match="outside the governed inbox"):
        _intake(tmp_path).discover(str(outside), command_id="discover-0001")


def test_executable_contribution_requires_future_local_review(tmp_path: Path) -> None:
    package = _package(tmp_path / "source")
    tool = package / "tools" / "runner.py"
    tool.parent.mkdir()
    tool.write_text("raise RuntimeError('must never execute')", encoding="utf-8")

    result = _intake(tmp_path).discover(str(package), command_id="discover-0001")

    assert result["state"]["status"] == "quarantined"
    assert {
        (issue["code"], issue["path"]) for issue in result["compatibility_report"]["issues"]
    } == {("executable_contribution_requires_local_review", "tools")}


def _mcp_reference(package: Path, **changes: object) -> Path:
    server = package / "mcp" / "calendar-server"
    server.mkdir(parents=True)
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "server_id": "calendar-server",
        "approval_revision": 3,
        "manifest_revision": 8,
        "endpoint_identity": "calendar-prod",
        "credential_subject_id": "calendar-user",
        "transport_generation": 2,
    }
    payload.update(changes)
    path = server / "server-ref.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _hand(package: Path, **changes: object) -> Path:
    hand = package / "hands" / "summarize-hand"
    payload = hand / "payload"
    payload.mkdir(parents=True)
    descriptor: dict[str, object] = {
        "schema_version": "1.0.0",
        "id": "summarize-hand",
        "runtime": "python-stdio-v1",
        "entrypoint": "payload/worker.bin",
        "input_schema": {
            "type": "object", "properties": {"text": {"type": "string"}},
            "required": ["text"], "additionalProperties": False,
        },
        "output_schema": {
            "type": "object", "properties": {"summary": {"type": "string"}},
            "required": ["summary"], "additionalProperties": False,
        },
        "effect": "read",
        "operation_semantics": "read_only",
        "requested_resources": ["workspace_input"],
    }
    descriptor.update(changes)
    path = hand / "hand.json"
    path.write_text(json.dumps(descriptor), encoding="utf-8")
    (payload / "worker.bin").write_bytes(b"not executed during intake")
    return path


def _hook_hand(package: Path) -> Path:
    hand = package / "hands" / "policy-hand"
    payload = hand / "payload"
    payload.mkdir(parents=True)
    descriptor = {
        "schema_version": "1.0.0",
        "id": "policy-hand",
        "runtime": "powershell-stdio-v1",
        "entrypoint": "payload/worker.bin",
        "input_schema": {
            "type": "object",
            "properties": {"hook_event": {"type": "string"}, "payload": {"type": "object"}},
            "required": ["hook_event", "payload"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "exit_code": {"type": "integer"},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
            },
            "required": ["exit_code", "stdout", "stderr"],
            "additionalProperties": False,
        },
        "effect": "read",
        "operation_semantics": "read_only",
        "requested_resources": [],
    }
    path = hand / "hand.json"
    path.write_text(json.dumps(descriptor), encoding="utf-8")
    (payload / "worker.bin").write_bytes(b"not executed during intake")
    return path


def _hook(package: Path, **changes: object) -> Path:
    hook = package / "hooks" / "pre-tool-policy"
    hook.mkdir(parents=True)
    descriptor: dict[str, object] = {
        "schema_version": "1.0.0",
        "id": "pre-tool-policy",
        "hand_id": "policy-hand",
        "event": "PreToolUse",
        "order": 10,
        "sync": True,
        "timeout_ms": 750,
        "metadata_projection": "codex-hook-v1",
        "recursion": "deny",
    }
    descriptor.update(changes)
    path = hook / "hook.json"
    path.write_text(json.dumps(descriptor), encoding="utf-8")
    return path


def test_strict_plugin_hook_is_captured_as_disabled_hand_binding(tmp_path: Path) -> None:
    package = _package(tmp_path / "source")
    _hook_hand(package)
    _hook(package)

    result = _intake(tmp_path).discover(str(package), command_id="discover-hook-0001")

    assert result["state"]["status"] == "discovered"
    assert result["compatibility_report"]["activation_effect"] == "none"
    assert result["normalized_manifest"]["hook_candidates"] == [{
        "schema_version": "1.0.0",
        "id": "pre-tool-policy",
        "hand_id": "policy-hand",
        "event": "PreToolUse",
        "order": 10,
        "sync": True,
        "timeout_ms": 750,
        "metadata_projection": "codex-hook-v1",
        "recursion": "deny",
    }]


@pytest.mark.parametrize("mutation", [
    {"command": "python policy.py"},
    {"hand_id": "missing-hand"},
    {"event": "BeforeAnything"},
    {"event": "SessionStart"},
    {"metadata_projection": "full-session"},
    {"recursion": "allow"},
])
def test_plugin_hook_rejects_locators_drift_and_expanded_policy(
    tmp_path: Path, mutation: dict[str, object],
) -> None:
    package = _package(tmp_path / "source")
    _hook_hand(package)
    _hook(package, **mutation)

    result = _intake(tmp_path).discover(str(package), command_id="discover-hook-0001")

    assert result["state"]["status"] == "quarantined"
    assert ("invalid_plugin_hook_candidate", "hooks/pre-tool-policy/hook.json") in {
        (issue["code"], issue["path"]) for issue in result["compatibility_report"]["issues"]
    }


@pytest.mark.parametrize("hand_change", [
    {"effect": "write", "operation_semantics": "receipt_required"},
    {"requested_resources": ["workspace_input"]},
    {"requested_resources": ["workspace_output"]},
])
def test_plugin_hook_requires_a_read_only_resource_free_hand(
    tmp_path: Path, hand_change: dict[str, object],
) -> None:
    package = _package(tmp_path / "source")
    _hook_hand(package)
    hand_path = package / "hands" / "policy-hand" / "hand.json"
    descriptor = json.loads(hand_path.read_text(encoding="utf-8"))
    descriptor.update(hand_change)
    hand_path.write_text(json.dumps(descriptor), encoding="utf-8")
    _hook(package)

    result = _intake(tmp_path).discover(str(package), command_id="discover-hook-0001")

    assert result["state"]["status"] == "quarantined"


def test_strict_mcp_reference_is_a_disabled_candidate_not_connection_authority(tmp_path: Path) -> None:
    package = _package(tmp_path / "source")
    _mcp_reference(package)

    result = _intake(tmp_path).discover(str(package), command_id="discover-mcp-0001")

    assert result["state"]["status"] == "discovered"
    assert result["state"]["enabled"] is False
    assert result["normalized_manifest"]["mcp_server_candidates"] == [{
        "schema_version": "1.0.0", "server_id": "calendar-server",
        "approval_revision": 3, "manifest_revision": 8,
        "endpoint_identity": "calendar-prod", "credential_subject_id": "calendar-user",
        "transport_generation": 2,
    }]
    assert result["compatibility_report"]["activation_effect"] == "none"


@pytest.mark.parametrize("mutation", [
    {"endpoint_url": "https://example.test/mcp"},
    {"secret_ref": "mcp:calendar-server:token"},
    {"server_id": "different-server"},
])
def test_mcp_reference_rejects_connection_fields_and_identity_drift(
    tmp_path: Path, mutation: dict[str, object],
) -> None:
    package = _package(tmp_path / "source")
    _mcp_reference(package, **mutation)

    result = _intake(tmp_path).discover(str(package), command_id="discover-mcp-0001")

    assert result["state"]["status"] == "quarantined"
    assert any(issue["code"] == "invalid_declarative_mcp_server_reference" for issue in result["compatibility_report"]["issues"])


def test_mcp_executable_shape_remains_quarantined(tmp_path: Path) -> None:
    package = _package(tmp_path / "source")
    executable = package / "mcp" / "server.py"
    executable.parent.mkdir()
    executable.write_text("raise RuntimeError", encoding="utf-8")

    result = _intake(tmp_path).discover(str(package), command_id="discover-mcp-0001")

    assert result["state"]["status"] == "quarantined"
    assert ("executable_contribution_requires_local_review", "mcp") in {
        (issue["code"], issue["path"]) for issue in result["compatibility_report"]["issues"]
    }


def test_strict_hands_candidate_is_captured_but_remains_disabled_and_unexecuted(tmp_path: Path) -> None:
    package = _package(tmp_path / "source")
    _hand(package)

    result = _intake(tmp_path).discover(str(package), command_id="discover-hands-0001")

    assert result["state"]["status"] == "discovered"
    assert result["state"]["enabled"] is False
    assert result["compatibility_report"]["activation_effect"] == "none"
    assert result["normalized_manifest"]["hands_candidates"] == [{
        "schema_version": "1.0.0",
        "id": "summarize-hand",
        "runtime": "python-stdio-v1",
        "entrypoint": "payload/worker.bin",
        "input_schema": {
            "type": "object", "properties": {"text": {"type": "string"}},
            "required": ["text"], "additionalProperties": False,
        },
        "output_schema": {
            "type": "object", "properties": {"summary": {"type": "string"}},
            "required": ["summary"], "additionalProperties": False,
        },
        "effect": "read",
        "operation_semantics": "read_only",
        "requested_resources": ["workspace_input"],
        "payload_file_count": 1,
        "payload_bytes": len(b"not executed during intake"),
    }]
    installed = _intake(tmp_path, now="2026-08-26T00:01:00Z").install_disabled(
        "example-plugin",
        expected_state_revision=result["state_revision"],
        command_id="install-hands-0001",
        confirm=True,
    )
    assert installed["state"]["status"] == "installed_disabled"
    assert installed["state"]["enabled"] is False


@pytest.mark.parametrize("mutation", [
    {"launcher": "payload/worker.bin"},
    {"argv": ["--unsafe"]},
    {"environment": {"TOKEN": "secret"}},
    {"secret_ref": "plugin:secret"},
    {"network_policy": "allow"},
    {"display_name": "not part of the frozen contract"},
    {"entrypoint": "../worker.bin"},
    {"requested_resources": ["network"]},
    {"id": "different-hand"},
])
def test_hands_descriptor_rejects_launcher_policy_secret_and_identity_fields(
    tmp_path: Path, mutation: dict[str, object],
) -> None:
    package = _package(tmp_path / "source")
    _hand(package, **mutation)

    result = _intake(tmp_path).discover(str(package), command_id="discover-hands-0001")

    assert result["state"]["status"] == "quarantined"
    assert result["normalized_manifest"]["hands_candidates"] == []
    assert any(issue["code"] == "invalid_plugin_hands_candidate" for issue in result["compatibility_report"]["issues"])


def test_hands_candidate_requires_exact_descriptor_and_nonempty_payload_tree(tmp_path: Path) -> None:
    package = _package(tmp_path / "source")
    hand = package / "hands" / "summarize-hand"
    hand.mkdir(parents=True)
    (hand / "hand.json").write_text(json.dumps({
        "schema_version": "1.0.0", "id": "summarize-hand", "runtime": "python-stdio-v1",
        "entrypoint": "payload/worker.py",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "effect": "read", "operation_semantics": "read_only", "requested_resources": [],
    }), encoding="utf-8")
    (hand / "worker.py").write_text("raise RuntimeError('must never execute')", encoding="utf-8")

    result = _intake(tmp_path).discover(str(package), command_id="discover-hands-0001")

    assert result["state"]["status"] == "quarantined"
    assert {
        (issue["code"], issue["path"]) for issue in result["compatibility_report"]["issues"]
    } == {
        ("invalid_plugin_hands_candidate", "hands/summarize-hand/hand.json"),
        ("invalid_plugin_hands_candidate", "hands/summarize-hand/payload"),
        ("invalid_plugin_hands_candidate", "hands/summarize-hand/worker.py"),
    }


def test_hands_descriptor_rejects_duplicate_json_fields(tmp_path: Path) -> None:
    package = _package(tmp_path / "source")
    descriptor = _hand(package)
    descriptor.write_text(
        '{"schema_version":"1.0.0","id":"summarize-hand","id":"summarize-hand",'
        '"runtime":"python-stdio-v1","entrypoint":"payload/worker.bin",'
        '"input_schema":{"type":"object","properties":{},"required":[],"additionalProperties":false},'
        '"output_schema":{"type":"object","properties":{},"required":[],"additionalProperties":false},'
        '"effect":"read","operation_semantics":"read_only","requested_resources":[]}',
        encoding="utf-8",
    )

    result = _intake(tmp_path).discover(str(package), command_id="discover-hands-0001")

    assert result["state"]["status"] == "quarantined"
    assert result["compatibility_report"]["issues"] == [{
        "code": "invalid_plugin_hands_candidate", "path": "hands/summarize-hand/hand.json",
    }]


@pytest.mark.parametrize("changes", [
    {"effect": "read", "operation_semantics": "receipt_required"},
    {"effect": "write", "operation_semantics": "read_only"},
    {"effect": "read", "requested_resources": ["workspace_output"]},
    {"effect": "write", "operation_semantics": "receipt_required", "requested_resources": ["workspace_input", "workspace_input"]},
    {"input_schema": {"type": "object", "properties": {"value": {"$ref": "https://example.test/schema"}}, "required": [], "additionalProperties": False}},
    {"output_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": True}},
])
def test_hands_descriptor_enforces_effect_resources_and_closed_object_schemas(
    tmp_path: Path, changes: dict[str, object],
) -> None:
    package = _package(tmp_path / "source")
    _hand(package, **changes)

    result = _intake(tmp_path).discover(str(package), command_id="discover-hands-0001")

    assert result["state"]["status"] == "quarantined"
    assert result["compatibility_report"]["issues"] == [{
        "code": "invalid_plugin_hands_candidate", "path": "hands/summarize-hand/hand.json",
    }]
