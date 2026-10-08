"""Durable admission for the deliberately tiny declarative Plugin Tool class.

Plugins provide immutable lookup data only.  This module owns review state and
the provider; it never imports, materializes, or executes package code.
"""
from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from threading import RLock

from core.ai_tooling import ToolDefinition, ToolRetryPolicy
from core.storage_provider import SQLiteStructuredRecord, SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict

from .package_intake import PluginPackageIntakeConflict, PluginPackageIntakeError


_RAW = "plugin_raw_packages"
_REPORTS = "plugin_compatibility_reports"
_STATES = "plugin_package_states"
_REVIEWS = "plugin_declarative_tool_reviews"
_ACTIVATIONS = "plugin_declarative_tool_activations"
_COMMANDS = "plugin_declarative_tool_commands"
_LOCK = RLock()


@dataclass(frozen=True, slots=True)
class PluginToolBinding:
    plugin_id: str
    definition: ToolDefinition
    provider: "LocalLookupToolProvider"


class LocalLookupToolProvider:
    """OS-owned read-only executor for one frozen Plugin lookup table."""

    def __init__(self, entries: Mapping[str, str]) -> None:
        self._entries = dict(entries)

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        arguments = request.get("arguments", request.get("input", request))
        if not isinstance(arguments, Mapping) or set(arguments) != {"key"}:
            raise PluginPackageIntakeError("declarative lookup input must contain only key")
        key = arguments.get("key")
        if not isinstance(key, str) or not key or len(key) > 256:
            raise PluginPackageIntakeError("declarative lookup key is invalid")
        if key not in self._entries:
            return {"summary": "local lookup did not find a value", "result": {"found": False, "key": key}, "evidence_refs": ()}
        return {
            "summary": "local lookup completed",
            "result": {"found": True, "key": key, "value": self._entries[key]},
            "evidence_refs": (),
        }


class PluginToolActivation:
    """SQLite-backed review and reversible activation for local lookup Tools."""

    def __init__(self, records: SQLiteStructuredRecordStore, *, now: str) -> None:
        self._records = records
        self._now = _text(now, "now", 96)

    def review(self, plugin_id: str, *, tool_ids: Sequence[str], expected_state_revision: int, command_id: str, confirm: bool, reason: str) -> dict[str, object]:
        identity, tools, command = _text(plugin_id, "plugin_id", 64), _tool_ids(tool_ids), _command_id(command_id)
        if confirm is not True:
            raise PluginPackageIntakeError("Plugin Tool review requires explicit confirmation")
        reason = _text(reason, "reason", 500)
        try:
            with self._records.begin() as uow:
                replay = uow.read(_COMMANDS, command)
                if replay is not None:
                    return _replay(replay, "review", identity, tools)
                state = _required(uow.read(_STATES, identity), "package state")
                if state.revision != _revision(expected_state_revision, "expected_state_revision", allow_zero=False):
                    raise PluginPackageIntakeConflict("Plugin package state revision conflict")
                package_id = _installed_package_id(state)
                raw = _required(uow.read(_RAW, package_id), "raw package")
                report = _required(uow.read(_REPORTS, package_id), "compatibility report")
                if report.payload.get("compatible") is not True:
                    raise PluginPackageIntakeError("quarantined Plugin package cannot be reviewed")
                captured = _captured_tools(raw.payload)
                if tuple(item["id"] for item in captured) != tools:
                    raise PluginPackageIntakeError("reviewed declarative Tools are not the captured package Tools")
                payload = {"schema_version": "1.0.0", "plugin_id": identity, "package_record_id": package_id, "tools": captured, "tool_files": _captured_tool_files(raw.payload), "decision": "approved_disabled", "reviewed_by": "local-user", "reviewed_at": self._now, "reason": reason}
                current = uow.read(_REVIEWS, package_id)
                if current is None:
                    saved = uow.put(_REVIEWS, package_id, payload, expected_revision=0)
                elif dict(current.payload) == payload:
                    saved = current
                else:
                    raise PluginPackageIntakeConflict("Plugin Tool review identity drifted")
                result = _result(saved, "review", replayed=False)
                uow.put(_COMMANDS, command, {"operation": "review", "plugin_id": identity, "tool_ids": list(tools), "result": result}, expected_revision=0)
                uow.commit()
                return result
        except SQLiteUnitOfWorkConflict as error:
            raise PluginPackageIntakeConflict(str(error)) from error

    def activate(self, plugin_id: str, *, expected_review_revision: int, expected_activation_revision: int, command_id: str, confirm: bool) -> dict[str, object]:
        if confirm is not True:
            raise PluginPackageIntakeError("Plugin Tool activation requires explicit confirmation")
        identity, command = _text(plugin_id, "plugin_id", 64), _command_id(command_id)
        review_revision = _revision(expected_review_revision, "expected_review_revision", allow_zero=False)
        activation_revision = _revision(expected_activation_revision, "expected_activation_revision")
        with _LOCK:
            return self._activate(identity, review_revision, activation_revision, command)

    def _activate(self, identity: str, review_revision: int, activation_revision: int, command: str) -> dict[str, object]:
        try:
            with self._records.begin() as uow:
                replay = uow.read(_COMMANDS, command)
                state = _required(uow.read(_STATES, identity), "package state")
                package_id = _installed_package_id(state)
                review = _required(uow.read(_REVIEWS, package_id), "Tool review")
                tools = _review_tools(review, identity, package_id)
                if replay is not None:
                    return _replay(replay, "activate", identity, tuple(item["id"] for item in tools))
                if review.revision != review_revision:
                    raise PluginPackageIntakeConflict("Plugin Tool review revision conflict")
                raw = _required(uow.read(_RAW, package_id), "raw package")
                tool_files = _review_tool_files(review)
                if _captured_tools(raw.payload) != tools or _captured_tool_files(raw.payload) != tool_files:
                    raise PluginPackageIntakeConflict("Plugin Tool raw bytes drifted")
                current = uow.read(_ACTIVATIONS, identity)
                if (current.revision if current else 0) != activation_revision:
                    raise PluginPackageIntakeConflict("Plugin Tool activation revision conflict")
                payload = {"schema_version": "1.0.0", "plugin_id": identity, "package_record_id": package_id, "review_revision": review.revision, "tools": tools, "tool_files": tool_files, "status": "active", "activated_at": self._now, "disabled_at": None}
                if current is None:
                    saved = uow.put(_ACTIVATIONS, identity, payload, expected_revision=0)
                elif current.payload.get("package_record_id") != package_id:
                    raise PluginPackageIntakeConflict("Plugin Tool upgrade requires a future Gate")
                elif dict(current.payload) == payload:
                    saved = current
                else:
                    saved = uow.put(_ACTIVATIONS, identity, payload, expected_revision=current.revision)
                result = _result(saved, "activation", replayed=False)
                uow.put(_COMMANDS, command, {"operation": "activate", "plugin_id": identity, "tool_ids": [item["id"] for item in tools], "result": result}, expected_revision=0)
                uow.commit()
                return result
        except SQLiteUnitOfWorkConflict as error:
            raise PluginPackageIntakeConflict(str(error)) from error

    def disable(self, plugin_id: str, *, expected_activation_revision: int, command_id: str, confirm: bool, reason: str) -> dict[str, object]:
        if confirm is not True:
            raise PluginPackageIntakeError("Plugin Tool disable requires explicit confirmation")
        identity, command = _text(plugin_id, "plugin_id", 64), _command_id(command_id)
        expected = _revision(expected_activation_revision, "expected_activation_revision", allow_zero=False)
        reason = _text(reason, "reason", 500)
        with _LOCK:
            try:
                with self._records.begin() as uow:
                    replay = uow.read(_COMMANDS, command)
                    current = _required(uow.read(_ACTIVATIONS, identity), "Tool activation")
                    tools = _activation_tools(current, identity)
                    if replay is not None:
                        return _replay(replay, "disable", identity, tuple(item["id"] for item in tools))
                    if current.revision != expected:
                        raise PluginPackageIntakeConflict("Plugin Tool activation revision conflict")
                    payload = dict(current.payload) | {"status": "disabled", "disabled_at": self._now, "disable_reason": reason}
                    saved = uow.put(_ACTIVATIONS, identity, payload, expected_revision=current.revision)
                    result = _result(saved, "activation", replayed=False)
                    uow.put(_COMMANDS, command, {"operation": "disable", "plugin_id": identity, "tool_ids": [item["id"] for item in tools], "result": result}, expected_revision=0)
                    uow.commit()
                    return result
            except SQLiteUnitOfWorkConflict as error:
                raise PluginPackageIntakeConflict(str(error)) from error

    def active_tools(self, plugin_ids: Sequence[str]) -> tuple[PluginToolBinding, ...]:
        allowed = frozenset(_text(item, "plugin_id", 64) for item in plugin_ids)
        bindings: list[PluginToolBinding] = []
        for activation in self._records.list(_ACTIVATIONS):
            if activation.object_id not in allowed or activation.payload.get("status") != "active":
                continue
            try:
                identity = activation.object_id
                tools = _activation_tools(activation, identity)
                package_id = _text(activation.payload.get("package_record_id"), "package_record_id", 160)
                review = _required(self._records.read(_REVIEWS, package_id), "Tool review")
                if review.revision != activation.payload.get("review_revision") or _review_tools(review, identity, package_id) != tools:
                    continue
                raw = _required(self._records.read(_RAW, package_id), "raw package")
                if _review_tool_files(review) != _activation_tool_files(activation) or _captured_tools(raw.payload) != tools or _captured_tool_files(raw.payload) != _activation_tool_files(activation):
                    continue
                bindings.extend(PluginToolBinding(identity, _definition(identity, tool), LocalLookupToolProvider(_entries(tool))) for tool in tools)
            except (PluginPackageIntakeError, PluginPackageIntakeConflict):
                continue
        return tuple(sorted(bindings, key=lambda binding: (binding.plugin_id, binding.definition.tool_id)))

    def all_active_tools(self) -> tuple[PluginToolBinding, ...]:
        return self.active_tools(tuple(record.object_id for record in self._records.list(_ACTIVATIONS)))

    def active_contributions(self, plugin_ids: Sequence[str]) -> tuple[PluginToolBinding, ...]:
        """Composition seam: return only durable, fail-closed active bindings."""
        return self.active_tools(plugin_ids)


def _definition(plugin_id: str, tool: Mapping[str, object]) -> ToolDefinition:
    tool_id, version, description = _text(tool.get("id"), "tool id", 128), tool.get("version"), _text(tool.get("description"), "description", 2048)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PluginPackageIntakeError("declarative tool version is invalid")
    return ToolDefinition(tool_id=tool_id, version=version, display_name=tool_id, description=description, source="plugin", owner_id=plugin_id, effect="read", data_classes=("unclassified",), destination="local", input_schema_uri="crp://schemas/plugin/local-lookup-input-v1", output_schema_uri="crp://schemas/plugin/local-lookup-output-v1", receipt_schema_uri=None, operation_semantics="read_only", execution_mode="parallel", resource_locks=(), idempotency="idempotent", retry_policy=ToolRetryPolicy(max_attempts=1, backoff_ms=0, retryable_error_codes=()), verification_tool_id=None, compensation_tool_id=None, mutability="read_only", egress_class="none", network_scope=(), data_egress_scope=(), timeout_ms=5_000, required_scopes=(), boundary_requirements=())


def _captured_tools(raw: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    files = raw.get("files")
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        raise PluginPackageIntakeError("Plugin raw files are invalid")
    tools: list[Mapping[str, object]] = []
    for item in files:
        if not isinstance(item, Mapping) or not isinstance(item.get("relative_path"), str):
            raise PluginPackageIntakeError("Plugin raw file is invalid")
        path = str(item["relative_path"]).split("/")
        if len(path) != 3 or path[0] != "tools" or path[2] != "tool.json":
            continue
        try:
            content = base64.b64decode(_text(item.get("content_base64"), "content_base64", 6_000_000), validate=True)
            payload = json.loads(content.decode("utf-8"))
        except Exception as error:
            raise PluginPackageIntakeError("declarative Tool raw content is invalid") from error
        if len(content) != item.get("size_bytes"):
            raise PluginPackageIntakeConflict("declarative Tool raw bytes drifted")
        tools.append(_validate_tool_payload(payload, path[1]))
    tools.sort(key=lambda tool: str(tool["id"]))
    if not tools or len({item["id"] for item in tools}) != len(tools):
        raise PluginPackageIntakeError("declarative Tool package is invalid")
    return tuple(tools)


def _captured_tool_files(raw: Mapping[str, object]) -> dict[str, str]:
    files = raw.get("files")
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        raise PluginPackageIntakeError("Plugin raw files are invalid")
    captured: dict[str, str] = {}
    for item in files:
        if not isinstance(item, Mapping) or not isinstance(item.get("relative_path"), str):
            raise PluginPackageIntakeError("Plugin raw file is invalid")
        relative = item["relative_path"]
        parts = relative.split("/")
        if len(parts) == 3 and parts[0] == "tools" and parts[2] == "tool.json":
            content = _text(item.get("content_base64"), "content_base64", 6_000_000)
            try:
                decoded = base64.b64decode(content, validate=True)
            except Exception as error:
                raise PluginPackageIntakeError("declarative Tool raw content is invalid") from error
            if len(decoded) != item.get("size_bytes") or relative in captured:
                raise PluginPackageIntakeConflict("declarative Tool raw bytes drifted")
            captured[relative] = content
    if not captured:
        raise PluginPackageIntakeError("declarative Tool package is invalid")
    return dict(sorted(captured.items()))


def _validate_tool_payload(payload: object, expected_id: str) -> Mapping[str, object]:
    if not isinstance(payload, Mapping) or set(payload) != {"id", "version", "description", "entries"}:
        raise PluginPackageIntakeError("declarative Tool definition is invalid")
    tool_id = _text(payload.get("id"), "tool id", 128)
    if tool_id != expected_id or not all(part for part in tool_id.split(".")):
        raise PluginPackageIntakeError("declarative Tool id is invalid")
    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or not 1 <= version <= 1_000_000:
        raise PluginPackageIntakeError("declarative Tool version is invalid")
    entries = payload.get("entries")
    if not isinstance(entries, Mapping) or not 1 <= len(entries) <= 256:
        raise PluginPackageIntakeError("declarative Tool entries are invalid")
    normalized: dict[str, str] = {}
    for key, value in entries.items():
        key, value = _text(key, "entry key", 256), _text(value, "entry value", 4096)
        if _locator(key) or _locator(value):
            raise PluginPackageIntakeError("declarative Tool entries cannot contain locators")
        normalized[key] = value
    return {"id": tool_id, "version": version, "description": _text(payload.get("description"), "description", 2048), "entries": normalized}


def _locator(value: str) -> bool:
    low = value.lower()
    return "://" in low or low.startswith(("file:", "/", "\\\\")) or (len(low) > 2 and low[1] == ":" and low[2] in "\\\\/")


def _entries(tool: Mapping[str, object]) -> Mapping[str, str]:
    value = tool.get("entries")
    if not isinstance(value, Mapping):
        raise PluginPackageIntakeError("declarative Tool entries are invalid")
    return {str(key): str(item) for key, item in value.items()}


def _installed_package_id(state: SQLiteStructuredRecord) -> str:
    if state.payload.get("status") != "installed_disabled" or state.payload.get("enabled") is not False:
        raise PluginPackageIntakeError("Plugin package must remain installed disabled")
    return _text(state.payload.get("package_record_id"), "package_record_id", 160)


def _review_tools(record: SQLiteStructuredRecord, plugin_id: str, package_id: str) -> tuple[Mapping[str, object], ...]:
    if record.payload.get("plugin_id") != plugin_id or record.payload.get("package_record_id") != package_id or record.payload.get("decision") != "approved_disabled":
        raise PluginPackageIntakeConflict("Plugin Tool review identity drifted")
    return _tools(record.payload.get("tools"))


def _review_tool_files(record: SQLiteStructuredRecord) -> dict[str, str]:
    return _tool_files(record.payload.get("tool_files"))


def _activation_tools(record: SQLiteStructuredRecord, plugin_id: str) -> tuple[Mapping[str, object], ...]:
    if record.payload.get("plugin_id") != plugin_id:
        raise PluginPackageIntakeConflict("Plugin Tool activation identity drifted")
    return _tools(record.payload.get("tools"))


def _activation_tool_files(record: SQLiteStructuredRecord) -> dict[str, str]:
    return _tool_files(record.payload.get("tool_files"))


def _tools(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise PluginPackageIntakeError("declarative Tools are invalid")
    tools = tuple(_validate_tool_payload(item, _text(item.get("id") if isinstance(item, Mapping) else None, "tool id", 128)) for item in value)
    if tuple(sorted(item["id"] for item in tools)) != tuple(item["id"] for item in tools) or len({item["id"] for item in tools}) != len(tools):
        raise PluginPackageIntakeError("declarative Tools are invalid")
    return tools


def _tool_files(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise PluginPackageIntakeError("declarative Tool files are invalid")
    result: dict[str, str] = {}
    for relative, content in value.items():
        relative, content = _text(relative, "tool file path", 240), _text(content, "content_base64", 6_000_000)
        parts = relative.split("/")
        if len(parts) != 3 or parts[0] != "tools" or parts[2] != "tool.json":
            raise PluginPackageIntakeError("declarative Tool file path is invalid")
        try:
            base64.b64decode(content, validate=True)
        except Exception as error:
            raise PluginPackageIntakeError("declarative Tool raw content is invalid") from error
        result[relative] = content
    return dict(sorted(result.items()))


def _tool_ids(value: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise PluginPackageIntakeError("tool_ids must be a non-empty array")
    ids = tuple(sorted(_text(item, "tool id", 128) for item in value))
    if len(set(ids)) != len(ids):
        raise PluginPackageIntakeError("tool_ids are invalid")
    return ids


def _result(record: SQLiteStructuredRecord, label: str, *, replayed: bool) -> dict[str, object]:
    return {label: dict(record.payload), f"{label}_revision": record.revision, "replayed": replayed}


def _replay(command: SQLiteStructuredRecord, operation: str, plugin_id: str, tool_ids: tuple[str, ...]) -> dict[str, object]:
    if command.payload.get("operation") != operation or command.payload.get("plugin_id") != plugin_id or command.payload.get("tool_ids") != list(tool_ids) or not isinstance(command.payload.get("result"), Mapping):
        raise PluginPackageIntakeConflict("Plugin Tool command identity drifted")
    return dict(command.payload["result"]) | {"replayed": True}


def _required(record: SQLiteStructuredRecord | None, label: str) -> SQLiteStructuredRecord:
    if record is None:
        raise PluginPackageIntakeError(f"Plugin {label} is missing")
    return record


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise PluginPackageIntakeError(f"{label} is invalid")
    return value.strip()


def _command_id(value: object) -> str:
    value = _text(value, "command_id", 128)
    if len(value) < 8 or not value[0].isalnum() or any(not (item.isalnum() or item in "._~-") for item in value):
        raise PluginPackageIntakeError("command_id is invalid")
    return value


def _revision(value: object, label: str, *, allow_zero: bool = True) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < (0 if allow_zero else 1):
        raise PluginPackageIntakeError(f"{label} is invalid")
    return value
