from pathlib import Path
from datetime import datetime, timezone
import sqlite3

import pytest

from backend.security.project_boundary_mode_command import (
    BoundaryModeCommandConflict,
    ProjectBoundaryModeCommandService,
)
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from core.ai_boundary import BoundaryGrant


def test_mode_command_binds_defaults_and_replays_without_second_write(tmp_path: Path) -> None:
    service = ProjectBoundaryModeCommandService(tmp_path)
    try:
        receipt = service.submit(project_id="alpha", command_id="mode-1", mode="sealed", expected_boundary_revision=1, expected_capability_revision=1)
        assert (receipt.status, receipt.boundary_revision, receipt.capability_revision) == ("completed", 2, 2)
        replay = service.submit(project_id="alpha", command_id="mode-1", mode="sealed", expected_boundary_revision=1, expected_capability_revision=1)
        assert replay == receipt
        boundary = ProjectBoundaryProfileStore(tmp_path).get("alpha").profile
        capability = ProjectCapabilityProfileStore(tmp_path).get("alpha").profile
        assert (boundary.mode, boundary.remote_default, boundary.revision) == ("sealed", "deny", 2)
        assert capability.revision == 2
        assert capability.boundary_profile_revision == boundary.revision
    finally:
        service.close()


def test_mode_command_rejects_semantic_reuse_and_active_project_conflict(tmp_path: Path) -> None:
    service = ProjectBoundaryModeCommandService(tmp_path)
    try:
        service.submit(project_id="alpha", command_id="mode-1", mode="open", expected_boundary_revision=1, expected_capability_revision=1)
        with pytest.raises(BoundaryModeCommandConflict):
            service.submit(project_id="alpha", command_id="mode-1", mode="sealed", expected_boundary_revision=1, expected_capability_revision=1)
    finally:
        service.close()


def test_mode_command_receipt_has_no_grant_or_payload_material(tmp_path: Path) -> None:
    service = ProjectBoundaryModeCommandService(tmp_path)
    try:
        receipt = service.submit(project_id="alpha", command_id="mode-1", mode="guarded", expected_boundary_revision=1, expected_capability_revision=1)
        assert set(receipt.public()) == {"command_id", "project_id", "mode", "remote_default", "actor_id", "status", "expected_boundary_revision", "expected_capability_revision", "boundary_revision", "capability_revision", "created_at", "updated_at"}
        assert receipt.remote_default == "review"
        assert receipt.actor_id == "desktop-user"
    finally:
        service.close()


def test_mode_command_can_switch_an_already_persisted_project_twice(tmp_path: Path) -> None:
    service = ProjectBoundaryModeCommandService(tmp_path)
    try:
        first = service.submit(
            project_id="alpha", command_id="mode-1", mode="open",
            expected_boundary_revision=1, expected_capability_revision=1,
        )
        second = service.submit(
            project_id="alpha", command_id="mode-2", mode="guarded",
            expected_boundary_revision=2, expected_capability_revision=2,
        )
        assert (first.boundary_revision, first.capability_revision) == (2, 2)
        assert (second.status, second.boundary_revision, second.capability_revision) == ("completed", 3, 3)
        assert ProjectBoundaryProfileStore(tmp_path).get("alpha").profile.remote_default == "review"
    finally:
        service.close()


def test_mode_command_preserves_boundary_grants_and_capability_selection(tmp_path: Path) -> None:
    boundary_store = ProjectBoundaryProfileStore(tmp_path)
    grant = BoundaryGrant(
        "grant-1", "desktop-user", "alpha", "document.write", ("write",),
        ("project_content",), ("local",), datetime(2027, 1, 1, tzinfo=timezone.utc), 1,
    )
    boundary = boundary_store.update(
        "alpha", mode="guarded", remote_default="review",
        enabled_sources=("core", "user-skills"), denied_effects=("delete",),
        persistent_grants=(grant,), expected_revision=0,
    ).profile
    capability_store = ProjectCapabilityProfileStore(tmp_path)
    capability_store.update(
        "alpha", expected_revision=0, boundary_profile_id=boundary.profile_id,
        boundary_profile_revision=boundary.revision, enabled_sources=("core", "plugin"),
        enabled_plugin_ids=("plugin-a",), allowed_tool_ids=("tool.read",),
        denied_tool_ids=("tool.delete",), preferred_model_tier="deep", max_tools=7,
    )
    service = ProjectBoundaryModeCommandService(tmp_path)
    try:
        receipt = service.submit(
            project_id="alpha", command_id="mode-1", mode="sealed",
            expected_boundary_revision=1, expected_capability_revision=1,
        )
        assert receipt.status == "completed"
        updated_boundary = boundary_store.get("alpha").profile
        assert updated_boundary.enabled_sources == ("core", "user-skills")
        assert updated_boundary.denied_effects == ("delete",)
        assert updated_boundary.persistent_grants == (grant,)
        updated_capability = capability_store.get("alpha").profile
        assert updated_capability.enabled_plugin_ids == ("plugin-a",)
        assert updated_capability.allowed_tool_ids == ("tool.read",)
        assert updated_capability.denied_tool_ids == ("tool.delete",)
        assert updated_capability.preferred_model_tier == "deep"
        assert updated_capability.max_tools == 7
    finally:
        service.close()


def test_mode_command_resumes_crash_after_boundary_write_without_second_write(tmp_path: Path) -> None:
    service = ProjectBoundaryModeCommandService(tmp_path)
    original_set = service._set

    def crash_before_boundary_receipt(receipt, status, **revisions):
        if status == "boundary_updated":
            raise RuntimeError("simulated receipt crash")
        return original_set(receipt, status, **revisions)

    try:
        service._set = crash_before_boundary_receipt  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="simulated"):
            service.submit(
                project_id="alpha", command_id="mode-1", mode="sealed",
                expected_boundary_revision=1, expected_capability_revision=1,
            )
        assert ProjectBoundaryProfileStore(tmp_path).get("alpha").profile.revision == 2
        assert service.get("mode-1").status == "prepared"  # type: ignore[union-attr]
        assert ProjectCapabilityProfileStore(tmp_path).get("alpha").profile.revision == 1

        service._set = original_set  # type: ignore[method-assign]
        receipt = service.submit(
            project_id="alpha", command_id="mode-1", mode="sealed",
            expected_boundary_revision=1, expected_capability_revision=1,
        )
        assert (receipt.status, receipt.boundary_revision, receipt.capability_revision) == ("completed", 2, 2)
    finally:
        service.close()


def test_capability_drift_after_boundary_write_enters_repair_state(tmp_path: Path) -> None:
    service = ProjectBoundaryModeCommandService(tmp_path)
    competing_store = ProjectCapabilityProfileStore(tmp_path)
    original_rebind = service._capability.rebind_boundary

    def conflicting_rebind(project_id, **arguments):
        current = competing_store.get(project_id).profile
        competing_store.rebind_boundary(
            project_id,
            expected_revision=current.revision,
            boundary_profile_id=current.boundary_profile_id,
            boundary_profile_revision=current.boundary_profile_revision,
        )
        return original_rebind(project_id, **arguments)

    try:
        service._capability.rebind_boundary = conflicting_rebind  # type: ignore[method-assign]
        receipt = service.submit(
            project_id="alpha", command_id="mode-1", mode="sealed",
            expected_boundary_revision=1, expected_capability_revision=1,
        )
        assert receipt.status == "requires_repair"
        assert ProjectBoundaryProfileStore(tmp_path).get("alpha").profile.revision == 2
        capability = competing_store.get("alpha").profile
        assert capability.revision == 2
        assert capability.boundary_profile_revision == 1
    finally:
        service.close()


def test_mode_command_resumes_crash_after_capability_write_without_second_write(tmp_path: Path) -> None:
    service = ProjectBoundaryModeCommandService(tmp_path)
    original_set = service._set

    def crash_before_completed_receipt(receipt, status, **revisions):
        if status == "completed":
            raise RuntimeError("simulated receipt crash")
        return original_set(receipt, status, **revisions)

    try:
        service._set = crash_before_completed_receipt  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="simulated"):
            service.submit(
                project_id="alpha", command_id="mode-1", mode="open",
                expected_boundary_revision=1, expected_capability_revision=1,
            )
        assert ProjectCapabilityProfileStore(tmp_path).get("alpha").profile.revision == 2

        service._set = original_set  # type: ignore[method-assign]
        receipt = service.submit(
            project_id="alpha", command_id="mode-1", mode="open",
            expected_boundary_revision=1, expected_capability_revision=1,
        )
        assert (receipt.status, receipt.boundary_revision, receipt.capability_revision) == ("completed", 2, 2)
    finally:
        service.close()


def test_active_crashed_command_blocks_a_second_command_for_the_project(tmp_path: Path) -> None:
    service = ProjectBoundaryModeCommandService(tmp_path)
    original_set = service._set

    def crash_before_boundary_receipt(receipt, status, **revisions):
        if status == "boundary_updated":
            raise RuntimeError("simulated receipt crash")
        return original_set(receipt, status, **revisions)

    try:
        service._set = crash_before_boundary_receipt  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            service.submit(
                project_id="alpha", command_id="mode-1", mode="open",
                expected_boundary_revision=1, expected_capability_revision=1,
            )
        with pytest.raises(BoundaryModeCommandConflict, match="another Boundary mode command"):
            service.submit(
                project_id="alpha", command_id="mode-2", mode="sealed",
                expected_boundary_revision=2, expected_capability_revision=1,
            )
    finally:
        service.close()


def test_receipt_transition_lock_is_a_controlled_conflict(tmp_path: Path) -> None:
    service = ProjectBoundaryModeCommandService(tmp_path)

    def busy_transition(_receipt, _status, **_revisions):
        raise sqlite3.OperationalError("database is locked")

    try:
        service._set = busy_transition  # type: ignore[method-assign]
        with pytest.raises(BoundaryModeCommandConflict, match="receipt authority is busy"):
            service.submit(
                project_id="alpha", command_id="mode-1", mode="sealed",
                expected_boundary_revision=1, expected_capability_revision=1,
            )
        assert ProjectBoundaryProfileStore(tmp_path).get("alpha").profile.revision == 2
    finally:
        service.close()


@pytest.mark.parametrize("revision", [0, -1, True])
def test_mode_command_rejects_non_public_revisions_before_receipt(tmp_path: Path, revision: object) -> None:
    service = ProjectBoundaryModeCommandService(tmp_path)
    try:
        with pytest.raises(ValueError, match="positive integers"):
            service.submit(
                project_id="alpha", command_id="mode-1", mode="open",
                expected_boundary_revision=revision, expected_capability_revision=1,  # type: ignore[arg-type]
            )
        assert service.get("mode-1") is None
    finally:
        service.close()
