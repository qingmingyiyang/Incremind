from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.security.project_boundary_profiles import ProjectBoundaryProfileConflict, ProjectBoundaryProfileStore
from core.ai_boundary import BoundaryGrant


def _grant() -> BoundaryGrant:
    return BoundaryGrant(
        grant_id="grant-1", subject_id="ai-kernel", project_id="alpha",
        target_id="core.read@tool:contract-v1",
        actions=("read",), data_classes=("project_content",), destinations=("local",),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1), revision=1,
    )


def test_create_and_revoke_grant_are_narrow_cas_mutations(tmp_path: Path) -> None:
    store = ProjectBoundaryProfileStore(tmp_path)
    baseline = store.update("alpha", mode="guarded", remote_default="review", enabled_sources=("mcp",), denied_effects=("delete",), expected_revision=0)
    created = store.create_grant("alpha", grant=_grant(), expected_revision=baseline.profile.revision)
    assert created.profile.revision == 2
    assert created.profile.enabled_sources == ("mcp",)
    assert created.profile.denied_effects == ("delete",)
    revoked = store.revoke_grant("alpha", grant_id="grant-1", expected_revision=2, expected_grant_revision=1)
    assert revoked.profile.revision == 3
    assert revoked.profile.persistent_grants[0].revoked is True
    assert revoked.profile.persistent_grants[0].revision == 2


def test_grant_store_rejects_duplicate_active_target_and_stale_grant_revision(tmp_path: Path) -> None:
    store = ProjectBoundaryProfileStore(tmp_path)
    store.create_grant("alpha", grant=_grant(), expected_revision=1)
    with pytest.raises(ProjectBoundaryProfileConflict):
        store.create_grant("alpha", grant=_grant(), expected_revision=2)
    with pytest.raises(ProjectBoundaryProfileConflict):
        store.revoke_grant("alpha", grant_id="grant-1", expected_revision=2, expected_grant_revision=2)


def test_grant_store_rejects_client_shaped_authority_and_duplicate_identity(tmp_path: Path) -> None:
    store = ProjectBoundaryProfileStore(tmp_path)
    with pytest.raises(ValueError, match="new grant authority"):
        store.create_grant("alpha", grant=replace(_grant(), subject_id="client"), expected_revision=1)
    store.create_grant("alpha", grant=_grant(), expected_revision=1)
    with pytest.raises(ProjectBoundaryProfileConflict, match="identity already exists"):
        store.create_grant(
            "alpha", grant=replace(_grant(), target_id="another-target"), expected_revision=2,
        )


@pytest.mark.parametrize("revision", [True, 0, -1])
def test_grant_store_rejects_non_positive_or_boolean_cas(tmp_path: Path, revision: object) -> None:
    store = ProjectBoundaryProfileStore(tmp_path)
    with pytest.raises(ValueError, match="must be positive"):
        store.create_grant("alpha", grant=_grant(), expected_revision=revision)  # type: ignore[arg-type]
    store.create_grant("alpha", grant=_grant(), expected_revision=1)
    with pytest.raises(ValueError, match="must be positive"):
        store.revoke_grant(
            "alpha", grant_id="grant-1", expected_revision=2,
            expected_grant_revision=revision,  # type: ignore[arg-type]
        )


def test_expired_target_can_receive_a_new_grant_identity(tmp_path: Path) -> None:
    store = ProjectBoundaryProfileStore(tmp_path)
    expired = replace(_grant(), expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    store.create_grant("alpha", grant=expired, expected_revision=1)
    renewed = replace(_grant(), grant_id="grant-2")
    snapshot = store.create_grant("alpha", grant=renewed, expected_revision=2)
    assert tuple(grant.grant_id for grant in snapshot.profile.persistent_grants) == ("grant-1", "grant-2")
