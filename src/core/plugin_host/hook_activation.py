"""Explicit review and activation authority for declarative Plugin Hooks.

This authority owns only a Hook-to-Hand binding.  Code, launch details and
containment remain owned by the existing reviewed Plugin Hand authority.  A
binding is executable only while the exact Hand activation and immutable raw
package bytes reviewed here remain current.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from core.storage_provider import SQLiteStructuredRecord, SQLiteStructuredRecordStore

from .hands_activation import PluginHandsActivationAuthority
from .package_intake import PluginPackageIntakeError, captured_hook_candidates


_STATES = "plugin_package_states"
_RAW = "plugin_raw_packages"
_HAND_ACTIVATIONS = "plugin_hands_activations"
_REVIEWS = "plugin_hook_reviews"
_ACTIVATIONS = "plugin_hook_activations"
_COMMANDS = "plugin_hook_activation_commands"


class PluginHookActivationError(ValueError):
    pass


class PluginHookActivationConflict(PluginHookActivationError):
    pass


@dataclass(frozen=True, slots=True)
class PluginHookActivation:
    plugin_id: str
    hook_id: str
    hand_id: str
    package_record_id: str
    event: str
    order: int
    synchronous: bool
    timeout_ms: int
    review_revision: int
    hand_activation_revision: int
    activation_revision: int

    @property
    def handler_revision(self) -> str:
        return f"hook-r{self.review_revision}-hand-r{self.hand_activation_revision}-active-r{self.activation_revision}"


class PluginHookActivationAuthority:
    def __init__(
        self,
        records: SQLiteStructuredRecordStore,
        *,
        hands: PluginHandsActivationAuthority,
        now: str,
    ) -> None:
        self._records = records
        self._hands = hands
        self._now = _text(now, "now", 96)

    def review(
        self,
        plugin_id: str,
        *,
        hook_id: str,
        expected_state_revision: int,
        expected_hand_activation_revision: int,
        command_id: str,
        confirm: bool,
        reason: str,
    ) -> dict[str, object]:
        plugin, hook, command = _identity(plugin_id, "plugin_id"), _identity(hook_id, "hook_id"), _identity(command_id, "command_id", maximum=128)
        state_revision = _revision(expected_state_revision, "expected_state_revision")
        hand_revision = _revision(expected_hand_activation_revision, "expected_hand_activation_revision")
        reason = _text(reason, "reason", 500)
        if confirm is not True:
            raise PluginHookActivationError("Plugin Hook review requires explicit confirmation")
        with self._records.begin() as uow:
            replay = uow.read(_COMMANDS, command)
            semantic = {
                "operation": "review", "plugin_id": plugin, "hook_id": hook,
                "expected_state_revision": state_revision,
                "expected_hand_activation_revision": hand_revision, "reason": reason,
            }
            if replay is not None:
                return _replay(replay, semantic)
            state = _required(uow.read(_STATES, plugin), "Plugin package state")
            if state.revision != state_revision or state.payload.get("status") != "installed_disabled" or state.payload.get("enabled") is not False:
                raise PluginHookActivationConflict("Plugin package state drifted")
            package = _identity(state.payload.get("package_record_id"), "package_record_id", maximum=160)
            raw = _required(uow.read(_RAW, package), "Plugin raw package")
            candidate = _candidate(raw, plugin, hook)
            hand_id = str(candidate["hand_id"])
            hand = _required(uow.read(_HAND_ACTIVATIONS, f"{plugin}--{hand_id}"), "Plugin Hand activation")
            _require_hand(hand, plugin, hand_id, package, hand_revision)
            payload = {
                "schema_version": "1.0.0", "plugin_id": plugin, "hook_id": hook,
                "hand_id": hand_id, "package_record_id": package,
                "candidate": dict(candidate), "state_revision": state.revision,
                "hand_activation_revision": hand.revision, "decision": "approved_disabled",
                "reviewed_by": "local-user", "reviewed_at": self._now, "reason": reason,
            }
            current = uow.read(_REVIEWS, f"{plugin}--{hook}")
            if current is None:
                saved = uow.put(_REVIEWS, f"{plugin}--{hook}", payload, expected_revision=0)
            elif dict(current.payload) == payload:
                saved = current
            else:
                raise PluginHookActivationConflict("Plugin Hook review identity drifted")
            result = {"review": dict(saved.payload), "review_revision": saved.revision, "replayed": False}
            uow.put(_COMMANDS, command, semantic | {"result": result}, expected_revision=0)
            uow.commit()
            return result

    def activate(
        self,
        plugin_id: str,
        *,
        hook_id: str,
        expected_review_revision: int,
        expected_activation_revision: int,
        command_id: str,
        confirm: bool,
    ) -> dict[str, object]:
        plugin, hook, command = _identity(plugin_id, "plugin_id"), _identity(hook_id, "hook_id"), _identity(command_id, "command_id", maximum=128)
        review_revision = _revision(expected_review_revision, "expected_review_revision")
        activation_revision = _revision(expected_activation_revision, "expected_activation_revision", allow_zero=True)
        if confirm is not True:
            raise PluginHookActivationError("Plugin Hook activation requires explicit confirmation")
        with self._records.begin() as uow:
            semantic = {
                "operation": "activate", "plugin_id": plugin, "hook_id": hook,
                "expected_review_revision": review_revision,
                "expected_activation_revision": activation_revision,
            }
            replay = uow.read(_COMMANDS, command)
            if replay is not None:
                return _replay(replay, semantic)
            review = _required(uow.read(_REVIEWS, f"{plugin}--{hook}"), "Plugin Hook review")
            reviewed = _review_payload(review, plugin, hook)
            if review.revision != review_revision:
                raise PluginHookActivationConflict("Plugin Hook review revision drifted")
            hand = _required(uow.read(_HAND_ACTIVATIONS, f"{plugin}--{reviewed['hand_id']}"), "Plugin Hand activation")
            _require_hand(hand, plugin, str(reviewed["hand_id"]), str(reviewed["package_record_id"]), int(reviewed["hand_activation_revision"]))
            current = uow.read(_ACTIVATIONS, f"{plugin}--{hook}")
            if (current.revision if current is not None else 0) != activation_revision:
                raise PluginHookActivationConflict("Plugin Hook activation revision drifted")
            payload = {
                "schema_version": "1.0.0", "plugin_id": plugin, "hook_id": hook,
                "hand_id": reviewed["hand_id"], "package_record_id": reviewed["package_record_id"],
                "candidate": reviewed["candidate"], "review_revision": review.revision,
                "hand_activation_revision": hand.revision, "status": "active", "activated_at": self._now,
            }
            saved = uow.put(_ACTIVATIONS, f"{plugin}--{hook}", payload, expected_revision=activation_revision)
            result = {"activation": dict(saved.payload), "activation_revision": saved.revision, "replayed": False}
            uow.put(_COMMANDS, command, semantic | {"result": result}, expected_revision=0)
            uow.commit()
            return result

    def disable(
        self,
        plugin_id: str,
        *,
        hook_id: str,
        expected_activation_revision: int,
        command_id: str,
        reason: str,
    ) -> dict[str, object]:
        plugin, hook, command = _identity(plugin_id, "plugin_id"), _identity(hook_id, "hook_id"), _identity(command_id, "command_id", maximum=128)
        expected = _revision(expected_activation_revision, "expected_activation_revision")
        reason = _text(reason, "reason", 500)
        with self._records.begin() as uow:
            semantic = {"operation": "disable", "plugin_id": plugin, "hook_id": hook, "expected_activation_revision": expected, "reason": reason}
            replay = uow.read(_COMMANDS, command)
            if replay is not None:
                return _replay(replay, semantic)
            current = _required(uow.read(_ACTIVATIONS, f"{plugin}--{hook}"), "Plugin Hook activation")
            active = _activation_payload(current, plugin, hook)
            if current.revision != expected or active["status"] != "active":
                raise PluginHookActivationConflict("Plugin Hook activation revision drifted")
            payload = dict(active) | {"status": "disabled", "disabled_at": self._now, "disable_reason": reason}
            saved = uow.put(_ACTIVATIONS, current.object_id, payload, expected_revision=current.revision)
            result = {"activation": dict(saved.payload), "activation_revision": saved.revision, "replayed": False}
            uow.put(_COMMANDS, command, semantic | {"result": result}, expected_revision=0)
            uow.commit()
            return result

    def resolve_active(self, plugin_id: str, *, hook_id: str) -> PluginHookActivation | None:
        plugin, hook = _identity(plugin_id, "plugin_id"), _identity(hook_id, "hook_id")
        record = self._records.read(_ACTIVATIONS, f"{plugin}--{hook}")
        if record is None:
            return None
        payload = _activation_payload(record, plugin, hook)
        if payload["status"] != "active":
            return None
        review = _required(self._records.read(_REVIEWS, f"{plugin}--{hook}"), "Plugin Hook review")
        reviewed = _review_payload(review, plugin, hook)
        if review.revision != payload["review_revision"] or reviewed["candidate"] != payload["candidate"]:
            raise PluginHookActivationConflict("Plugin Hook review authority drifted")
        raw = _required(self._records.read(_RAW, str(payload["package_record_id"])), "Plugin raw package")
        if _candidate(raw, plugin, hook) != payload["candidate"]:
            raise PluginHookActivationConflict("Plugin Hook raw bytes drifted")
        hand = self._hands.resolve_active(plugin, hand_id=str(payload["hand_id"]))
        if hand is None or hand.activation_revision != payload["hand_activation_revision"] or hand.package_record_id != payload["package_record_id"]:
            raise PluginHookActivationConflict("Plugin Hook Hand activation drifted")
        candidate = payload["candidate"]
        return PluginHookActivation(
            plugin, hook, str(payload["hand_id"]), str(payload["package_record_id"]),
            str(candidate["event"]), int(candidate["order"]), bool(candidate["sync"]),
            int(candidate["timeout_ms"]), int(payload["review_revision"]),
            int(payload["hand_activation_revision"]), record.revision,
        )

    def all_active(self) -> tuple[PluginHookActivation, ...]:
        active: list[PluginHookActivation] = []
        for record in self._records.list(_ACTIVATIONS):
            try:
                value = self.resolve_active(str(record.payload.get("plugin_id")), hook_id=str(record.payload.get("hook_id")))
            except PluginHookActivationError:
                continue
            if value is not None:
                active.append(value)
        return tuple(sorted(active, key=lambda item: (item.event, item.order, item.plugin_id, item.hook_id)))


def _candidate(raw: SQLiteStructuredRecord, plugin: str, hook: str) -> dict[str, object]:
    if raw.payload.get("plugin_id") != plugin:
        raise PluginHookActivationConflict("Plugin raw package identity drifted")
    try:
        matches = [dict(item) for item in captured_hook_candidates(raw.payload.get("files")) if item.get("id") == hook]
    except PluginPackageIntakeError as exc:
        raise PluginHookActivationConflict("Plugin Hook raw bytes are invalid") from exc
    if len(matches) != 1:
        raise PluginHookActivationError("Plugin Hook candidate is unavailable")
    return matches[0]


def _require_hand(record: SQLiteStructuredRecord, plugin: str, hand: str, package: str, revision: int) -> None:
    payload = record.payload
    if record.revision != revision or payload.get("plugin_id") != plugin or payload.get("hand_id") != hand or payload.get("package_record_id") != package or payload.get("status") != "active":
        raise PluginHookActivationConflict("Plugin Hook Hand activation drifted")


def _review_payload(record: SQLiteStructuredRecord, plugin: str, hook: str) -> dict[str, object]:
    value = dict(record.payload)
    required = {"schema_version", "plugin_id", "hook_id", "hand_id", "package_record_id", "candidate", "state_revision", "hand_activation_revision", "decision", "reviewed_by", "reviewed_at", "reason"}
    if set(value) != required or value.get("schema_version") != "1.0.0" or value.get("plugin_id") != plugin or value.get("hook_id") != hook or value.get("decision") != "approved_disabled" or not isinstance(value.get("candidate"), Mapping):
        raise PluginHookActivationConflict("Plugin Hook review is invalid")
    value["state_revision"] = _revision(value["state_revision"], "state_revision")
    value["hand_activation_revision"] = _revision(value["hand_activation_revision"], "hand_activation_revision")
    return value


def _activation_payload(record: SQLiteStructuredRecord, plugin: str, hook: str) -> dict[str, object]:
    value = dict(record.payload)
    required = {"schema_version", "plugin_id", "hook_id", "hand_id", "package_record_id", "candidate", "review_revision", "hand_activation_revision", "status", "activated_at"}
    allowed = required | {"disabled_at", "disable_reason"}
    if not required <= set(value) or not set(value) <= allowed or value.get("schema_version") != "1.0.0" or value.get("plugin_id") != plugin or value.get("hook_id") != hook or value.get("status") not in {"active", "disabled"} or not isinstance(value.get("candidate"), Mapping):
        raise PluginHookActivationConflict("Plugin Hook activation is invalid")
    value["review_revision"] = _revision(value["review_revision"], "review_revision")
    value["hand_activation_revision"] = _revision(value["hand_activation_revision"], "hand_activation_revision")
    return value


def _replay(record: SQLiteStructuredRecord, semantic: Mapping[str, object]) -> dict[str, object]:
    value = dict(record.payload)
    result = value.pop("result", None)
    if value != dict(semantic) or not isinstance(result, Mapping):
        raise PluginHookActivationConflict("Plugin Hook command identity drifted")
    return dict(result) | {"replayed": True}


def _required(record: SQLiteStructuredRecord | None, label: str) -> SQLiteStructuredRecord:
    if record is None:
        raise PluginHookActivationError(f"{label} is unavailable")
    return record


def _identity(value: object, label: str, *, maximum: int = 64) -> str:
    text = _text(value, label, maximum)
    if not text[0].isalnum() or any(not (item.isalnum() or item in "._~-:") for item in text):
        raise PluginHookActivationError(f"{label} is invalid")
    return text


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum or "\x00" in value:
        raise PluginHookActivationError(f"{label} is invalid")
    return value.strip()


def _revision(value: object, label: str, *, allow_zero: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < (0 if allow_zero else 1):
        raise PluginHookActivationError(f"{label} is invalid")
    return value
