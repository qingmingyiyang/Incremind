from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from core.storage_provider import (
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SQLiteStructuredRecordUnitOfWork,
    SQLiteUnitOfWorkConflict,
)


class PluginPackageIntakeError(ValueError):
    """Raised when a local Plugin package cannot enter governed staging."""


class PluginPackageIntakeConflict(PluginPackageIntakeError):
    """Raised when an immutable package, command, or state identity drifts."""


_PLUGIN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]{1,48})?$")
_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")
_ALLOWED_MANIFEST_FIELDS = frozenset(
    {"name", "version", "description", "author", "homepage", "repository", "license", "keywords"}
)
_ALLOWED_TOP_LEVEL = frozenset(
    {".codex-plugin", "skills", "tools", "mcp", "hands", "hooks", "model-providers", "ui", "migrations"}
)
_CONTRIBUTION_GROUPS = tuple(sorted(_ALLOWED_TOP_LEVEL - {".codex-plugin"}))
# ``tools`` is deliberately absent here.  It is only admitted when every file
# is a declarative local lookup definition; all other tool shapes remain
# quarantined like the executable contribution classes below.
_EXECUTABLE_GROUPS = frozenset({"model-providers", "migrations"})
_TOOL_ID = re.compile(r"^[a-z][a-z0-9._-]{1,127}$")
_MCP_SERVER_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_HAND_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_RAW = "plugin_raw_packages"
_REPORTS = "plugin_compatibility_reports"
_MANIFESTS = "plugin_normalized_manifests"
_STATES = "plugin_package_states"
_COMMANDS = "plugin_package_commands"
_UPGRADE_STAGE_RECEIPTS = "plugin_upgrade_stage_receipts"
_MAX_FILES = 256
_MAX_FILE_BYTES = 1024 * 1024
_MAX_PACKAGE_BYTES = 4 * 1024 * 1024
_MAX_RELATIVE_PATH = 240


@dataclass(frozen=True, slots=True)
class _InspectedPackage:
    plugin_id: str
    version: str
    record_id: str
    raw: Mapping[str, object]
    report: Mapping[str, object]
    manifest: Mapping[str, object]


class PluginPackageIntake:
    """Stage raw local Plugin packages without enabling or executing them."""

    def __init__(
        self,
        records: SQLiteStructuredRecordStore,
        *,
        now: str,
        source_root: Path,
    ) -> None:
        self._records = records
        self._now = _required_text(now, "now", maximum=96)
        self._source_root = source_root.expanduser().resolve(strict=False)

    def discover(self, source_path: str, *, command_id: str) -> dict[str, object]:
        command = _command_id(command_id)
        inspected = _inspect_package(_selected_directory(source_path, self._source_root))
        try:
            with self._records.begin() as uow:
                replay = uow.read(_COMMANDS, command)
                if replay is not None:
                    _require_replayed_raw(uow, replay, inspected)
                    return _replay(replay, "discover", inspected.plugin_id, inspected.record_id)

                _put_immutable(uow, _RAW, inspected.record_id, inspected.raw)
                _put_immutable(uow, _REPORTS, inspected.record_id, inspected.report)
                _put_immutable(uow, _MANIFESTS, inspected.record_id, inspected.manifest)
                current = uow.read(_STATES, inspected.plugin_id)
                if current is None:
                    status = "discovered" if inspected.report["compatible"] is True else "quarantined"
                    state = {
                        "schema_version": "1.0.0",
                        "plugin_id": inspected.plugin_id,
                        "package_record_id": inspected.record_id,
                        "status": status,
                        "enabled": False,
                        "compatible": inspected.report["compatible"],
                        "discovered_at": self._now,
                        "installed_at": None,
                    }
                    state_record = uow.put(_STATES, inspected.plugin_id, state, expected_revision=0)
                else:
                    _require_same_package(current, inspected.record_id)
                    state_record = current
                result = _result(state_record, inspected.report, inspected.manifest, replayed=False)
                uow.put(
                    _COMMANDS,
                    command,
                    {
                        "schema_version": "1.0.0",
                        "operation": "discover",
                        "plugin_id": inspected.plugin_id,
                        "package_record_id": inspected.record_id,
                        "result": result,
                    },
                    expected_revision=0,
                )
                uow.commit()
                return result
        except SQLiteUnitOfWorkConflict as error:
            raise PluginPackageIntakeConflict(str(error)) from error

    def stage_upgrade_candidate(
        self, source_path: str, *, plugin_id: str, expected_state_revision: int,
        expected_package_record_id: str, command_id: str, confirm: bool,
    ) -> dict[str, object]:
        """Freeze a different immutable package without changing the active state pointer."""

        identity, command = _plugin_id(plugin_id), _command_id(command_id)
        expected_package = _package_record_id(expected_package_record_id, plugin_id=identity)
        if confirm is not True:
            raise PluginPackageIntakeError("Plugin upgrade staging requires explicit confirmation")
        if not isinstance(expected_state_revision, int) or isinstance(expected_state_revision, bool) or expected_state_revision < 1:
            raise PluginPackageIntakeError("expected_state_revision must be a positive integer")
        inspected = _inspect_package(_selected_directory(source_path, self._source_root))
        if inspected.plugin_id != identity or inspected.record_id == expected_package:
            raise PluginPackageIntakeConflict("Plugin upgrade candidate identity is invalid")
        requested = {
            "plugin_id": identity, "old_package_record_id": expected_package,
            "candidate_package_record_id": inspected.record_id,
            "expected_state_revision": expected_state_revision,
        }
        receipt_id = candidate_stage_receipt_object_id(identity, inspected.record_id)
        receipt_payload = {
            "schema": "1.0.0",
            "plugin": identity,
            "old": expected_package,
            "candidate": inspected.record_id,
            "state_revision": expected_state_revision,
            "command_id": command,
        }
        try:
            with self._records.begin() as uow:
                replay = uow.read(_COMMANDS, command)
                if replay is not None:
                    if replay.payload.get("operation") != "stage_upgrade_candidate" or any(replay.payload.get(key) != value for key, value in requested.items()):
                        raise PluginPackageIntakeConflict("Plugin upgrade staging command drifted")
                    frozen_raw = _required_record(uow.read(_RAW, inspected.record_id), "raw package")
                    if dict(frozen_raw.payload) != dict(inspected.raw):
                        raise PluginPackageIntakeConflict("Plugin upgrade candidate source bytes drifted")
                    _require_upgrade_stage_receipt(
                        _required_record(
                            uow.read(_UPGRADE_STAGE_RECEIPTS, receipt_id),
                            "upgrade staging receipt",
                        ),
                        receipt_id,
                        receipt_payload,
                    )
                    result = replay.payload.get("result")
                    if not isinstance(result, Mapping):
                        raise PluginPackageIntakeConflict("Plugin upgrade staging command is invalid")
                    uow.rollback()
                    return dict(result) | {"replayed": True}
                current = _required_record(uow.read(_STATES, identity), "state")
                if current.revision != expected_state_revision or current.payload.get("package_record_id") != expected_package:
                    raise PluginPackageIntakeConflict("Plugin upgrade current package binding drifted")
                if uow.read(_UPGRADE_STAGE_RECEIPTS, receipt_id) is not None:
                    raise PluginPackageIntakeConflict("Plugin upgrade candidate package is already staged")
                _put_immutable(uow, _RAW, inspected.record_id, inspected.raw)
                _put_immutable(uow, _REPORTS, inspected.record_id, inspected.report)
                _put_immutable(uow, _MANIFESTS, inspected.record_id, inspected.manifest)
                if inspected.report.get("compatible") is not True:
                    raise PluginPackageIntakeError("quarantined Plugin package cannot be staged for upgrade")
                result = {
                    "schema_version": "1.0.0", "plugin_id": identity,
                    "old_package_record_id": expected_package,
                    "candidate_package_record_id": inspected.record_id,
                    "state_revision": current.revision,
                    "compatibility_report": dict(inspected.report),
                    "normalized_manifest": dict(inspected.manifest),
                    "replayed": False,
                }
                uow.put(
                    _UPGRADE_STAGE_RECEIPTS,
                    receipt_id,
                    receipt_payload,
                    expected_revision=0,
                )
                uow.put(_COMMANDS, command, {"schema_version": "1.0.0", "operation": "stage_upgrade_candidate", **requested, "result": result}, expected_revision=0)
                uow.commit()
                return result
        except SQLiteUnitOfWorkConflict as error:
            raise PluginPackageIntakeConflict(str(error)) from error

    def install_disabled(
        self,
        plugin_id: str,
        *,
        expected_state_revision: int,
        command_id: str,
        confirm: bool,
    ) -> dict[str, object]:
        identity = _plugin_id(plugin_id)
        command = _command_id(command_id)
        if confirm is not True:
            raise PluginPackageIntakeError("Plugin installation requires explicit confirmation")
        if not isinstance(expected_state_revision, int) or isinstance(expected_state_revision, bool) or expected_state_revision < 1:
            raise PluginPackageIntakeError("expected_state_revision must be a positive integer")
        try:
            with self._records.begin() as uow:
                replay = uow.read(_COMMANDS, command)
                if replay is not None:
                    package_record_id = _required_payload_text(replay.payload, "package_record_id")
                    replayed = _replay(replay, "install_disabled", identity, package_record_id)
                    current = _required_record(uow.read(_STATES, identity), "state")
                    if current.revision != replayed.get("state_revision"):
                        raise PluginPackageIntakeConflict("Plugin install command result is stale")
                    return replayed
                state_record = uow.read(_STATES, identity)
                if state_record is None:
                    raise PluginPackageIntakeError("Plugin package has not been discovered")
                if state_record.revision != expected_state_revision:
                    raise PluginPackageIntakeConflict(
                        f"expected state revision {expected_state_revision}, found {state_record.revision}"
                    )
                package_record_id = _required_payload_text(state_record.payload, "package_record_id")
                raw = _required_record(uow.read(_RAW, package_record_id), "raw package")
                report_record = _required_record(uow.read(_REPORTS, package_record_id), "compatibility report")
                manifest_record = _required_record(uow.read(_MANIFESTS, package_record_id), "normalized manifest")
                _require_bundle_identity(identity, package_record_id, raw, report_record, manifest_record)
                if report_record.payload.get("compatible") is not True:
                    raise PluginPackageIntakeError("quarantined Plugin package cannot be installed")
                if state_record.payload.get("status") != "discovered":
                    raise PluginPackageIntakeConflict("Plugin package is not in discovered state")
                updated = dict(state_record.payload)
                updated.update({"status": "installed_disabled", "enabled": False, "installed_at": self._now})
                installed = uow.put(
                    _STATES,
                    identity,
                    updated,
                    expected_revision=state_record.revision,
                )
                result = _result(installed, report_record.payload, manifest_record.payload, replayed=False)
                uow.put(
                    _COMMANDS,
                    command,
                    {
                        "schema_version": "1.0.0",
                        "operation": "install_disabled",
                        "plugin_id": identity,
                        "package_record_id": package_record_id,
                        "result": result,
                    },
                    expected_revision=0,
                )
                uow.commit()
                return result
        except SQLiteUnitOfWorkConflict as error:
            raise PluginPackageIntakeConflict(str(error)) from error

    def snapshot(self) -> dict[str, object]:
        packages: list[dict[str, object]] = []
        for state_record in self._records.list(_STATES):
            identity = state_record.object_id
            package_record_id = _required_payload_text(state_record.payload, "package_record_id")
            raw = _required_record(self._records.read(_RAW, package_record_id), "raw package")
            report = _required_record(self._records.read(_REPORTS, package_record_id), "compatibility report")
            manifest = _required_record(self._records.read(_MANIFESTS, package_record_id), "normalized manifest")
            _require_bundle_identity(identity, package_record_id, raw, report, manifest)
            packages.append(_result(state_record, report.payload, manifest.payload, replayed=False))
        return {"schema_version": "1.0.0", "packages": packages}


def _inspect_package(root: Path) -> _InspectedPackage:
    manifest_path = root / ".codex-plugin" / "plugin.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise PluginPackageIntakeError(".codex-plugin/plugin.json is required and cannot be a symlink")
    files: list[dict[str, object]] = []
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise PluginPackageIntakeError("Plugin package cannot contain symlinks")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PluginPackageIntakeError("Plugin package entries must be regular files")
        relative = path.relative_to(root).as_posix()
        _validate_relative_path(relative)
        raw = path.read_bytes()
        if len(raw) > _MAX_FILE_BYTES:
            raise PluginPackageIntakeError("Plugin package file exceeds the size limit")
        total_bytes += len(raw)
        if total_bytes > _MAX_PACKAGE_BYTES:
            raise PluginPackageIntakeError("Plugin package exceeds the total size limit")
        files.append(
            {
                "relative_path": relative,
                "size_bytes": len(raw),
                "content_base64": base64.b64encode(raw).decode("ascii"),
            }
        )
    if not files or len(files) > _MAX_FILES:
        raise PluginPackageIntakeError("Plugin package file count is invalid")
    captured_manifest = next(
        item for item in files if item["relative_path"] == ".codex-plugin/plugin.json"
    )
    manifest = _parse_manifest_bytes(base64.b64decode(str(captured_manifest["content_base64"])))
    identity = _plugin_id(manifest.get("name"))
    version = _version(manifest.get("version"))
    record_id = f"{identity}~{version}"
    issues = _compatibility_issues(root, manifest, files)
    compatible = not issues
    contributions = {
        group: [item["relative_path"] for item in files if str(item["relative_path"]).startswith(f"{group}/")]
        for group in _CONTRIBUTION_GROUPS
    }
    contributions = {key: value for key, value in contributions.items() if value}
    raw_payload = {
        "schema_version": "1.0.0",
        "source_format": "openai-codex-plugin",
        "plugin_id": identity,
        "version": version,
        "file_count": len(files),
        "package_bytes": total_bytes,
        "files": files,
    }
    report = {
        "schema_version": "1.0.0",
        "plugin_id": identity,
        "version": version,
        "source_format": "openai-codex-plugin",
        "compatible": compatible,
        "issues": issues,
        "candidate_contributions": contributions,
        "activation_effect": "none",
    }
    declarative_tools = _captured_declarative_tools(files) if not any(
        issue["path"] == "tools" or issue["path"].startswith("tools/") for issue in issues
    ) else ()
    mcp_server_candidates = _captured_mcp_server_candidates(files) if not any(
        issue["path"] == "mcp" or issue["path"].startswith("mcp/") for issue in issues
    ) else ()
    hands_candidates = _captured_hands_candidates(files) if not any(
        issue["path"] == "hands" or issue["path"].startswith("hands/") for issue in issues
    ) else ()
    has_hook_files = any(
        PurePosixPath(str(item["relative_path"])).parts[0] == "hooks" for item in files
    )
    hook_candidates = _captured_hook_candidates(files) if has_hook_files and not any(
        issue["path"] == "hooks" or issue["path"].startswith("hooks/")
        or issue["path"] == "hands" or issue["path"].startswith("hands/")
        for issue in issues
    ) else ()
    normalized = {
        "schema_version": "1.0.0",
        "core_api": "1",
        "plugin_id": identity,
        "version": version,
        "display_name": identity,
        "description": _required_text(manifest.get("description"), "description", maximum=2048),
        "source_format": "openai-codex-plugin",
        "compatible": compatible,
        "candidate_contributions": contributions if compatible else {},
        "declarative_tools": [dict(tool) for tool in declarative_tools] if compatible else [],
        "mcp_server_candidates": [dict(candidate) for candidate in mcp_server_candidates] if compatible else [],
        "hands_candidates": [dict(candidate) for candidate in hands_candidates] if compatible else [],
        "hook_candidates": [dict(candidate) for candidate in hook_candidates] if compatible else [],
        "enabled": False,
        "permission_effect": "none",
        "runtime_contract": {
            "execution_state_owner": "core_effect_log",
            "recovery_owner": "core_reaper",
            "secret_access": "lease_reference_only",
            "memory_write": "proposal_only",
            "document_write": "draft_only",
            "policy_predicates": "closed_core_set",
            "effect_semantics_source": "reviewed_contribution_descriptors",
        },
    }
    return _InspectedPackage(identity, version, record_id, raw_payload, report, normalized)


def _compatibility_issues(
    root: Path,
    manifest: Mapping[str, object],
    files: Sequence[Mapping[str, object]],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    unknown_fields = sorted(set(manifest) - _ALLOWED_MANIFEST_FIELDS)
    for field in unknown_fields:
        issues.append({"code": "unsupported_manifest_field", "path": f".codex-plugin/plugin.json#{field}"})
    unknown_top = sorted(
        {
            PurePosixPath(str(item["relative_path"])).parts[0]
            for item in files
            if PurePosixPath(str(item["relative_path"])).parts[0] not in _ALLOWED_TOP_LEVEL
        }
    )
    for name in unknown_top:
        issues.append({"code": "unsupported_top_level_entry", "path": name})
    executable_top = sorted(set(_EXECUTABLE_GROUPS) & {
        PurePosixPath(str(item["relative_path"])).parts[0] for item in files
    })
    for name in executable_top:
        issues.append({"code": "executable_contribution_requires_local_review", "path": name})
    if any(PurePosixPath(str(item["relative_path"])).parts[0] == "tools" for item in files):
        issues.extend(_declarative_tool_issues(files))
    if any(PurePosixPath(str(item["relative_path"])).parts[0] == "mcp" for item in files):
        issues.extend(_declarative_mcp_reference_issues(files))
    if any(PurePosixPath(str(item["relative_path"])).parts[0] == "hands" for item in files):
        issues.extend(_hands_candidate_issues(root, files))
    if any(PurePosixPath(str(item["relative_path"])).parts[0] == "hooks" for item in files):
        issues.extend(_hook_candidate_issues(files))
    plugin_dir_entries = sorted(
        path.relative_to(root).as_posix()
        for path in (root / ".codex-plugin").iterdir()
        if path.name != "plugin.json"
    )
    for relative in plugin_dir_entries:
        issues.append({"code": "unsupported_plugin_metadata", "path": relative})
    return issues


def _declarative_tool_issues(files: Sequence[Mapping[str, object]]) -> list[dict[str, str]]:
    """Validate the sole first-class Plugin Tool shape without executing it."""
    issues: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in files:
        relative = str(item.get("relative_path", ""))
        parts = PurePosixPath(relative).parts
        if not parts or parts[0] != "tools":
            continue
        if len(parts) != 3 or parts[2] != "tool.json" or not _TOOL_ID.fullmatch(parts[1]):
            issues.append({"code": "executable_contribution_requires_local_review", "path": "tools"})
            continue
        try:
            raw = base64.b64decode(str(item.get("content_base64", "")), validate=True)
            tool = _parse_declarative_tool(raw, expected_id=parts[1])
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, PluginPackageIntakeError):
            issues.append({"code": "invalid_declarative_local_lookup_tool", "path": relative})
            continue
        tool_id = str(tool["id"])
        if tool_id in seen:
            issues.append({"code": "invalid_declarative_local_lookup_tool", "path": relative})
        seen.add(tool_id)
    return issues


def _captured_declarative_tools(files: Sequence[Mapping[str, object]]) -> tuple[Mapping[str, object], ...]:
    tools: list[Mapping[str, object]] = []
    for item in files:
        relative = str(item.get("relative_path", ""))
        parts = PurePosixPath(relative).parts
        if len(parts) == 3 and parts[0] == "tools" and parts[2] == "tool.json":
            raw = base64.b64decode(str(item.get("content_base64", "")), validate=True)
            tools.append(_parse_declarative_tool(raw, expected_id=parts[1]))
    return tuple(sorted(tools, key=lambda tool: str(tool["id"])))


def _declarative_mcp_reference_issues(files: Sequence[Mapping[str, object]]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in files:
        relative = str(item.get("relative_path", ""))
        parts = PurePosixPath(relative).parts
        if not parts or parts[0] != "mcp":
            continue
        if len(parts) != 3 or parts[2] != "server-ref.json" or not _MCP_SERVER_ID.fullmatch(parts[1]):
            issues.append({"code": "executable_contribution_requires_local_review", "path": "mcp"})
            continue
        try:
            raw = base64.b64decode(str(item.get("content_base64", "")), validate=True)
            candidate = _parse_mcp_server_reference(raw, expected_id=parts[1])
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, PluginPackageIntakeError):
            issues.append({"code": "invalid_declarative_mcp_server_reference", "path": relative})
            continue
        server_id = str(candidate["server_id"])
        if server_id in seen:
            issues.append({"code": "invalid_declarative_mcp_server_reference", "path": relative})
        seen.add(server_id)
    return issues


def _captured_mcp_server_candidates(files: Sequence[Mapping[str, object]]) -> tuple[Mapping[str, object], ...]:
    candidates: list[Mapping[str, object]] = []
    for item in files:
        relative = str(item.get("relative_path", ""))
        parts = PurePosixPath(relative).parts
        if len(parts) == 3 and parts[0] == "mcp" and parts[2] == "server-ref.json":
            raw = base64.b64decode(str(item.get("content_base64", "")), validate=True)
            candidates.append(_parse_mcp_server_reference(raw, expected_id=parts[1]))
    return tuple(sorted(candidates, key=lambda item: str(item["server_id"])))


def _hands_candidate_issues(root: Path, files: Sequence[Mapping[str, object]]) -> list[dict[str, str]]:
    """Accept only a descriptor plus opaque payload tree; never a launch recipe."""
    issues: list[dict[str, str]] = []
    descriptors: dict[str, str] = {}
    payload_ids: set[str] = set()
    seen: set[str] = set()
    for item in files:
        relative = str(item.get("relative_path", ""))
        parts = PurePosixPath(relative).parts
        if not parts or parts[0] != "hands":
            continue
        if len(parts) == 3 and parts[2] == "hand.json" and _HAND_ID.fullmatch(parts[1]):
            hand_id = parts[1]
            if hand_id in seen:
                issues.append({"code": "invalid_plugin_hands_candidate", "path": relative})
                continue
            seen.add(hand_id)
            descriptors[hand_id] = relative
            try:
                raw = base64.b64decode(str(item.get("content_base64", "")), validate=True)
                _parse_hand_descriptor(raw, expected_id=hand_id, files=files)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError, PluginPackageIntakeError):
                issues.append({"code": "invalid_plugin_hands_candidate", "path": relative})
            continue
        if len(parts) >= 4 and parts[2] == "payload" and _HAND_ID.fullmatch(parts[1]):
            payload_ids.add(parts[1])
            continue
        issues.append({"code": "invalid_plugin_hands_candidate", "path": relative})
    for hand_id in sorted(set(descriptors) | payload_ids):
        if hand_id not in descriptors:
            issues.append({"code": "invalid_plugin_hands_candidate", "path": f"hands/{hand_id}"})
        if hand_id not in payload_ids:
            issues.append({"code": "invalid_plugin_hands_candidate", "path": f"hands/{hand_id}/payload"})
    hands_root = root / "hands"
    for path in hands_root.rglob("*"):
        if not path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        parts = PurePosixPath(relative).parts
        is_payload_tree = len(parts) >= 3 and parts[2] == "payload" and _HAND_ID.fullmatch(parts[1])
        is_required_parent = len(parts) == 2 and _HAND_ID.fullmatch(parts[1])
        if not is_payload_tree and not is_required_parent:
            issues.append({"code": "invalid_plugin_hands_candidate", "path": relative})
    return issues


def _captured_hands_candidates(files: Sequence[Mapping[str, object]]) -> tuple[Mapping[str, object], ...]:
    candidates: list[Mapping[str, object]] = []
    for item in files:
        relative = str(item.get("relative_path", ""))
        parts = PurePosixPath(relative).parts
        if len(parts) != 3 or parts[0] != "hands" or parts[2] != "hand.json":
            continue
        hand_id = parts[1]
        raw = base64.b64decode(str(item.get("content_base64", "")), validate=True)
        descriptor = dict(_parse_hand_descriptor(raw, expected_id=hand_id, files=files))
        payload = [
            candidate for candidate in files
            if PurePosixPath(str(candidate.get("relative_path", ""))).parts[:3] == ("hands", hand_id, "payload")
        ]
        # Paths remain solely in the immutable raw package.  The normalized
        # candidate carries only a bounded payload fact, never an entrypoint.
        descriptor["payload_file_count"] = len(payload)
        descriptor["payload_bytes"] = sum(int(candidate["size_bytes"]) for candidate in payload)
        candidates.append(descriptor)
    return tuple(sorted(candidates, key=lambda item: str(item["id"])))


_CODEX_HOOK_EVENTS = frozenset({"PreToolUse"})
_HOOK_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"hook_event": {"type": "string"}, "payload": {"type": "object"}},
    "required": ["hook_event", "payload"],
    "additionalProperties": False,
}
_HOOK_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "exit_code": {"type": "integer"},
        "stdout": {"type": "string"},
        "stderr": {"type": "string"},
    },
    "required": ["exit_code", "stdout", "stderr"],
    "additionalProperties": False,
}


def _hook_candidate_issues(files: Sequence[Mapping[str, object]]) -> list[dict[str, str]]:
    """Accept only declarative Hook-to-Hand bindings, never Hook code or locators."""

    issues: list[dict[str, str]] = []
    try:
        hands = {str(item["id"]): item for item in _captured_hands_candidates(files)}
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, PluginPackageIntakeError):
        return [{"code": "invalid_plugin_hook_candidate", "path": "hooks"}]
    seen: set[str] = set()
    for item in files:
        relative = str(item.get("relative_path", ""))
        parts = PurePosixPath(relative).parts
        if not parts or parts[0] != "hooks":
            continue
        if len(parts) != 3 or parts[2] != "hook.json" or not _HAND_ID.fullmatch(parts[1]):
            issues.append({"code": "invalid_plugin_hook_candidate", "path": relative or "hooks"})
            continue
        try:
            raw = base64.b64decode(str(item.get("content_base64", "")), validate=True)
            candidate = _parse_hook_descriptor(raw, expected_id=parts[1], hands=hands)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, PluginPackageIntakeError):
            issues.append({"code": "invalid_plugin_hook_candidate", "path": relative})
            continue
        hook_id = str(candidate["id"])
        if hook_id in seen:
            issues.append({"code": "invalid_plugin_hook_candidate", "path": relative})
        seen.add(hook_id)
    return issues


def _captured_hook_candidates(files: Sequence[Mapping[str, object]]) -> tuple[Mapping[str, object], ...]:
    hands = {str(item["id"]): item for item in _captured_hands_candidates(files)}
    candidates: list[Mapping[str, object]] = []
    for item in files:
        relative = str(item.get("relative_path", ""))
        parts = PurePosixPath(relative).parts
        if len(parts) == 3 and parts[0] == "hooks" and parts[2] == "hook.json":
            raw = base64.b64decode(str(item.get("content_base64", "")), validate=True)
            candidates.append(_parse_hook_descriptor(raw, expected_id=parts[1], hands=hands))
    return tuple(sorted(candidates, key=lambda item: (str(item["event"]), int(item["order"]), str(item["id"]))))


def captured_hook_candidates(files: object) -> tuple[Mapping[str, object], ...]:
    """Re-parse immutable raw package files for a downstream review authority."""

    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        raise PluginPackageIntakeError("Plugin raw package files are invalid")
    if any(not isinstance(item, Mapping) for item in files):
        raise PluginPackageIntakeError("Plugin raw package files are invalid")
    return _captured_hook_candidates(files)


def _parse_hook_descriptor(
    raw: bytes,
    *,
    expected_id: str,
    hands: Mapping[str, Mapping[str, object]],
) -> Mapping[str, object]:
    if len(raw) > 16 * 1024:
        raise PluginPackageIntakeError("Plugin Hook descriptor exceeds the size limit")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PluginPackageIntakeError("Plugin Hook descriptor must be valid UTF-8 JSON") from error
    fields = {
        "schema_version", "id", "hand_id", "event", "order", "sync", "timeout_ms",
        "metadata_projection", "recursion",
    }
    if not isinstance(payload, Mapping) or set(payload) != fields or payload.get("schema_version") != "1.0.0":
        raise PluginPackageIntakeError("Plugin Hook descriptor fields are invalid")
    hook_id = payload.get("id")
    if not isinstance(hook_id, str) or hook_id != expected_id or not _HAND_ID.fullmatch(hook_id):
        raise PluginPackageIntakeError("Plugin Hook descriptor id is invalid")
    hand_id = payload.get("hand_id")
    hand = hands.get(hand_id) if isinstance(hand_id, str) else None
    if hand is None:
        raise PluginPackageIntakeError("Plugin Hook Hand binding is invalid")
    if (
        hand.get("runtime") != "powershell-stdio-v1"
        or hand.get("effect") != "read"
        or hand.get("operation_semantics") != "read_only"
        or hand.get("requested_resources") != []
        or hand.get("input_schema") != _HOOK_INPUT_SCHEMA
        or hand.get("output_schema") != _HOOK_OUTPUT_SCHEMA
    ):
        raise PluginPackageIntakeError("Plugin Hook Hand contract is invalid")
    event = payload.get("event")
    order = payload.get("order")
    synchronous = payload.get("sync")
    timeout_ms = payload.get("timeout_ms")
    if event not in _CODEX_HOOK_EVENTS:
        raise PluginPackageIntakeError("Plugin Hook event is invalid")
    if not isinstance(order, int) or isinstance(order, bool) or not 0 <= order <= 4096:
        raise PluginPackageIntakeError("Plugin Hook order is invalid")
    if not isinstance(synchronous, bool):
        raise PluginPackageIntakeError("Plugin Hook sync is invalid")
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or not 1 <= timeout_ms <= 60_000:
        raise PluginPackageIntakeError("Plugin Hook timeout is invalid")
    if payload.get("metadata_projection") != "codex-hook-v1" or payload.get("recursion") != "deny":
        raise PluginPackageIntakeError("Plugin Hook execution policy is invalid")
    return {
        "schema_version": "1.0.0", "id": hook_id, "hand_id": hand_id,
        "event": event, "order": order, "sync": synchronous, "timeout_ms": timeout_ms,
        "metadata_projection": "codex-hook-v1", "recursion": "deny",
    }


def _parse_hand_descriptor(
    raw: bytes,
    *,
    expected_id: str,
    files: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    if len(raw) > 16 * 1024:
        raise PluginPackageIntakeError("Plugin Hands descriptor exceeds the size limit")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PluginPackageIntakeError("Plugin Hands descriptor must be valid UTF-8 JSON") from error
    fields = {
        "schema_version", "id", "runtime", "entrypoint", "input_schema", "output_schema",
        "effect", "operation_semantics", "requested_resources",
    }
    if not isinstance(payload, Mapping) or set(payload) != fields or payload.get("schema_version") != "1.0.0":
        raise PluginPackageIntakeError("Plugin Hands descriptor fields are invalid")
    hand_id = payload.get("id")
    if not isinstance(hand_id, str) or hand_id != expected_id or not _HAND_ID.fullmatch(hand_id):
        raise PluginPackageIntakeError("Plugin Hands descriptor id is invalid")
    runtime = payload.get("runtime")
    if runtime not in {"python-stdio-v1", "powershell-stdio-v1"}:
        raise PluginPackageIntakeError("Plugin Hands descriptor runtime is invalid")
    entrypoint = _hand_entrypoint(payload.get("entrypoint"), expected_id, files)
    input_schema = _hand_schema(payload.get("input_schema"), "input_schema")
    output_schema = _hand_schema(payload.get("output_schema"), "output_schema")
    effect = payload.get("effect")
    semantics = payload.get("operation_semantics")
    if effect not in {"read", "write"}:
        raise PluginPackageIntakeError("Plugin Hands descriptor effect is invalid")
    if (effect == "read" and semantics != "read_only") or (effect == "write" and semantics != "receipt_required"):
        raise PluginPackageIntakeError("Plugin Hands descriptor operation semantics are invalid")
    resources = payload.get("requested_resources")
    if not isinstance(resources, list) or any(not isinstance(item, str) for item in resources):
        raise PluginPackageIntakeError("Plugin Hands requested_resources are invalid")
    if len(resources) != len(set(resources)) or set(resources) - {"workspace_input", "workspace_output"}:
        raise PluginPackageIntakeError("Plugin Hands requested_resources are invalid")
    if effect == "read" and "workspace_output" in resources:
        raise PluginPackageIntakeError("read-only Plugin Hands cannot request workspace_output")
    return {
        "schema_version": "1.0.0",
        "id": hand_id,
        "runtime": runtime,
        "entrypoint": entrypoint,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "effect": effect,
        "operation_semantics": semantics,
        "requested_resources": list(resources),
    }


def _hand_entrypoint(value: object, hand_id: str, files: Sequence[Mapping[str, object]]) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_RELATIVE_PATH or "\x00" in value:
        raise PluginPackageIntakeError("Plugin Hands entrypoint is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or len(path.parts) < 2 or path.parts[0] != "payload" or any(part in {".", ".."} for part in path.parts):
        raise PluginPackageIntakeError("Plugin Hands entrypoint is invalid")
    package_path = f"hands/{hand_id}/{path.as_posix()}"
    if not any(item.get("relative_path") == package_path for item in files):
        raise PluginPackageIntakeError("Plugin Hands entrypoint must name a captured payload file")
    return path.as_posix()


def _hand_schema(value: object, label: str) -> dict[str, object]:
    fields = {"type", "properties", "required", "additionalProperties"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise PluginPackageIntakeError(f"Plugin Hands {label} is invalid")
    if value.get("type") != "object" or value.get("additionalProperties") is not False:
        raise PluginPackageIntakeError(f"Plugin Hands {label} is invalid")
    properties = value.get("properties")
    required = value.get("required")
    if not isinstance(properties, Mapping) or len(properties) > 64:
        raise PluginPackageIntakeError(f"Plugin Hands {label} properties are invalid")
    if any(not isinstance(key, str) or not key or len(key) > 128 or not isinstance(item, Mapping) for key, item in properties.items()):
        raise PluginPackageIntakeError(f"Plugin Hands {label} properties are invalid")
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise PluginPackageIntakeError(f"Plugin Hands {label} required is invalid")
    if len(required) != len(set(required)) or any(item not in properties for item in required):
        raise PluginPackageIntakeError(f"Plugin Hands {label} required is invalid")
    if _contains_external_schema_reference(value):
        raise PluginPackageIntakeError(f"Plugin Hands {label} cannot contain $ref")
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _contains_external_schema_reference(value: object) -> bool:
    if isinstance(value, Mapping):
        return "$ref" in value or any(_contains_external_schema_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_external_schema_reference(item) for item in value)
    return False


def _parse_mcp_server_reference(raw: bytes, *, expected_id: str) -> Mapping[str, object]:
    if len(raw) > 16 * 1024:
        raise PluginPackageIntakeError("MCP server reference exceeds the size limit")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PluginPackageIntakeError("MCP server reference must be valid UTF-8 JSON") from error
    fields = {
        "schema_version", "server_id", "approval_revision", "manifest_revision",
        "endpoint_identity", "credential_subject_id", "transport_generation",
    }
    if not isinstance(payload, Mapping) or set(payload) != fields or payload.get("schema_version") != "1.0.0":
        raise PluginPackageIntakeError("MCP server reference fields are invalid")
    server_id = payload.get("server_id")
    if not isinstance(server_id, str) or server_id != expected_id or not _MCP_SERVER_ID.fullmatch(server_id):
        raise PluginPackageIntakeError("MCP server reference id is invalid")
    revisions = (payload.get("approval_revision"), payload.get("manifest_revision"), payload.get("transport_generation"))
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in revisions):
        raise PluginPackageIntakeError("MCP server reference revision is invalid")
    endpoint = _required_text(payload.get("endpoint_identity"), "endpoint_identity", maximum=256)
    subject = _required_text(payload.get("credential_subject_id"), "credential_subject_id", maximum=256)
    return {
        "schema_version": "1.0.0", "server_id": server_id,
        "approval_revision": revisions[0], "manifest_revision": revisions[1],
        "endpoint_identity": endpoint, "credential_subject_id": subject,
        "transport_generation": revisions[2],
    }


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PluginPackageIntakeError("JSON object contains duplicate fields")
        result[key] = value
    return result


def _parse_declarative_tool(raw: bytes, *, expected_id: str) -> Mapping[str, object]:
    if len(raw) > 128 * 1024:
        raise PluginPackageIntakeError("declarative tool definition exceeds the size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PluginPackageIntakeError("declarative tool definition must be valid UTF-8 JSON") from error
    if not isinstance(payload, Mapping) or set(payload) != {"id", "version", "description", "entries"}:
        raise PluginPackageIntakeError("declarative tool definition fields are invalid")
    tool_id = payload.get("id")
    if not isinstance(tool_id, str) or tool_id != expected_id or not _TOOL_ID.fullmatch(tool_id):
        raise PluginPackageIntakeError("declarative tool id is invalid")
    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or not 1 <= version <= 1_000_000:
        raise PluginPackageIntakeError("declarative tool version is invalid")
    description = _required_text(payload.get("description"), "declarative tool description", maximum=2048)
    entries = payload.get("entries")
    if not isinstance(entries, Mapping) or not 1 <= len(entries) <= 256:
        raise PluginPackageIntakeError("declarative tool entries are invalid")
    canonical_entries: dict[str, str] = {}
    for key, value in entries.items():
        if not isinstance(key, str) or not key or len(key) > 256 or not isinstance(value, str) or len(value) > 4096:
            raise PluginPackageIntakeError("declarative tool entry is invalid")
        if _unsafe_lookup_literal(key) or _unsafe_lookup_literal(value):
            raise PluginPackageIntakeError("declarative tool entry cannot contain a locator")
        canonical_entries[key] = value
    return {"id": tool_id, "version": version, "description": description, "entries": canonical_entries}


def _unsafe_lookup_literal(value: str) -> bool:
    normalized = value.strip().lower()
    return (
        "://" in normalized
        or normalized.startswith(("file:", "/", "\\\\"))
        or bool(re.match(r"^[a-z]:[\\\\/]", normalized))
    )


def _parse_manifest_bytes(raw: bytes) -> Mapping[str, object]:
    if len(raw) > 64 * 1024:
        raise PluginPackageIntakeError("plugin.json exceeds the size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PluginPackageIntakeError("plugin.json must be valid UTF-8 JSON") from error
    if not isinstance(payload, Mapping):
        raise PluginPackageIntakeError("plugin.json must be an object")
    _required_text(payload.get("description"), "description", maximum=2048)
    return dict(payload)


def _put_immutable(
    uow: SQLiteStructuredRecordUnitOfWork,
    collection: str,
    object_id: str,
    payload: Mapping[str, object],
) -> None:
    current = uow.read(collection, object_id)
    if current is None:
        uow.put(collection, object_id, payload, expected_revision=0)
        return
    if dict(current.payload) != dict(payload):
        raise PluginPackageIntakeConflict(f"immutable {collection} identity drifted")


def _require_replayed_raw(
    uow: SQLiteStructuredRecordUnitOfWork,
    command: SQLiteStructuredRecord,
    inspected: _InspectedPackage,
) -> None:
    package_record_id = _required_payload_text(command.payload, "package_record_id")
    raw = _required_record(uow.read(_RAW, package_record_id), "raw package")
    if dict(raw.payload) != dict(inspected.raw):
        raise PluginPackageIntakeConflict("Plugin package command source bytes drifted")


def _result(
    state_record: SQLiteStructuredRecord,
    report: Mapping[str, object],
    manifest: Mapping[str, object],
    *,
    replayed: bool,
) -> dict[str, object]:
    return {
        "plugin_id": state_record.object_id,
        "package_record_id": _required_payload_text(state_record.payload, "package_record_id"),
        "state": dict(state_record.payload),
        "state_revision": state_record.revision,
        "compatibility_report": dict(report),
        "normalized_manifest": dict(manifest),
        "replayed": replayed,
    }


def _replay(
    command: SQLiteStructuredRecord,
    operation: str,
    plugin_id: str,
    package_record_id: str,
) -> dict[str, object]:
    payload = command.payload
    if (
        payload.get("operation") != operation
        or payload.get("plugin_id") != plugin_id
        or payload.get("package_record_id") != package_record_id
        or not isinstance(payload.get("result"), Mapping)
    ):
        raise PluginPackageIntakeConflict("Plugin package command identity drifted")
    return dict(payload["result"]) | {"replayed": True}


def _require_bundle_identity(
    plugin_id: str,
    package_record_id: str,
    raw: SQLiteStructuredRecord,
    report: SQLiteStructuredRecord,
    manifest: SQLiteStructuredRecord,
) -> None:
    for record in (raw, report, manifest):
        if record.object_id != package_record_id or record.payload.get("plugin_id") != plugin_id:
            raise PluginPackageIntakeError("Plugin package authority identity drifted")


def _require_same_package(state: SQLiteStructuredRecord, package_record_id: str) -> None:
    if state.payload.get("package_record_id") != package_record_id:
        raise PluginPackageIntakeConflict("Plugin version replacement requires a future upgrade Gate")


def candidate_stage_receipt_object_id(plugin_id: str, candidate_package_record_id: str) -> str:
    """Return the stable receipt id for one plugin's immutable upgrade candidate.

    Candidate package record ids already encode the package identity as
    ``{plugin_id}~{version}``; validating both inputs makes that compact record
    id a collision-free, storage-safe receipt key without persisting a path.
    """

    identity = _plugin_id(plugin_id)
    return _package_record_id(candidate_package_record_id, plugin_id=identity)


def _package_record_id(value: object, *, plugin_id: str) -> str:
    record_id = _required_text(value, "package_record_id", maximum=127)
    prefix, separator, version = record_id.partition("~")
    if separator != "~" or prefix != plugin_id or not version or "~" in version:
        raise PluginPackageIntakeError("Plugin package record id is invalid")
    _version(version)
    return record_id


def _require_upgrade_stage_receipt(
    receipt: SQLiteStructuredRecord,
    receipt_id: str,
    expected_payload: Mapping[str, object],
) -> None:
    if receipt.object_id != receipt_id or receipt.revision != 1 or dict(receipt.payload) != dict(expected_payload):
        raise PluginPackageIntakeConflict("Plugin upgrade staging receipt drifted")


def _required_record(record: SQLiteStructuredRecord | None, label: str) -> SQLiteStructuredRecord:
    if record is None:
        raise PluginPackageIntakeError(f"Plugin {label} is missing")
    return record


def _selected_directory(value: str, source_root: Path) -> Path:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise PluginPackageIntakeError("source_path is required")
    path = Path(value).expanduser()
    if path.is_symlink():
        raise PluginPackageIntakeError("Plugin package root cannot be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise PluginPackageIntakeError("Plugin package directory is missing") from error
    if not resolved.is_dir():
        raise PluginPackageIntakeError("Plugin package source must be a directory")
    try:
        relative = resolved.relative_to(source_root)
    except ValueError as error:
        raise PluginPackageIntakeError("Plugin package source is outside the governed inbox") from error
    if len(relative.parts) != 1:
        raise PluginPackageIntakeError("Plugin package must be a direct child of the governed inbox")
    return resolved


def _validate_relative_path(relative: str) -> None:
    path = PurePosixPath(relative)
    if (
        not relative
        or len(relative) > _MAX_RELATIVE_PATH
        or path.is_absolute()
        or ".." in path.parts
        or not path.parts
    ):
        raise PluginPackageIntakeError("Plugin package relative path is invalid")


def _plugin_id(value: object) -> str:
    if not isinstance(value, str) or not _PLUGIN_ID.fullmatch(value):
        raise PluginPackageIntakeError("Plugin id must use lowercase package identity")
    return value


def _version(value: object) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise PluginPackageIntakeError("Plugin version must use semantic version form")
    return value


def _command_id(value: object) -> str:
    if not isinstance(value, str) or not _COMMAND_ID.fullmatch(value):
        raise PluginPackageIntakeError("command_id is invalid")
    return value


def _required_text(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise PluginPackageIntakeError(f"{label} is invalid")
    return value.strip()


def _required_payload_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise PluginPackageIntakeError(f"Plugin package {key} is invalid")
    return value
