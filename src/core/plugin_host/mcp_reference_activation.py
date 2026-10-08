"""Durable, fail-closed admission for Plugin references to approved MCP servers.

The Plugin contributes only a frozen identity tuple.  Connection details,
credentials, transport selection, and Tool policies remain exclusively owned by
the existing approved-server authority.
"""
from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import RLock
from typing import Protocol

from core.storage_provider import SQLiteStructuredRecord, SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict

from .package_intake import PluginPackageIntakeConflict, PluginPackageIntakeError


_RAW = "plugin_raw_packages"
_STATES = "plugin_package_states"
_REVIEWS = "plugin_mcp_reference_reviews"
_ACTIVATIONS = "plugin_mcp_reference_activations"
_COMMANDS = "plugin_mcp_reference_commands"
_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")
_CANDIDATE_FIELDS = frozenset({"schema_version", "server_id", "approval_revision", "manifest_revision", "endpoint_identity", "credential_subject_id", "transport_generation"})
_LOCK = RLock()


class MCPApprovedServerSnapshotPort(Protocol):
    @property
    def servers(self) -> Sequence[object]: ...


@dataclass(frozen=True, slots=True)
class PluginMCPReferenceBinding:
    plugin_id: str
    server_id: str
    identity: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PluginMCPReferenceStatus:
    """Safe local projection that never exposes a reference's raw contents."""

    plugin_id: str
    status: str

    def __post_init__(self) -> None:
        if self.status not in {"active", "needs_review", "unavailable", "invalid"}:
            raise PluginPackageIntakeError("Plugin MCP reference status is invalid")


class PluginMCPReferenceActivation:
    """Review and activate a Plugin's reference to one enabled MCP server."""

    def __init__(self, records: SQLiteStructuredRecordStore, *, authority_loader: Callable[[], MCPApprovedServerSnapshotPort], now: str) -> None:
        self._records = records
        self._authority_loader = authority_loader
        self._now = _text(now, "now", 96)

    def review(self, plugin_id: str, *, expected_state_revision: int, command_id: str, confirm: bool, reason: str) -> dict[str, object]:
        if confirm is not True:
            raise PluginPackageIntakeError("Plugin MCP reference review requires explicit confirmation")
        plugin_id, command, reason = _text(plugin_id, "plugin_id", 64), _command_id(command_id), _text(reason, "reason", 500)
        expected = _revision(expected_state_revision, "expected_state_revision", allow_zero=False)
        try:
            with self._records.begin() as uow:
                replay = uow.read(_COMMANDS, command)
                state = _installed_state(uow.read(_STATES, plugin_id), plugin_id)
                raw = _required(uow.read(_RAW, _package_id(state)), "raw package")
                candidate = _candidate(raw, plugin_id)
                _approved_identity(self._authority_loader(), candidate)
                if replay is not None:
                    return _replay(replay, "review", plugin_id)
                if state.revision != expected:
                    raise PluginPackageIntakeConflict("Plugin package state revision conflict")
                payload = {"schema_version": "1.0.0", "plugin_id": plugin_id, "package_record_id": raw.object_id, "candidate": candidate, "reference_content_base64": _reference_bytes(raw), "decision": "approved_disabled", "reviewed_by": "local-user", "reviewed_at": self._now, "reason": reason}
                current = uow.read(_REVIEWS, raw.object_id)
                if current is None:
                    saved = uow.put(_REVIEWS, raw.object_id, payload, expected_revision=0)
                elif dict(current.payload) == payload:
                    saved = current
                else:
                    raise PluginPackageIntakeConflict("Plugin MCP reference review identity drifted")
                result = _result(saved, "review", replayed=False)
                uow.put(_COMMANDS, command, {"operation": "review", "plugin_id": plugin_id, "result": result}, expected_revision=0)
                uow.commit()
                return result
        except SQLiteUnitOfWorkConflict as error:
            raise PluginPackageIntakeConflict(str(error)) from error

    def activate(self, plugin_id: str, *, expected_review_revision: int, expected_activation_revision: int, command_id: str, confirm: bool) -> dict[str, object]:
        if confirm is not True:
            raise PluginPackageIntakeError("Plugin MCP reference activation requires explicit confirmation")
        plugin_id, command = _text(plugin_id, "plugin_id", 64), _command_id(command_id)
        review_expected = _revision(expected_review_revision, "expected_review_revision", allow_zero=False)
        activation_expected = _revision(expected_activation_revision, "expected_activation_revision")
        with _LOCK:
            try:
                with self._records.begin() as uow:
                    replay = uow.read(_COMMANDS, command)
                    if replay is not None:
                        return _replay(replay, "activate", plugin_id)
                    state = _installed_state(uow.read(_STATES, plugin_id), plugin_id)
                    raw = _required(uow.read(_RAW, _package_id(state)), "raw package")
                    review = _required(uow.read(_REVIEWS, raw.object_id), "MCP reference review")
                    candidate = _review_candidate(review, plugin_id, raw.object_id)
                    if review.revision != review_expected:
                        raise PluginPackageIntakeConflict("Plugin MCP reference review revision conflict")
                    if _candidate(raw, plugin_id) != candidate or _reference_bytes(raw) != review.payload.get("reference_content_base64"):
                        raise PluginPackageIntakeConflict("Plugin MCP reference raw bytes drifted")
                    _approved_identity(self._authority_loader(), candidate)
                    current = uow.read(_ACTIVATIONS, plugin_id)
                    if (current.revision if current else 0) != activation_expected:
                        raise PluginPackageIntakeConflict("Plugin MCP reference activation revision conflict")
                    payload = {"schema_version": "1.0.0", "plugin_id": plugin_id, "package_record_id": raw.object_id, "review_revision": review.revision, "candidate": candidate, "status": "active", "activated_at": self._now, "disabled_at": None}
                    if current is None:
                        saved = uow.put(_ACTIVATIONS, plugin_id, payload, expected_revision=0)
                    elif dict(current.payload) == payload:
                        saved = current
                    else:
                        saved = uow.put(_ACTIVATIONS, plugin_id, payload, expected_revision=current.revision)
                    result = _result(saved, "activation", replayed=False)
                    uow.put(_COMMANDS, command, {"operation": "activate", "plugin_id": plugin_id, "result": result}, expected_revision=0)
                    uow.commit()
                    return result
            except SQLiteUnitOfWorkConflict as error:
                raise PluginPackageIntakeConflict(str(error)) from error

    def active_references(self, plugin_ids: Sequence[str]) -> tuple[PluginMCPReferenceBinding, ...]:
        allowed = frozenset(_text(item, "plugin_id", 64) for item in plugin_ids)
        try:
            snapshot = self._authority_loader()
        except Exception:
            return ()
        bindings: list[PluginMCPReferenceBinding] = []
        for activation in self._records.list(_ACTIVATIONS):
            if activation.object_id not in allowed or activation.payload.get("status") != "active":
                continue
            try:
                state = _installed_state(self._records.read(_STATES, activation.object_id), activation.object_id)
                raw = _required(self._records.read(_RAW, _package_id(state)), "raw package")
                review = _required(self._records.read(_REVIEWS, raw.object_id), "MCP reference review")
                candidate = _review_candidate(review, activation.object_id, raw.object_id)
                if activation.payload.get("review_revision") != review.revision or activation.payload.get("candidate") != candidate or _candidate(raw, activation.object_id) != candidate or _reference_bytes(raw) != review.payload.get("reference_content_base64"):
                    continue
                _approved_identity(snapshot, candidate)
                bindings.append(PluginMCPReferenceBinding(activation.object_id, str(candidate["server_id"]), dict(candidate)))
            except (PluginPackageIntakeError, PluginPackageIntakeConflict, ValueError, TypeError, AttributeError):
                continue
        return tuple(sorted(bindings, key=lambda binding: binding.plugin_id))

    def all_active_references(self) -> tuple[PluginMCPReferenceBinding, ...]:
        return self.active_references(tuple(record.object_id for record in self._records.list(_ACTIVATIONS)))

    def active_contributions(self, plugin_ids: Sequence[str]) -> tuple[PluginMCPReferenceBinding, ...]:
        return self.active_references(plugin_ids)

    def reference_statuses(
        self, plugin_ids: Sequence[str],
    ) -> tuple[PluginMCPReferenceStatus, ...]:
        """Project a closed local status without changing activation state."""
        allowed = tuple(sorted({_text(item, "plugin_id", 64) for item in plugin_ids}))
        try:
            snapshot = self._authority_loader()
        except Exception:
            snapshot = None
        statuses: list[PluginMCPReferenceStatus] = []
        for plugin_id in allowed:
            activation = self._records.read(_ACTIVATIONS, plugin_id)
            if activation is None or activation.payload.get("status") != "active":
                statuses.append(PluginMCPReferenceStatus(plugin_id, "unavailable"))
                continue
            try:
                state = _installed_state(self._records.read(_STATES, plugin_id), plugin_id)
                raw = _required(self._records.read(_RAW, _package_id(state)), "raw package")
                review = _required(self._records.read(_REVIEWS, raw.object_id), "MCP reference review")
                candidate = _review_candidate(review, plugin_id, raw.object_id)
                if (
                    activation.payload.get("review_revision") != review.revision
                    or activation.payload.get("candidate") != candidate
                    or _candidate(raw, plugin_id) != candidate
                    or _reference_bytes(raw) != review.payload.get("reference_content_base64")
                ):
                    statuses.append(PluginMCPReferenceStatus(plugin_id, "invalid"))
                    continue
            except (PluginPackageIntakeError, PluginPackageIntakeConflict, ValueError, TypeError, AttributeError):
                statuses.append(PluginMCPReferenceStatus(plugin_id, "invalid"))
                continue
            if snapshot is None:
                statuses.append(PluginMCPReferenceStatus(plugin_id, "unavailable"))
                continue
            statuses.append(PluginMCPReferenceStatus(
                plugin_id, _reference_authority_status(snapshot, candidate),
            ))
        return tuple(statuses)


def _candidate(raw: SQLiteStructuredRecord, plugin_id: str) -> dict[str, object]:
    if raw.payload.get("plugin_id") != plugin_id:
        raise PluginPackageIntakeConflict("Plugin MCP reference raw identity drifted")
    files = raw.payload.get("files")
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        raise PluginPackageIntakeError("Plugin MCP reference raw files are invalid")
    refs = [item for item in files if isinstance(item, Mapping) and isinstance(item.get("relative_path"), str) and str(item["relative_path"]).startswith("mcp/")]
    if len(refs) != 1:
        raise PluginPackageIntakeError("Plugin MCP package must contain exactly one server reference")
    item = refs[0]
    path = str(item["relative_path"])
    parts = path.split("/")
    if len(parts) != 3 or parts[0] != "mcp" or not parts[1] or parts[2] != "server-ref.json":
        raise PluginPackageIntakeError("Plugin MCP server reference path is invalid")
    try:
        encoded = _text(item.get("content_base64"), "content_base64", 1_000_000)
        content = base64.b64decode(encoded, validate=True)
        if len(content) != item.get("size_bytes"):
            raise ValueError
        value = json.loads(content.decode("utf-8"))
    except Exception as error:
        raise PluginPackageIntakeError("Plugin MCP server reference is invalid") from error
    if not isinstance(value, Mapping) or set(value) != _CANDIDATE_FIELDS:
        raise PluginPackageIntakeError("Plugin MCP server reference identity is invalid")
    candidate = dict(value)
    if candidate["server_id"] != parts[1]:
        raise PluginPackageIntakeError("Plugin MCP server reference identity is invalid")
    _candidate_types(candidate)
    return candidate


def _reference_bytes(raw: SQLiteStructuredRecord) -> str:
    files = raw.payload.get("files")
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        raise PluginPackageIntakeError("Plugin MCP reference raw files are invalid")
    matches = [item for item in files if isinstance(item, Mapping) and isinstance(item.get("relative_path"), str) and str(item["relative_path"]).startswith("mcp/")]
    if len(matches) != 1:
        raise PluginPackageIntakeError("Plugin MCP server reference is invalid")
    return _text(matches[0].get("content_base64"), "content_base64", 1_000_000)


def _approved_identity(snapshot: MCPApprovedServerSnapshotPort, candidate: Mapping[str, object]) -> None:
    status = _reference_authority_status(snapshot, candidate)
    if status == "unavailable":
        raise PluginPackageIntakeError("Plugin MCP server is not enabled and approved")
    if status != "active":
        raise PluginPackageIntakeConflict("Plugin MCP server identity drifted")


def _reference_authority_status(
    snapshot: MCPApprovedServerSnapshotPort, candidate: Mapping[str, object],
) -> str:
    records = getattr(snapshot, "servers", ())
    matches = [record for record in records if getattr(record, "server_id", None) == candidate["server_id"]]
    if len(matches) != 1 or matches[0].enabled is not True:
        return "unavailable"
    record = matches[0]
    actual = {"server_id": record.server_id, "approval_revision": record.approval_revision, "manifest_revision": record.host_connection.manifest_revision, "endpoint_identity": record.host_connection.endpoint_identity, "credential_subject_id": record.host_connection.credential_subject_id, "transport_generation": record.host_connection.transport_generation}
    if candidate.get("schema_version") != "1.0.0" or actual != {name: candidate[name] for name in actual}:
        return "needs_review"
    return "active"


def _review_candidate(record: SQLiteStructuredRecord, plugin_id: str, package_id: str) -> dict[str, object]:
    payload = record.payload
    if payload.get("plugin_id") != plugin_id or payload.get("package_record_id") != package_id or payload.get("decision") != "approved_disabled":
        raise PluginPackageIntakeConflict("Plugin MCP reference review identity drifted")
    candidate = payload.get("candidate")
    if not isinstance(candidate, Mapping) or set(candidate) != _CANDIDATE_FIELDS:
        raise PluginPackageIntakeConflict("Plugin MCP reference review candidate is invalid")
    value = dict(candidate)
    _candidate_types(value)
    return value


def _installed_state(record: SQLiteStructuredRecord | None, plugin_id: str) -> SQLiteStructuredRecord:
    record = _required(record, "package state")
    if record.payload.get("plugin_id") != plugin_id or record.payload.get("status") != "installed_disabled" or record.payload.get("enabled") is not False:
        raise PluginPackageIntakeError("Plugin package must remain installed disabled")
    _package_id(record)
    return record


def _package_id(record: SQLiteStructuredRecord) -> str:
    return _text(record.payload.get("package_record_id"), "package_record_id", 160)


def _candidate_types(value: Mapping[str, object]) -> None:
    if value.get("schema_version") != "1.0.0":
        raise PluginPackageIntakeError("schema_version is invalid")
    for name in ("server_id", "endpoint_identity", "credential_subject_id"):
        _text(value.get(name), name, 256)
    for name in ("approval_revision", "manifest_revision", "transport_generation"):
        _revision(value.get(name), name, allow_zero=False)


def _required(record: SQLiteStructuredRecord | None, label: str) -> SQLiteStructuredRecord:
    if record is None:
        raise PluginPackageIntakeError(f"Plugin {label} is missing")
    return record


def _result(record: SQLiteStructuredRecord, label: str, *, replayed: bool) -> dict[str, object]:
    return {label: dict(record.payload), f"{label}_revision": record.revision, "replayed": replayed}


def _replay(record: SQLiteStructuredRecord, operation: str, plugin_id: str) -> dict[str, object]:
    payload = record.payload
    if payload.get("operation") != operation or payload.get("plugin_id") != plugin_id or not isinstance(payload.get("result"), Mapping):
        raise PluginPackageIntakeConflict("Plugin MCP reference command identity drifted")
    return dict(payload["result"]) | {"replayed": True}


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise PluginPackageIntakeError(f"{label} is invalid")
    return value.strip()


def _command_id(value: object) -> str:
    if not isinstance(value, str) or not _COMMAND_ID.fullmatch(value):
        raise PluginPackageIntakeError("command_id is invalid")
    return value


def _revision(value: object, label: str, *, allow_zero: bool = True) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < (0 if allow_zero else 1):
        raise PluginPackageIntakeError(f"{label} is invalid")
    return value
