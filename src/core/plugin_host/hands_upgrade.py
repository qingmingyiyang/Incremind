"""Durable, non-executing cutover authority for reviewed Plugin Hands.

This authority deliberately does not inspect package bytes, provision a Hand,
or change the legacy activation records.  It records the *decision* and drives
small, idempotency-keyed adapters supplied by later composition.  Consequently
an interrupted cutover can resume without making a second registration call.

Only opaque package identities and revision facts are persisted here.  Paths,
argv, environments, secrets, inputs, outputs and package payloads are rejected
by construction.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
import re
from uuid import NAMESPACE_URL, uuid5

from core.storage_provider import SQLiteStructuredRecord, SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError


_CUTOVERS = "plugin_hands_upgrade_cutovers"
_ACTIVE = "plugin_hands_upgrade_active"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")
_PLUGIN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_STAGES = frozenset({"prepared", "old_revoked", "new_switched", "new_registered", "completed", "rollback_prepared", "rollback_new_revoked", "rollback_old_switched", "rollback_old_registered", "rolled_back", "finalized"})


class PluginHandsUpgradeError(ValueError):
    """Raised when a package/runtime cutover is malformed or unsafe."""


class PluginHandsUpgradeConflict(PluginHandsUpgradeError):
    """Raised when the durable cutover identity or revision changed."""


@dataclass(frozen=True, slots=True)
class PluginHandsUpgradeSnapshot:
    """Frozen, safe references for one side of a cutover."""

    package_record_id: str
    review_revision: int
    materialization_revision: int
    activation_revision: int
    runtime_revision: str
    resource_policy_revision: str

    def __post_init__(self) -> None:
        _identifier(self.package_record_id, "package_record_id")
        for value in (self.review_revision, self.materialization_revision, self.activation_revision):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise PluginHandsUpgradeError("Plugin Hands upgrade revision is invalid")
        _identifier(self.runtime_revision, "runtime_revision")
        _identifier(self.resource_policy_revision, "resource_policy_revision")


@dataclass(frozen=True, slots=True)
class PluginHandsUpgradePreview:
    plugin_id: str
    hand_id: str
    old: PluginHandsUpgradeSnapshot
    new: PluginHandsUpgradeSnapshot
    automatic_rollback_blocked_for_write_attempts: bool


@dataclass(frozen=True, slots=True)
class PluginHandsUpgradePorts:
    """Idempotency-keyed integration boundary for a later composition layer.

    Each callback receives the cutover id and an unchanging phase key.  A port
    must treat that pair as its external idempotency key: a process crash after
    an external call but before this SQLite record advances will then replay
    the same operation, never create a second registration.
    """

    revoke: Callable[[PluginHandsUpgradeSnapshot, str, str], None]
    switch: Callable[[PluginHandsUpgradeSnapshot, str, str], None]
    register: Callable[[PluginHandsUpgradeSnapshot, str, str], None]


class PluginHandsUpgradeAuthority:
    """CAS state machine for an explicit Hand package/runtime cutover.

    ``attempts`` is an optional read-only port returning statuses/effects of
    attempts bound to the old or new snapshot.  It is used solely to prohibit
    automatic rollback after a fenced/unknown write; historical attempts are
    never moved or replayed.
    """

    def __init__(self, records: SQLiteStructuredRecordStore, *, now: str, validate_snapshots: Callable[..., None], attempts: Callable[[PluginHandsUpgradeSnapshot, PluginHandsUpgradeSnapshot], Iterable[Mapping[str, object]]] | None = None) -> None:
        if not isinstance(records, SQLiteStructuredRecordStore):
            raise PluginHandsUpgradeError("Plugin Hands upgrade store is invalid")
        if not callable(validate_snapshots):
            raise PluginHandsUpgradeError("Plugin Hands upgrade snapshot authority is invalid")
        self._records, self._now, self._validate_snapshots, self._attempts = records, _text(now, "now"), validate_snapshots, attempts

    def preview(self, plugin_id: str, *, hand_id: str, old: PluginHandsUpgradeSnapshot, new: PluginHandsUpgradeSnapshot) -> PluginHandsUpgradePreview:
        plugin, hand = _plugin(plugin_id), _plugin(hand_id)
        _different(old, new)
        self._validate_current(plugin, hand, old, new, "preview", "old")
        return PluginHandsUpgradePreview(plugin, hand, old, new, self._has_write_blocker(old, new))

    def begin(self, cutover_id: str, plugin_id: str, *, hand_id: str, old: PluginHandsUpgradeSnapshot, new: PluginHandsUpgradeSnapshot, expected_revision: int = 0) -> dict[str, object]:
        cutover, plugin, hand = _identifier(cutover_id, "cutover_id"), _plugin(plugin_id), _plugin(hand_id)
        _different(old, new)
        if expected_revision != 0:
            raise PluginHandsUpgradeConflict("Plugin Hands upgrade must begin at revision zero")
        payload = _payload(plugin, hand, old, new, "prepared", self._now)
        try:
            with self._records.begin() as uow:
                self._validate_snapshots(uow, plugin, hand, old, new, "begin", "old")
                current = uow.read(_CUTOVERS, cutover)
                if current is not None:
                    decoded = _decode(current)
                    if decoded["plugin_id"] != plugin or decoded["hand_id"] != hand or decoded["old"] != old or decoded["new"] != new:
                        raise PluginHandsUpgradeConflict("Plugin Hands upgrade identity drifted")
                    uow.rollback()
                    return _result(current, replayed=True)
                active_id = _active_id(plugin, hand)
                active = uow.read(_ACTIVE, active_id)
                if active is not None:
                    _validate_active(active, active_id)
                    if active.payload.get("status") == "active":
                        raise PluginHandsUpgradeConflict("Plugin Hand already has an active upgrade")
                saved = uow.put(_CUTOVERS, cutover, payload, expected_revision=0)
                uow.put(_ACTIVE, active_id, {"plugin_id": plugin, "hand_id": hand, "cutover_id": cutover, "status": "active"}, expected_revision=active.revision if active is not None else 0)
                uow.commit()
                return _result(saved, replayed=False)
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as exc:
            raise PluginHandsUpgradeConflict("Plugin Hands upgrade CAS failed") from exc

    def load(self, cutover_id: str) -> dict[str, object] | None:
        record = self._records.read(_CUTOVERS, _identifier(cutover_id, "cutover_id"))
        return decode_plugin_hands_upgrade_record(record) if record is not None else None

    def resume(self, cutover_id: str, *, ports: PluginHandsUpgradePorts) -> dict[str, object]:
        """Advance the four durable cutover phases; safe to call after crashes."""
        key = _identifier(cutover_id, "cutover_id")
        initial = self._current(key)
        self._validate_current(initial["plugin_id"], initial["hand_id"], initial["old"], initial["new"], initial["stage"], initial["active_pointer"])
        if str(initial["stage"]).startswith("rollback_"):
            return self._resume_rollback(key, ports)
        for stage, callback, snapshot, next_stage in (
            ("prepared", ports.revoke, "old", "old_revoked"),
            ("old_revoked", ports.switch, "new", "new_switched"),
            ("new_switched", ports.register, "new", "new_registered"),
        ):
            current = self._current(key)
            if current["stage"] != stage:
                continue
            value = current[snapshot]
            callback(value, key, stage)
            self._advance(key, current["revision"], next_stage)
        current = self._current(key)
        if current["stage"] == "new_registered":
            self._advance(key, current["revision"], "completed")
        return _result_from_decoded(self._current(key), replayed=False)

    def rollback(self, cutover_id: str, *, ports: PluginHandsUpgradePorts, automatic: bool = True, confirm: bool = False, reason: str | None = None) -> dict[str, object]:
        """Restore the old pointer only when durable effects make it safe.

        Manual rollback remains explicit; automatic rollback is blocked for a
        fenced/unknown write because the external effect is then indeterminate.
        """
        key = _identifier(cutover_id, "cutover_id")
        current = self._current(key)
        if current["stage"] == "rolled_back":
            return _result_from_decoded(current, replayed=False)
        if automatic and self._has_write_blocker(current["old"], current["new"]):
            raise PluginHandsUpgradeConflict("Plugin Hands upgrade automatic rollback is blocked by a fenced or unknown write attempt")
        if not automatic and (confirm is not True or not isinstance(reason, str)):
            raise PluginHandsUpgradeConflict("Plugin Hands upgrade manual rollback requires confirmation and reason")
        self._validate_current(current["plugin_id"], current["hand_id"], current["old"], current["new"], current["stage"], current["active_pointer"])
        if current["stage"] not in {"completed", "rollback_prepared", "rollback_new_revoked", "rollback_old_switched", "rollback_old_registered"}:
            raise PluginHandsUpgradeConflict("Plugin Hands upgrade rollback is not available before cutover completion")
        if current["stage"] == "completed":
            self._prepare_rollback(key, current["revision"], automatic=automatic, reason="automatic-safe" if automatic else _identifier(reason, "rollback_reason"))
        return self._resume_rollback(key, ports)

    def _resume_rollback(self, key: str, ports: PluginHandsUpgradePorts) -> dict[str, object]:
        for stage, callback, snapshot, next_stage in (
            ("rollback_prepared", ports.revoke, "new", "rollback_new_revoked"),
            ("rollback_new_revoked", ports.switch, "old", "rollback_old_switched"),
            ("rollback_old_switched", ports.register, "old", "rollback_old_registered"),
        ):
            current = self._current(key)
            if current["stage"] == stage:
                callback(current[snapshot], key, stage)
                self._advance(key, current["revision"], next_stage)
        current = self._current(key)
        if current["stage"] == "rollback_old_registered":
            self._advance(key, current["revision"], "rolled_back")
        return _result_from_decoded(self._current(key), replayed=False)

    def finalize(self, cutover_id: str) -> dict[str, object]:
        """Close the rollback window so a later upgrade may acquire the Hand."""
        key = _identifier(cutover_id, "cutover_id")
        current = self._current(key)
        if current["stage"] != "completed":
            raise PluginHandsUpgradeConflict("Plugin Hands upgrade can only finalize after completion")
        return self._advance(key, current["revision"], "finalized")

    def _current(self, cutover_id: str) -> dict[str, object]:
        record = self._records.read(_CUTOVERS, cutover_id)
        if record is None:
            raise PluginHandsUpgradeError("Plugin Hands upgrade is missing")
        return _decode(record)

    def _validate_current(self, plugin: str, hand: str, old: PluginHandsUpgradeSnapshot, new: PluginHandsUpgradeSnapshot, stage: str, pointer: str) -> None:
        try:
            with self._records.begin() as uow:
                self._validate_snapshots(uow, plugin, hand, old, new, stage, pointer)
                uow.rollback()
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as exc:
            raise PluginHandsUpgradeConflict("Plugin Hands upgrade snapshot validation failed") from exc

    def _prepare_rollback(self, cutover_id: str, revision: int, *, automatic: bool, reason: str) -> dict[str, object]:
        try:
            with self._records.begin() as uow:
                record = uow.read(_CUTOVERS, cutover_id)
                if record is None or record.revision != revision or record.payload.get("stage") != "completed":
                    raise PluginHandsUpgradeConflict("Plugin Hands rollback revision conflict")
                payload = dict(record.payload) | {
                    "stage": "rollback_prepared", "active_pointer": "new", "updated_at": self._now,
                    "rollback_mode": "automatic" if automatic else "manual",
                    "rollback_reason": _identifier(reason, "rollback_reason"), "rollback_authorized_at": self._now,
                }
                saved = uow.put(_CUTOVERS, cutover_id, payload, expected_revision=record.revision)
                uow.commit()
                return _result(saved, replayed=False)
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as exc:
            raise PluginHandsUpgradeConflict("Plugin Hands rollback CAS failed") from exc

    def _advance(self, cutover_id: str, revision: int, stage: str) -> dict[str, object]:
        try:
            with self._records.begin() as uow:
                record = uow.read(_CUTOVERS, cutover_id)
                if record is None or record.revision != revision:
                    raise PluginHandsUpgradeConflict("Plugin Hands upgrade revision conflict")
                payload = dict(record.payload) | {"stage": stage, "active_pointer": _pointer_for_stage(stage), "updated_at": self._now}
                saved = uow.put(_CUTOVERS, cutover_id, payload, expected_revision=record.revision)
                if stage in {"rolled_back", "finalized"}:
                    active_id = _active_id(payload["plugin_id"], payload["hand_id"])
                    active = uow.read(_ACTIVE, active_id)
                    if active is None or active.payload.get("cutover_id") != cutover_id or active.payload.get("status") != "active":
                        raise PluginHandsUpgradeConflict("Plugin Hands upgrade active owner drifted")
                    uow.put(_ACTIVE, active_id, dict(active.payload) | {"status": "closed"}, expected_revision=active.revision)
                uow.commit()
                return _result(saved, replayed=False)
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as exc:
            raise PluginHandsUpgradeConflict("Plugin Hands upgrade CAS failed") from exc

    def _has_write_blocker(self, old: PluginHandsUpgradeSnapshot, new: PluginHandsUpgradeSnapshot) -> bool:
        if self._attempts is None:
            return True
        for attempt in self._attempts(old, new):
            if not isinstance(attempt, Mapping):
                raise PluginHandsUpgradeError("Plugin Hands upgrade attempt projection is invalid")
            effect, state, outcome = attempt.get("effect"), attempt.get("state"), attempt.get("outcome_status")
            if effect == "write" and (state in {"fenced", "unknown"} or outcome == "unknown"):
                return True
        return False


def _payload(plugin: str, hand: str, old: PluginHandsUpgradeSnapshot, new: PluginHandsUpgradeSnapshot, stage: str, now: str) -> dict[str, object]:
    return {"schema_version": "1.0.0", "plugin_id": plugin, "hand_id": hand, "old": asdict(old), "new": asdict(new), "stage": stage, "active_pointer": "old", "created_at": now, "updated_at": now, "rollback_mode": None, "rollback_reason": None, "rollback_authorized_at": None}


def _decode(record: SQLiteStructuredRecord) -> dict[str, object]:
    payload = dict(record.payload)
    required = {"schema_version", "plugin_id", "hand_id", "old", "new", "stage", "active_pointer", "created_at", "updated_at", "rollback_mode", "rollback_reason", "rollback_authorized_at"}
    if set(payload) != required or payload.get("schema_version") != "1.0.0" or payload.get("stage") not in _STAGES or payload.get("active_pointer") != _pointer_for_stage(payload.get("stage")):
        raise PluginHandsUpgradeConflict("Plugin Hands upgrade record is invalid")
    plugin, hand = _plugin(payload.get("plugin_id")), _plugin(payload.get("hand_id"))
    try:
        old, new = PluginHandsUpgradeSnapshot(**dict(_mapping(payload["old"]))), PluginHandsUpgradeSnapshot(**dict(_mapping(payload["new"])))
    except (TypeError, ValueError) as exc:
        raise PluginHandsUpgradeConflict("Plugin Hands upgrade record is invalid") from exc
    _different(old, new)
    _text(payload.get("created_at"), "created_at"); _text(payload.get("updated_at"), "updated_at")
    rollback_mode, rollback_reason, rollback_at = payload.get("rollback_mode"), payload.get("rollback_reason"), payload.get("rollback_authorized_at")
    if payload["stage"] in {"rollback_prepared", "rollback_new_revoked", "rollback_old_switched", "rollback_old_registered", "rolled_back"}:
        if rollback_mode not in {"automatic", "manual"} or not isinstance(rollback_reason, str) or not rollback_reason or not isinstance(rollback_at, str): raise PluginHandsUpgradeConflict("Plugin Hands rollback authorization is invalid")
    elif any(value is not None for value in (rollback_mode, rollback_reason, rollback_at)):
        raise PluginHandsUpgradeConflict("Plugin Hands rollback authorization is invalid")
    return {"cutover_id": record.object_id, "plugin_id": plugin, "hand_id": hand, "old": old, "new": new, "stage": payload["stage"], "active_pointer": payload["active_pointer"], "revision": record.revision, "created_at": payload["created_at"], "updated_at": payload["updated_at"], "rollback_mode": rollback_mode, "rollback_reason": rollback_reason, "rollback_authorized_at": rollback_at}


def _result(record: SQLiteStructuredRecord, *, replayed: bool) -> dict[str, object]:
    return _result_from_decoded(_decode(record), replayed=replayed)


def decode_plugin_hands_upgrade_record(record: SQLiteStructuredRecord) -> dict[str, object]:
    """Strictly decode one durable cutover without opening or mutating a store.

    Startup probes may construct ``SQLiteStructuredRecord`` values from an
    already-opened read-only SQLite cursor.  Keeping this operation pure lets
    those probes reject malformed or future records without provisioning the
    writable storage adapter or an AI runtime.
    """

    if not isinstance(record, SQLiteStructuredRecord) or record.collection != _CUTOVERS:
        raise PluginHandsUpgradeConflict("Plugin Hands upgrade record is invalid")
    _identifier(record.object_id, "cutover_id")
    if not isinstance(record.revision, int) or isinstance(record.revision, bool) or record.revision < 1:
        raise PluginHandsUpgradeConflict("Plugin Hands upgrade record is invalid")
    return _result(record, replayed=False)


def _result_from_decoded(value: Mapping[str, object], *, replayed: bool) -> dict[str, object]:
    result = dict(value)
    result["old"] = asdict(result["old"])
    result["new"] = asdict(result["new"])
    result["replayed"] = replayed
    return result


def _pointer_for_stage(stage: object) -> str:
    if stage in {"new_switched", "new_registered", "completed", "finalized", "rollback_prepared", "rollback_new_revoked"}:
        return "new"
    return "old"


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError
    return value


def _plugin(value: object) -> str:
    if not isinstance(value, str) or _PLUGIN.fullmatch(value) is None:
        raise PluginHandsUpgradeError("Plugin Hands upgrade identity is invalid")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise PluginHandsUpgradeError(f"Plugin Hands upgrade {label} is invalid")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
        raise PluginHandsUpgradeError(f"Plugin Hands upgrade {label} is invalid")
    return value


def _different(old: PluginHandsUpgradeSnapshot, new: PluginHandsUpgradeSnapshot) -> None:
    if not isinstance(old, PluginHandsUpgradeSnapshot) or not isinstance(new, PluginHandsUpgradeSnapshot) or old == new:
        raise PluginHandsUpgradeError("Plugin Hands upgrade snapshots must be distinct")


def _active_id(plugin: str, hand: str) -> str:
    return "hands-upgrade-" + uuid5(NAMESPACE_URL, f"{plugin}:{hand}").hex


def _validate_active(record: SQLiteStructuredRecord, active_id: str) -> None:
    if record.object_id != active_id or set(record.payload) != {"plugin_id", "hand_id", "cutover_id", "status"} or record.payload.get("status") not in {"active", "closed"}:
        raise PluginHandsUpgradeConflict("Plugin Hands upgrade active owner is invalid")
