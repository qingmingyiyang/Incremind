from __future__ import annotations

from pathlib import Path

import pytest

from core.plugin_host.hands_upgrade import (
    PluginHandsUpgradeAuthority,
    PluginHandsUpgradeConflict,
    PluginHandsUpgradeError,
    PluginHandsUpgradePorts,
    PluginHandsUpgradeSnapshot,
    decode_plugin_hands_upgrade_record,
)
from core.storage_provider import SQLiteStructuredRecord, SQLiteStructuredRecordStore


def _snapshot(version: str, activation: int) -> PluginHandsUpgradeSnapshot:
    return PluginHandsUpgradeSnapshot(
        package_record_id=f"demo-plugin~{version}", review_revision=activation,
        materialization_revision=activation + 10, activation_revision=activation,
        runtime_revision=f"runtime-{version}", resource_policy_revision="plugin-hands-resource-v1",
    )


def _authority(root: Path, attempts=lambda old, new: (), validate=lambda uow, plugin, hand, old, new, stage, pointer: None) -> PluginHandsUpgradeAuthority:
    return PluginHandsUpgradeAuthority(SQLiteStructuredRecordStore(root / "records.sqlite3"), now="2026-08-26T00:00:00Z", validate_snapshots=validate, attempts=attempts)


def _ports(events: list[tuple[str, str, str]]) -> PluginHandsUpgradePorts:
    def event(kind):
        def call(snapshot, cutover_id, phase):
            events.append((kind, snapshot.package_record_id, f"{cutover_id}:{phase}"))
        return call
    return PluginHandsUpgradePorts(revoke=event("revoke"), switch=event("switch"), register=event("register"))


def test_preview_freezes_revision_only_contract_and_begin_is_cas_replay_safe(tmp_path: Path) -> None:
    authority, old, new = _authority(tmp_path), _snapshot("1.0.0", 1), _snapshot("2.0.0", 2)
    preview = authority.preview("demo-plugin", hand_id="summarize", old=old, new=new)
    assert preview.old == old and preview.new == new and preview.automatic_rollback_blocked_for_write_attempts is False
    started = authority.begin("cutover-0001", "demo-plugin", hand_id="summarize", old=old, new=new)
    assert started["stage"] == "prepared" and set(started["old"]) == {"package_record_id", "review_revision", "materialization_revision", "activation_revision", "runtime_revision", "resource_policy_revision"}
    assert authority.begin("cutover-0001", "demo-plugin", hand_id="summarize", old=old, new=new)["replayed"] is True
    with pytest.raises(PluginHandsUpgradeConflict, match="identity drifted"):
        authority.begin("cutover-0001", "demo-plugin", hand_id="summarize", old=old, new=_snapshot("3.0.0", 3))


def test_pure_record_decoder_requires_collection_identifier_and_current_schema(tmp_path: Path) -> None:
    authority, old, new = _authority(tmp_path), _snapshot("1.0.0", 1), _snapshot("2.0.0", 2)
    authority.begin("cutover-0001", "demo-plugin", hand_id="summarize", old=old, new=new)
    stored = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3").read("plugin_hands_upgrade_cutovers", "cutover-0001")
    assert stored is not None
    decoded = decode_plugin_hands_upgrade_record(stored)
    assert decoded["cutover_id"] == "cutover-0001" and decoded["stage"] == "prepared"
    with pytest.raises(PluginHandsUpgradeConflict):
        decode_plugin_hands_upgrade_record(SQLiteStructuredRecord("wrong", stored.object_id, stored.payload, stored.revision))
    with pytest.raises(PluginHandsUpgradeError):
        decode_plugin_hands_upgrade_record(SQLiteStructuredRecord(stored.collection, "short", stored.payload, stored.revision))


def test_only_one_cutover_per_hand_can_be_active_until_finalize(tmp_path: Path) -> None:
    authority, old, new = _authority(tmp_path), _snapshot("1.0.0", 1), _snapshot("2.0.0", 2)
    authority.begin("cutover-0001", "demo-plugin", hand_id="summarize", old=old, new=new)
    with pytest.raises(PluginHandsUpgradeConflict, match="active upgrade"):
        authority.begin("cutover-0002", "demo-plugin", hand_id="summarize", old=old, new=_snapshot("3.0.0", 3))
    authority.resume("cutover-0001", ports=_ports([]))
    authority.finalize("cutover-0001")
    assert authority.begin("cutover-0002", "demo-plugin", hand_id="summarize", old=new, new=_snapshot("3.0.0", 3))["stage"] == "prepared"


def test_resume_advances_four_durable_phases_and_does_not_double_register_after_restart(tmp_path: Path) -> None:
    old, new, events = _snapshot("1.0.0", 1), _snapshot("2.0.0", 2), []
    _authority(tmp_path).begin("cutover-0001", "demo-plugin", hand_id="summarize", old=old, new=new)
    result = _authority(tmp_path).resume("cutover-0001", ports=_ports(events))
    assert result["stage"] == "completed" and result["active_pointer"] == "new"
    assert events == [
        ("revoke", "demo-plugin~1.0.0", "cutover-0001:prepared"),
        ("switch", "demo-plugin~2.0.0", "cutover-0001:old_revoked"),
        ("register", "demo-plugin~2.0.0", "cutover-0001:new_switched"),
    ]
    assert _authority(tmp_path).resume("cutover-0001", ports=_ports(events))["stage"] == "completed"
    assert len(events) == 3


def test_crash_resume_repeats_only_the_idempotency_keyed_uncommitted_phase(tmp_path: Path) -> None:
    old, new, events = _snapshot("1.0.0", 1), _snapshot("2.0.0", 2), []
    authority = _authority(tmp_path)
    authority.begin("cutover-0001", "demo-plugin", hand_id="summarize", old=old, new=new)

    def crash_after_revoke(snapshot, cutover_id, phase):
        events.append(("revoke", snapshot.package_record_id, f"{cutover_id}:{phase}"))
        raise RuntimeError("simulated crash")
    with pytest.raises(RuntimeError):
        authority.resume("cutover-0001", ports=PluginHandsUpgradePorts(crash_after_revoke, _ports(events).switch, _ports(events).register))
    assert authority.load("cutover-0001")["stage"] == "prepared"
    assert _authority(tmp_path).resume("cutover-0001", ports=_ports(events))["stage"] == "completed"
    assert events[0] == events[1] == ("revoke", "demo-plugin~1.0.0", "cutover-0001:prepared")


def test_automatic_rollback_is_blocked_for_fenced_or_unknown_write_attempts(tmp_path: Path) -> None:
    attempts = lambda old, new: [{"effect": "write", "state": "unknown", "outcome_status": "unknown"}]
    old, new = _snapshot("1.0.0", 1), _snapshot("2.0.0", 2)
    authority = _authority(tmp_path, attempts)
    assert authority.preview("demo-plugin", hand_id="summarize", old=old, new=new).automatic_rollback_blocked_for_write_attempts is True
    authority.begin("cutover-0001", "demo-plugin", hand_id="summarize", old=old, new=new)
    events: list[tuple[str, str, str]] = []
    authority.resume("cutover-0001", ports=_ports(events))
    with pytest.raises(PluginHandsUpgradeConflict, match="blocked"):
        authority.rollback("cutover-0001", ports=_ports(events), automatic=True)
    assert authority.load("cutover-0001")["stage"] == "completed"


def test_missing_attempt_projection_blocks_automatic_rollback_fail_closed(tmp_path: Path) -> None:
    old, new = _snapshot("1.0.0", 1), _snapshot("2.0.0", 2)
    authority = _authority(tmp_path, attempts=None)
    authority.begin("cutover-0001", "demo-plugin", hand_id="summarize", old=old, new=new)
    authority.resume("cutover-0001", ports=_ports([]))
    with pytest.raises(PluginHandsUpgradeConflict, match="blocked"):
        authority.rollback("cutover-0001", ports=_ports([]))


def test_snapshot_authority_is_rechecked_before_begin_and_resume(tmp_path: Path) -> None:
    allowed = {"value": True}
    def validate(uow, plugin, hand, old, new, stage, pointer):
        assert (plugin, hand) == ("demo-plugin", "summarize")
        if not allowed["value"]:
            raise PluginHandsUpgradeConflict("snapshot drift")
    old, new = _snapshot("1.0.0", 1), _snapshot("2.0.0", 2)
    authority = _authority(tmp_path, validate=validate)
    authority.begin("cutover-0001", "demo-plugin", hand_id="summarize", old=old, new=new)
    allowed["value"] = False
    with pytest.raises(PluginHandsUpgradeConflict, match="snapshot drift"):
        authority.resume("cutover-0001", ports=_ports([]))


def test_safe_rollback_restores_only_pointer_and_never_migrates_historical_attempts(tmp_path: Path) -> None:
    old, new, events = _snapshot("1.0.0", 1), _snapshot("2.0.0", 2), []
    authority = _authority(tmp_path)
    authority.begin("cutover-0001", "demo-plugin", hand_id="summarize", old=old, new=new)
    authority.resume("cutover-0001", ports=_ports(events))
    result = authority.rollback("cutover-0001", ports=_ports(events))
    assert result["stage"] == "rolled_back" and result["active_pointer"] == "old"
    assert events[-3:] == [
        ("revoke", "demo-plugin~2.0.0", "cutover-0001:rollback_prepared"),
        ("switch", "demo-plugin~1.0.0", "cutover-0001:rollback_new_revoked"),
        ("register", "demo-plugin~1.0.0", "cutover-0001:rollback_old_switched"),
    ]


def test_rollback_authorization_is_durable_and_resume_replays_only_same_phase_key(tmp_path: Path) -> None:
    old, new, events = _snapshot("1.0.0", 1), _snapshot("2.0.0", 2), []
    authority = _authority(tmp_path)
    authority.begin("cutover-0001", "demo-plugin", hand_id="summarize", old=old, new=new)
    authority.resume("cutover-0001", ports=_ports(events))

    def crash_after_revoke(snapshot, cutover_id, phase):
        events.append(("revoke", snapshot.package_record_id, f"{cutover_id}:{phase}"))
        raise RuntimeError("simulated rollback crash")
    with pytest.raises(RuntimeError):
        authority.rollback("cutover-0001", ports=PluginHandsUpgradePorts(crash_after_revoke, _ports(events).switch, _ports(events).register), automatic=False, confirm=True, reason="operator-approved-rollback")
    retained = _authority(tmp_path).load("cutover-0001")
    assert retained["stage"] == "rollback_prepared"
    assert retained["rollback_mode"] == "manual" and retained["rollback_reason"] == "operator-approved-rollback"
    assert _authority(tmp_path).resume("cutover-0001", ports=_ports(events))["stage"] == "rolled_back"
    phase = ("revoke", "demo-plugin~2.0.0", "cutover-0001:rollback_prepared")
    assert events.count(phase) == 2
