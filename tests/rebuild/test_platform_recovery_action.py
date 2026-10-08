from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.composition import build_platform_recovery_actions
from core.product_core import (
    CreatePlatformRecoveryActions,
    PlatformHealth,
    PlatformRecoveryActionError,
    ReviewPlatformRecoveryAction,
    ResolvePlatformRecoveryAction,
)
from core.product_core.health import ProductHealth
from core.storage_provider import JsonObjectStore, ObjectStorePlatformRecoveryActionRepository
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _health(platform: PlatformHealth) -> ProductHealth:
    return ProductHealth(
        status="degraded" if platform.status == "degraded" else "ready",
        contract_count=21,
        missing_contracts=(),
        storage_isolated=True,
        legacy_access="disabled",
        namespace_id="default",
        storage_version=1,
        root_uri="crp://default/",
        reference_root_uri="crp-ref://default/",
        backup_ready=True,
        index_status="ready",
        index_manifest_present=True,
        index_backend_kind="object_store_lexical",
        index_entry_count=2,
        index_traceable=True,
        index_vector_enabled=False,
        platform_status=platform.status,
        platform_capability_count=5,
        platform_ready_capabilities=platform.ready_capabilities,
        platform_degraded_capabilities=platform.degraded_capabilities,
        platform_missing_capabilities=platform.missing_capabilities,
        platform_os_path_leaks=platform.os_path_leaks,
    )


@dataclass
class InMemoryPlatformRecoveryActions:
    items: list[dict[str, object]]

    def save(self, action):
        payload = dict(action)
        self.items.append(payload)
        return payload

    def get(self, action_id):
        for item in self.items:
            if item.get("id") == action_id:
                return dict(item)
        return None

    def update(self, action):
        payload = dict(action)
        for index, item in enumerate(self.items):
            if item.get("id") == payload.get("id"):
                self.items[index] = payload
                return payload
        raise ValueError("platform recovery action not found")


def test_platform_recovery_action_is_created_from_worker_lifecycle_degradation() -> None:
    writer = InMemoryPlatformRecoveryActions([])
    health = _health(
        PlatformHealth(
            status="degraded",
            capability_count=5,
            ready_capabilities=("app_data_dir", "backup_destination", "file_picker", "system_info"),
            degraded_capabilities=("worker_lifecycle",),
            missing_capabilities=(),
            os_path_leaks=(),
        )
    )

    result = CreatePlatformRecoveryActions(actions=writer).execute(
        health=health,
        created_at="2026-07-01T02:00:00+08:00",
    )
    action = result.actions[0]

    assert len(result.actions) == 1
    assert action["capability"] == "worker_lifecycle"
    assert action["severity"] == "warning"
    assert action["status"] == "open"
    assert action["action_type"] == "restart_local_worker"
    assert action["source"] == {
        "health_status": "degraded",
        "health_ref": "crp://default/product-health/platform",
    }
    assert action["evidence"]["degraded_capabilities"] == ["worker_lifecycle"]
    assert action["execution"] == {
        "auto_execute": False,
        "requires_user_confirmation": True,
        "job_id": None,
    }
    assert action["user_decision"] is None
    assert action["resolution"] is None
    assert validate_contract_instance(
        "platform_recovery_action.schema.json",
        _schema("platform_recovery_action.schema.json"),
        action,
    ) == []


def test_platform_recovery_actions_persist_in_object_store_without_legacy_library(tmp_path: Path) -> None:
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    repository = ObjectStorePlatformRecoveryActionRepository(object_store)
    health = _health(
        PlatformHealth(
            status="degraded",
            capability_count=5,
            ready_capabilities=("app_data_dir", "backup_destination", "file_picker", "system_info"),
            degraded_capabilities=("worker_lifecycle",),
            missing_capabilities=(),
            os_path_leaks=(),
        )
    )

    result = CreatePlatformRecoveryActions(actions=repository).execute(
        health=health,
        created_at="2026-07-01T02:01:00+08:00",
    )
    action = result.actions[0]

    assert repository.get(str(action["id"])) == action
    assert repository.list_open() == (action,)
    assert not (tmp_path / "library").exists()


def test_platform_recovery_action_can_be_acknowledged_without_executing_repair(tmp_path: Path) -> None:
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    repository = ObjectStorePlatformRecoveryActionRepository(object_store)
    health = _health(
        PlatformHealth(
            status="degraded",
            capability_count=5,
            ready_capabilities=("app_data_dir", "backup_destination", "file_picker", "system_info"),
            degraded_capabilities=("worker_lifecycle",),
            missing_capabilities=(),
            os_path_leaks=(),
        )
    )
    action = CreatePlatformRecoveryActions(actions=repository).execute(
        health=health,
        created_at="2026-07-01T02:03:00+08:00",
    ).actions[0]

    result = ReviewPlatformRecoveryAction(actions=repository).acknowledge(
        action_id=str(action["id"]),
        decided_by="user",
        note="Keep local-only fallback visible.",
        decided_at="2026-07-01T02:04:00+08:00",
    )
    reviewed = result.action

    assert reviewed["status"] == "acknowledged"
    assert reviewed["user_decision"] == {
        "decision": "acknowledged",
        "decided_by": "user",
        "decided_at": "2026-07-01T02:04:00+08:00",
        "note": "Keep local-only fallback visible.",
        "execution_requested": False,
    }
    assert reviewed["execution"] == {
        "auto_execute": False,
        "requires_user_confirmation": True,
        "job_id": None,
    }
    assert reviewed["resolution"] is None
    assert repository.list_open() == ()
    assert object_store.list("jobs") == ()
    assert validate_contract_instance(
        "platform_recovery_action.schema.json",
        _schema("platform_recovery_action.schema.json"),
        reviewed,
    ) == []
    assert not (tmp_path / "library").exists()


def test_platform_recovery_action_can_be_dismissed_without_executing_repair(tmp_path: Path) -> None:
    repository = ObjectStorePlatformRecoveryActionRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    )
    health = _health(
        PlatformHealth(
            status="degraded",
            capability_count=5,
            ready_capabilities=("app_data_dir", "backup_destination", "file_picker", "system_info"),
            degraded_capabilities=("worker_lifecycle",),
            missing_capabilities=(),
            os_path_leaks=(),
        )
    )
    action = CreatePlatformRecoveryActions(actions=repository).execute(
        health=health,
        created_at="2026-07-01T02:05:00+08:00",
    ).actions[0]

    reviewed = ReviewPlatformRecoveryAction(actions=repository).dismiss(
        action_id=str(action["id"]),
        decided_by="user",
        note=None,
        decided_at="2026-07-01T02:06:00+08:00",
    ).action

    assert reviewed["status"] == "dismissed"
    assert reviewed["user_decision"]["decision"] == "dismissed"
    assert reviewed["user_decision"]["execution_requested"] is False
    assert reviewed["resolution"] is None
    assert reviewed["execution"]["job_id"] is None
    assert repository.list_open() == ()
    assert not (tmp_path / "library").exists()


def test_platform_recovery_action_decision_requires_open_action(tmp_path: Path) -> None:
    repository = ObjectStorePlatformRecoveryActionRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    )
    health = _health(
        PlatformHealth(
            status="degraded",
            capability_count=5,
            ready_capabilities=("app_data_dir", "backup_destination", "file_picker", "system_info"),
            degraded_capabilities=("worker_lifecycle",),
            missing_capabilities=(),
            os_path_leaks=(),
        )
    )
    action = CreatePlatformRecoveryActions(actions=repository).execute(
        health=health,
        created_at="2026-07-01T02:07:00+08:00",
    ).actions[0]
    review = ReviewPlatformRecoveryAction(actions=repository)
    review.acknowledge(
        action_id=str(action["id"]),
        decided_by="user",
        decided_at="2026-07-01T02:08:00+08:00",
    )

    with pytest.raises(PlatformRecoveryActionError, match="open status"):
        review.dismiss(
            action_id=str(action["id"]),
            decided_by="user",
            decided_at="2026-07-01T02:09:00+08:00",
        )


def test_platform_recovery_action_resolves_only_after_platform_health_recovers(tmp_path: Path) -> None:
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    repository = ObjectStorePlatformRecoveryActionRepository(object_store)
    degraded = _health(
        PlatformHealth(
            status="degraded",
            capability_count=5,
            ready_capabilities=("app_data_dir", "backup_destination", "file_picker", "system_info"),
            degraded_capabilities=("worker_lifecycle",),
            missing_capabilities=(),
            os_path_leaks=(),
        )
    )
    recovered = _health(
        PlatformHealth(
            status="ready",
            capability_count=5,
            ready_capabilities=(
                "app_data_dir",
                "backup_destination",
                "file_picker",
                "system_info",
                "worker_lifecycle",
            ),
            degraded_capabilities=(),
            missing_capabilities=(),
            os_path_leaks=(),
        )
    )
    action = CreatePlatformRecoveryActions(actions=repository).execute(
        health=degraded,
        created_at="2026-07-01T02:10:00+08:00",
    ).actions[0]

    resolved = ResolvePlatformRecoveryAction(actions=repository).execute(
        action_id=str(action["id"]),
        recovered_health=recovered,
        resolved_by="system",
        resolved_at="2026-07-01T02:11:00+08:00",
    ).action

    assert resolved["status"] == "resolved"
    assert resolved["user_decision"] is None
    assert resolved["resolution"] == {
        "resolved_by": "system",
        "resolved_at": "2026-07-01T02:11:00+08:00",
        "health_status": "ready",
        "health_ref": "crp://default/product-health/platform",
        "recovered_capability": "worker_lifecycle",
        "readiness_verified": True,
        "remaining_degraded_capabilities": [],
        "remaining_missing_capabilities": [],
        "remaining_os_path_leaks": [],
    }
    assert repository.list_open() == ()
    assert object_store.list("jobs") == ()
    assert validate_contract_instance(
        "platform_recovery_action.schema.json",
        _schema("platform_recovery_action.schema.json"),
        resolved,
    ) == []
    assert not (tmp_path / "library").exists()


def test_platform_recovery_action_can_resolve_after_acknowledgment_when_health_recovers(
    tmp_path: Path,
) -> None:
    repository = ObjectStorePlatformRecoveryActionRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    )
    degraded = _health(
        PlatformHealth(
            status="degraded",
            capability_count=5,
            ready_capabilities=("app_data_dir", "backup_destination", "file_picker", "system_info"),
            degraded_capabilities=("worker_lifecycle",),
            missing_capabilities=(),
            os_path_leaks=(),
        )
    )
    recovered = _health(
        PlatformHealth(
            status="ready",
            capability_count=5,
            ready_capabilities=(
                "app_data_dir",
                "backup_destination",
                "file_picker",
                "system_info",
                "worker_lifecycle",
            ),
            degraded_capabilities=(),
            missing_capabilities=(),
            os_path_leaks=(),
        )
    )
    action = CreatePlatformRecoveryActions(actions=repository).execute(
        health=degraded,
        created_at="2026-07-01T02:12:00+08:00",
    ).actions[0]
    acknowledged = ReviewPlatformRecoveryAction(actions=repository).acknowledge(
        action_id=str(action["id"]),
        decided_by="user",
        note="Noted.",
        decided_at="2026-07-01T02:13:00+08:00",
    ).action

    resolved = ResolvePlatformRecoveryAction(actions=repository).execute(
        action_id=str(action["id"]),
        recovered_health=recovered,
        resolved_by="system",
        resolved_at="2026-07-01T02:14:00+08:00",
    ).action

    assert acknowledged["status"] == "acknowledged"
    assert resolved["status"] == "resolved"
    assert resolved["user_decision"] == acknowledged["user_decision"]
    assert resolved["resolution"]["readiness_verified"] is True
    assert resolved["resolution"]["recovered_capability"] == "worker_lifecycle"
    assert validate_contract_instance(
        "platform_recovery_action.schema.json",
        _schema("platform_recovery_action.schema.json"),
        resolved,
    ) == []
    assert not (tmp_path / "library").exists()


def test_platform_recovery_action_resolution_rejects_acknowledgment_without_recovery(
    tmp_path: Path,
) -> None:
    repository = ObjectStorePlatformRecoveryActionRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    )
    still_degraded = _health(
        PlatformHealth(
            status="degraded",
            capability_count=5,
            ready_capabilities=("app_data_dir", "backup_destination", "file_picker", "system_info"),
            degraded_capabilities=("worker_lifecycle",),
            missing_capabilities=(),
            os_path_leaks=(),
        )
    )
    action = CreatePlatformRecoveryActions(actions=repository).execute(
        health=still_degraded,
        created_at="2026-07-01T02:15:00+08:00",
    ).actions[0]
    acknowledged = ReviewPlatformRecoveryAction(actions=repository).acknowledge(
        action_id=str(action["id"]),
        decided_by="user",
        decided_at="2026-07-01T02:16:00+08:00",
    ).action

    with pytest.raises(PlatformRecoveryActionError, match="verified ready capability"):
        ResolvePlatformRecoveryAction(actions=repository).execute(
            action_id=str(action["id"]),
            recovered_health=still_degraded,
            resolved_by="system",
            resolved_at="2026-07-01T02:17:00+08:00",
        )

    assert repository.get(str(action["id"])) == acknowledged
    assert repository.get(str(action["id"]))["status"] == "acknowledged"
    assert not (tmp_path / "library").exists()


def test_platform_recovery_action_resolution_rejects_dismissed_action(tmp_path: Path) -> None:
    repository = ObjectStorePlatformRecoveryActionRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    )
    degraded = _health(
        PlatformHealth(
            status="degraded",
            capability_count=5,
            ready_capabilities=("app_data_dir", "backup_destination", "file_picker", "system_info"),
            degraded_capabilities=("worker_lifecycle",),
            missing_capabilities=(),
            os_path_leaks=(),
        )
    )
    recovered = _health(
        PlatformHealth(
            status="ready",
            capability_count=5,
            ready_capabilities=(
                "app_data_dir",
                "backup_destination",
                "file_picker",
                "system_info",
                "worker_lifecycle",
            ),
            degraded_capabilities=(),
            missing_capabilities=(),
            os_path_leaks=(),
        )
    )
    action = CreatePlatformRecoveryActions(actions=repository).execute(
        health=degraded,
        created_at="2026-07-01T02:18:00+08:00",
    ).actions[0]
    dismissed = ReviewPlatformRecoveryAction(actions=repository).dismiss(
        action_id=str(action["id"]),
        decided_by="user",
        decided_at="2026-07-01T02:19:00+08:00",
    ).action

    with pytest.raises(PlatformRecoveryActionError, match="open or acknowledged status"):
        ResolvePlatformRecoveryAction(actions=repository).execute(
            action_id=str(action["id"]),
            recovered_health=recovered,
            resolved_by="system",
            resolved_at="2026-07-01T02:20:00+08:00",
        )

    assert repository.get(str(action["id"])) == dismissed
    assert not (tmp_path / "library").exists()


def test_platform_recovery_actions_return_empty_for_ready_platform() -> None:
    writer = InMemoryPlatformRecoveryActions([])
    health = _health(
        PlatformHealth(
            status="ready",
            capability_count=5,
            ready_capabilities=(
                "app_data_dir",
                "backup_destination",
                "file_picker",
                "system_info",
                "worker_lifecycle",
            ),
            degraded_capabilities=(),
            missing_capabilities=(),
            os_path_leaks=(),
        )
    )

    result = CreatePlatformRecoveryActions(actions=writer).execute(health=health)

    assert result.actions == ()
    assert writer.items == []


def test_platform_recovery_action_repository_rejects_auto_execute(tmp_path: Path) -> None:
    repository = ObjectStorePlatformRecoveryActionRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    )
    invalid = json.loads(
        (CONTRACT_ROOT / "fixtures" / "platform_recovery_action" / "invalid-auto-execute.json").read_text(
            encoding="utf-8"
        )
    )

    with pytest.raises(ValueError, match="auto execute"):
        repository.save(invalid)


def test_platform_recovery_action_repository_rejects_fake_resolved_state(tmp_path: Path) -> None:
    repository = ObjectStorePlatformRecoveryActionRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    )
    invalid = json.loads(
        (
            CONTRACT_ROOT
            / "fixtures"
            / "platform_recovery_action"
            / "invalid-resolved-without-ready-health.json"
        ).read_text(encoding="utf-8")
    )

    with pytest.raises(ValueError, match="must not list recovered capability"):
        repository.save(invalid)


def test_platform_recovery_actions_composition_uses_temp_storage_only(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    use_case = build_platform_recovery_actions(repository_root, runtime_root=tmp_path)
    health = _health(
        PlatformHealth(
            status="degraded",
            capability_count=5,
            ready_capabilities=("app_data_dir", "backup_destination", "file_picker", "system_info"),
            degraded_capabilities=("worker_lifecycle",),
            missing_capabilities=(),
            os_path_leaks=(),
        )
    )

    result = use_case.execute(health=health, created_at="2026-07-01T02:02:00+08:00")

    assert len(result.actions) == 1
    assert (tmp_path / ".rebuild-data").exists()
    assert not (tmp_path / "library").exists()
