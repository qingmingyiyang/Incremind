from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from backend.security.project_boundary_grant_command import (
    BoundaryGrantCommandConflict,
    BoundaryGrantCommandError,
    ProjectBoundaryGrantCommandService,
    resolve_grant_target,
)
from backend.security.project_boundary_mode_command import ProjectBoundaryModeCommandService
from backend.security.project_boundary_mutation_reservation import ProjectBoundaryMutationReservation
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from core.ai_kernel import CapabilityDefinition


def _capability(*, approval: bool = False) -> CapabilityDefinition:
    return CapabilityDefinition(
        "memory.recall", 1, "read", approval, "read_only",
        "crp://input", "crp://output",
    )


def _target(tmp_path: Path):
    return resolve_grant_target(
        stable_id="memory.recall",
        profile=ProjectCapabilityProfileStore(tmp_path).get("alpha").profile,
        boundary=ProjectBoundaryProfileStore(tmp_path).get("alpha").profile,
        capabilities=(_capability(),), registry_generation=4,
        expected_registry_generation=4,
    )


def test_create_replays_exact_receipt_without_extending_expiry_and_revoke_needs_no_runtime(tmp_path: Path) -> None:
    service = ProjectBoundaryGrantCommandService(tmp_path)
    try:
        first = service.create(
            project_id="alpha", command_id="create-1", target=_target(tmp_path),
            duration="24_hours", expected_boundary_revision=1,
            expected_capability_revision=1,
        )
        replay = service.create(
            project_id="alpha", command_id="create-1", target=_target(tmp_path),
            duration="24_hours", expected_boundary_revision=1,
            expected_capability_revision=1,
        )
        assert replay == first
        assert first.status == "completed" and first.expires_at is not None
        assert (first.boundary_revision, first.capability_revision, first.grant_revision) == (2, 2, 1)
        revoked = service.revoke(
            project_id="alpha", command_id="revoke-1", grant_id=first.grant_id,
            expected_boundary_revision=2, expected_capability_revision=2,
            expected_grant_revision=1,
        )
        assert (revoked.status, revoked.boundary_revision, revoked.capability_revision, revoked.grant_revision) == ("completed", 3, 3, 2)
        grant = ProjectBoundaryProfileStore(tmp_path).get("alpha").profile.persistent_grants[0]
        assert grant.revoked is True and grant.revision == 2
    finally:
        service.close()


def test_create_recovers_crashes_after_each_authority_write(tmp_path: Path) -> None:
    service = ProjectBoundaryGrantCommandService(tmp_path)
    original_set = service._set

    def crash_boundary(receipt, status, **changes):
        if status == "boundary_updated":
            raise RuntimeError("crash after Boundary write")
        return original_set(receipt, status, **changes)

    try:
        service._set = crash_boundary  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="Boundary"):
            service.create(
                project_id="alpha", command_id="create-1", target=_target(tmp_path),
                duration="until_revoked", expected_boundary_revision=1,
                expected_capability_revision=1,
            )
        assert service.get("create-1").status == "prepared"  # type: ignore[union-attr]
        assert ProjectBoundaryProfileStore(tmp_path).get("alpha").profile.revision == 2

        service._set = original_set  # type: ignore[method-assign]
        completed = service.create(
            project_id="alpha", command_id="create-1", target=_target_for_written_boundary(tmp_path),
            duration="until_revoked", expected_boundary_revision=1,
            expected_capability_revision=1,
        )
        assert completed.status == "completed"
        assert len(ProjectBoundaryProfileStore(tmp_path).get("alpha").profile.persistent_grants) == 1
    finally:
        service.close()


def test_create_recovers_crash_after_capability_write(tmp_path: Path) -> None:
    service = ProjectBoundaryGrantCommandService(tmp_path)
    original_set = service._set

    def crash_completed(receipt, status, **changes):
        if status == "completed":
            raise RuntimeError("crash after Capability write")
        return original_set(receipt, status, **changes)

    try:
        service._set = crash_completed  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="Capability"):
            service.create(
                project_id="alpha", command_id="create-1", target=_target(tmp_path),
                duration="until_revoked", expected_boundary_revision=1,
                expected_capability_revision=1,
            )
        assert ProjectCapabilityProfileStore(tmp_path).get("alpha").profile.revision == 2
        service._set = original_set  # type: ignore[method-assign]
        receipt = service.create(
            project_id="alpha", command_id="create-1", target=_target(tmp_path),
            duration="until_revoked", expected_boundary_revision=1,
            expected_capability_revision=1,
        )
        assert receipt.status == "completed"
    finally:
        service.close()


def test_grant_and_mode_commands_share_one_project_reservation(tmp_path: Path) -> None:
    grant = ProjectBoundaryGrantCommandService(tmp_path)
    original_set = grant._set

    def crash_boundary(receipt, status, **changes):
        if status == "boundary_updated":
            raise RuntimeError("leave active")
        return original_set(receipt, status, **changes)

    try:
        grant._set = crash_boundary  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            grant.create(
                project_id="alpha", command_id="create-1", target=_target(tmp_path),
                duration="until_revoked", expected_boundary_revision=1,
                expected_capability_revision=1,
            )
        mode = ProjectBoundaryModeCommandService(tmp_path)
        try:
            with pytest.raises(BoundaryGrantCommandConflict, match="active"):
                # Both conflict classes are ValueError; normalize the Mode
                # result below so this test asserts the shared authority.
                try:
                    mode.submit(
                        project_id="alpha", command_id="mode-1", mode="sealed",
                        expected_boundary_revision=2, expected_capability_revision=1,
                    )
                except ValueError as error:
                    raise BoundaryGrantCommandConflict(str(error)) from error
        finally:
            mode.close()
    finally:
        grant.close()


def test_exact_replay_recovers_reservation_crash_before_receipt(tmp_path: Path) -> None:
    ProjectBoundaryMutationReservation(tmp_path).reserve(
        project_id="alpha", command_id="create-1",
        command_kind="grant-create", semantic="memory.recall|until_revoked|1|1",
    )
    service = ProjectBoundaryGrantCommandService(tmp_path)
    try:
        assert service.get("create-1") is None
        receipt = service.create(
            project_id="alpha", command_id="create-1", target=_target(tmp_path),
            duration="until_revoked", expected_boundary_revision=1,
            expected_capability_revision=1,
        )
        assert receipt.status == "completed"
    finally:
        service.close()


def test_target_resolution_fails_closed_for_generation_approval_sealed_and_denied(tmp_path: Path) -> None:
    capability = ProjectCapabilityProfileStore(tmp_path).get("alpha").profile
    boundary = ProjectBoundaryProfileStore(tmp_path).get("alpha").profile
    common = dict(stable_id="memory.recall", profile=capability, capabilities=(_capability(),), registry_generation=4)
    with pytest.raises(BoundaryGrantCommandError, match="target_unavailable"):
        resolve_grant_target(boundary=boundary, expected_registry_generation=3, **common)
    with pytest.raises(BoundaryGrantCommandError, match="target_unavailable"):
        resolve_grant_target(boundary=boundary, capabilities=(_capability(approval=True),), stable_id="memory.recall", profile=capability, registry_generation=4, expected_registry_generation=4)
    for blocked in (
        replace(boundary, mode="sealed", remote_default="deny"),
        replace(boundary, denied_effects=("read",)),
    ):
        with pytest.raises(BoundaryGrantCommandError, match="target_unavailable"):
            resolve_grant_target(boundary=blocked, expected_registry_generation=4, **common)


def test_receipt_database_does_not_store_internal_target_or_schema_path(tmp_path: Path) -> None:
    service = ProjectBoundaryGrantCommandService(tmp_path)
    target = _target(tmp_path)
    try:
        receipt = service.create(
            project_id="alpha", command_id="create-1", target=target,
            duration="until_revoked", expected_boundary_revision=1,
            expected_capability_revision=1,
        )
        assert "target_id" not in receipt.public()
    finally:
        service.close()
    payload = (tmp_path / ".rebuild-data" / "boundary-grant-commands.sqlite3").read_bytes()
    assert target.target_id.encode() not in payload
    assert b"crp://input" not in payload


@pytest.mark.parametrize("value", [0, -1, True])
def test_grant_command_rejects_non_public_revisions_before_receipt(tmp_path: Path, value: object) -> None:
    service = ProjectBoundaryGrantCommandService(tmp_path)
    try:
        with pytest.raises(BoundaryGrantCommandError, match="positive integers"):
            service.create(
                project_id="alpha", command_id="create-1", target=_target(tmp_path),
                duration="24_hours", expected_boundary_revision=value,  # type: ignore[arg-type]
                expected_capability_revision=1,
            )
        assert service.get("create-1") is None
    finally:
        service.close()


def _target_for_written_boundary(tmp_path: Path):
    capability = ProjectCapabilityProfileStore(tmp_path).get("alpha").profile
    boundary = ProjectBoundaryProfileStore(tmp_path).get("alpha").profile
    return resolve_grant_target(
        stable_id="memory.recall", profile=capability, boundary=boundary,
        capabilities=(_capability(),), registry_generation=4,
        expected_registry_generation=4, allow_pending_boundary_revision=True,
    )
