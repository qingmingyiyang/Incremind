from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from backend.api.effect_operations_health import build_effect_operations_health
from backend.api.effect_partition_inventory import (
    AI_TURNS_EFFECT_PARTITION,
    PRIMARY_EFFECT_PARTITION,
    register_enabled_effect_partitions,
)
from backend.api.storage_topology import STORAGE_TOPOLOGY, storage_topology_payload
from core.effect_log import EffectRecoveryCoordinator, build_effect_runtime


class _Recovery:
    def snapshot(self) -> dict[str, object]:
        return {
            "status": "ready", "last_started_at": 10,
            "last_finished_at": 11, "last_failure_type": None,
        }


def test_storage_topology_is_versioned_relative_and_governed() -> None:
    payload = storage_topology_payload()

    assert payload["schema_version"] == "storage-topology-v1"
    assert {entry.tier for entry in STORAGE_TOPOLOGY} == {
        "authority", "derived-ttl", "derived-nottl",
    }
    assert all(not Path(entry.relative_path).is_absolute() for entry in STORAGE_TOPOLOGY)
    assert all(not entry.deletable for entry in STORAGE_TOPOLOGY if entry.tier == "authority")
    assert next(entry for entry in STORAGE_TOPOLOGY if entry.tier == "derived-ttl").ttl_seconds
    assert {entry.name for entry in STORAGE_TOPOLOGY if entry.tier == "derived-nottl"} >= {
        "recall-indexes", "plugin-materializations", "ppt-jobs",
    }


def test_effect_operations_health_reads_existing_partition_without_creating_missing(tmp_path) -> None:
    primary = PRIMARY_EFFECT_PARTITION.database(tmp_path)
    build_effect_runtime(primary, owner_id="test")
    missing = tmp_path / ".rebuild-data" / "ai-turns.sqlite3"

    payload = build_effect_operations_health(tmp_path, _Recovery())

    assert [item["name"] for item in payload["partitions"]] == [
        "primary", "ai-turns", "ppt-master",
    ]
    assert payload["partitions"][0]["status"] == "ready"
    assert payload["partitions"][0]["effects"]["counts_available"] is True
    assert payload["partitions"][0]["effects"]["terminal"] == 0
    assert payload["partitions"][0]["storage"]["database_bytes"] > 0
    assert payload["partitions"][0]["lease_seconds"] == 30
    assert payload["partitions"][1]["status"] == "missing"
    assert not missing.exists()
    assert payload["recovery"]["status"] == "ready"
    assert str(tmp_path) not in repr(payload)


def test_effect_operations_health_uses_safe_error_category_for_non_database(tmp_path) -> None:
    target = PRIMARY_EFFECT_PARTITION.database(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text("not sqlite", encoding="utf-8")

    payload = build_effect_operations_health(tmp_path, None)

    assert payload["partitions"][0]["status"] == "unavailable"
    assert payload["partitions"][0]["error_type"] == "sqlite_error"
    assert payload["partitions"][0]["effects"]["counts_available"] is False
    assert "not sqlite" not in repr(payload)


def test_effect_operations_health_reports_inventory_drift_and_safe_partition_passes(tmp_path) -> None:
    primary = build_effect_runtime(
        PRIMARY_EFFECT_PARTITION.database(tmp_path),
        owner_id=PRIMARY_EFFECT_PARTITION.owner_id(1),
        lease_seconds=PRIMARY_EFFECT_PARTITION.lease_seconds,
    )
    ai = build_effect_runtime(
        AI_TURNS_EFFECT_PARTITION.database(tmp_path),
        owner_id=AI_TURNS_EFFECT_PARTITION.owner_id(1),
        lease_seconds=AI_TURNS_EFFECT_PARTITION.lease_seconds,
    )
    state = SimpleNamespace(
        effect_runtime=primary,
        ai_effect_runtime=ai,
        ppt_master_effect_runtime=None,
        ppt_master_capability=None,
    )
    coordinator = EffectRecoveryCoordinator(primary)
    register_enabled_effect_partitions(coordinator, state, root_dir=tmp_path)
    coordinator.recover_once(now=42)

    payload = build_effect_operations_health(
        tmp_path, _Recovery(), recovery_coordinator=coordinator,
        application_state=state, now=43,
    )

    assert payload["inventory"] == {
        "status": "ready",
        "expected_enabled_names": ("ai-turns", "primary"),
        "registered_names": ("ai-turns", "primary"),
        "missing_names": (),
        "unexpected_names": (),
    }
    assert payload["partitions"][0]["last_recovery_pass"] == {
        "status": "idle", "recovered_count": 0, "completed_at": 42,
    }
    assert "operation_id" not in repr(payload)


def test_effect_operations_health_degrades_top_level_on_unexpected_registration(tmp_path) -> None:
    primary = build_effect_runtime(
        PRIMARY_EFFECT_PARTITION.database(tmp_path),
        owner_id=PRIMARY_EFFECT_PARTITION.owner_id(1),
        lease_seconds=PRIMARY_EFFECT_PARTITION.lease_seconds,
    )
    unexpected = build_effect_runtime(
        tmp_path / ".rebuild-data" / "unexpected.sqlite3",
        owner_id="unexpected:1",
    )
    coordinator = EffectRecoveryCoordinator(primary)
    coordinator.register_partition("unexpected", unexpected)
    coordinator.configure_expected_partitions(("primary",))
    state = SimpleNamespace(
        effect_runtime=primary,
        ai_effect_runtime=None,
        ppt_master_effect_runtime=None,
        ppt_master_capability=None,
    )

    payload = build_effect_operations_health(
        tmp_path, _Recovery(), recovery_coordinator=coordinator,
        application_state=state, now=43,
    )

    assert payload["inventory"]["status"] == "drift"
    assert payload["inventory"]["unexpected_names"] == ("unexpected",)
    assert payload["status"] == "degraded"
