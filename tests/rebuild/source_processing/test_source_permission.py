from __future__ import annotations

import pytest

from core.source_processing import SourcePermissionAuthority, SourcePermissionConflict, SourcePermissionError
from core.storage_provider import JsonObjectStore


def authority(tmp_path) -> SourcePermissionAuthority:
    return SourcePermissionAuthority(JsonObjectStore(tmp_path / "objects", namespace_id="test"), namespace_id="test")


def grant(repo: SourcePermissionAuthority, *, command_id: str = "grant-1", expected_revision: int = 0):
    return repo.grant(
        project_id="project-a", permission_id="permission-a", source_id="source-a", platform="bilibili",
        source_manifest_ref="crp://test/source-manifests/projects/project-a/source-a-r1",
        source_manifest_revision="r1", metadata_evidence_ref="crp://test/platform-evidence/projects/project-a/source-a-r1",
        actor_id="user-a", command_id=command_id, created_at="2026-08-25T01:02:03Z", expected_revision=expected_revision,
    )


def test_grant_is_project_scoped_immutable_and_exact_command_replay_is_idempotent(tmp_path) -> None:
    repo = authority(tmp_path)
    first = grant(repo)
    replay = grant(repo)
    assert replay == first
    assert first.state == "granted"
    assert first.revision == 1
    assert first.revocation_generation == 0
    assert first.public_ref == "crp://test/source-permissions/projects/project-a/permission-a/r1"
    assert repo.current(project_id="project-a", permission_id="permission-a") == first
    assert repo.current(project_id="project-b", permission_id="permission-a") is None


def test_revoke_appends_revision_and_regrant_keeps_a_revocation_generation(tmp_path) -> None:
    repo = authority(tmp_path)
    granted = grant(repo)
    revoked = repo.revoke(project_id="project-a", permission_id="permission-a", actor_id="user-a", command_id="revoke-1", created_at="2026-08-25T01:03:03Z", expected_revision=1)
    regranted = grant(repo, command_id="grant-2", expected_revision=2)
    assert revoked.predecessor_ref == granted.public_ref
    assert revoked.state == "revoked"
    assert revoked.revocation_generation == 1
    assert regranted.state == "granted"
    assert regranted.revision == 3
    assert regranted.revocation_generation == 1
    assert repo.current_for_source(project_id="project-a", source_id="source-a", metadata_evidence_ref=granted.metadata_evidence_ref) == regranted


def test_stale_cas_or_identity_drift_cannot_change_an_existing_permission(tmp_path) -> None:
    repo = authority(tmp_path)
    grant(repo)
    with pytest.raises(SourcePermissionConflict, match="expected 0, current 1"):
        grant(repo, command_id="grant-2", expected_revision=0)
    with pytest.raises(SourcePermissionConflict, match="identity cannot change"):
        repo.grant(
            project_id="project-a", permission_id="permission-a", source_id="source-b", platform="bilibili",
            source_manifest_ref="crp://test/source-manifests/projects/project-a/source-b-r1", source_manifest_revision="r1",
            metadata_evidence_ref="crp://test/platform-evidence/projects/project-a/source-b-r1", actor_id="user-a",
            command_id="grant-2", created_at="2026-08-25T01:03:03Z", expected_revision=1,
        )


def test_only_active_permission_can_be_revoked_and_command_ids_cannot_be_reused(tmp_path) -> None:
    repo = authority(tmp_path)
    with pytest.raises(SourcePermissionConflict, match="does not exist"):
        repo.revoke(project_id="project-a", permission_id="permission-a", actor_id="user-a", command_id="revoke-1", created_at="2026-08-25T01:03:03Z", expected_revision=0)
    grant(repo)
    repo.revoke(project_id="project-a", permission_id="permission-a", actor_id="user-a", command_id="revoke-1", created_at="2026-08-25T01:03:03Z", expected_revision=1)
    with pytest.raises(SourcePermissionConflict, match="active"):
        repo.revoke(project_id="project-a", permission_id="permission-a", actor_id="user-a", command_id="revoke-2", created_at="2026-08-25T01:04:03Z", expected_revision=2)
    replay = repo.grant(
        project_id="project-a", permission_id="permission-a", source_id="source-a", platform="bilibili",
        source_manifest_ref="crp://test/source-manifests/projects/project-a/source-a-r1", source_manifest_revision="r1",
        metadata_evidence_ref="crp://test/platform-evidence/projects/project-a/source-a-r1", actor_id="user-a",
        command_id="grant-1", created_at="2026-08-25T01:05:03Z", expected_revision=2,
    )
    assert replay.revision == 1
    assert repo.current(project_id="project-a", permission_id="permission-a").state == "revoked"


def test_storage_corruption_and_cross_project_refs_fail_closed(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / "objects", namespace_id="test")
    repo = SourcePermissionAuthority(store, namespace_id="test")
    grant(repo)
    store.write("source_permissions", "project-a~permission-a~r2", {"schema_version": "1.0.0"}, expected_revision=0)
    with pytest.raises(SourcePermissionError, match="fields are invalid"):
        repo.current(project_id="project-a", permission_id="permission-a")
    with pytest.raises(SourcePermissionError, match="controlled crp"):
        repo.current_for_source(project_id="project-a", source_id="source-a", metadata_evidence_ref="https://outside.example/evidence")
    with pytest.raises(SourcePermissionError, match="namespace/project"):
        repo.current_for_source(project_id="project-b", source_id="source-a", metadata_evidence_ref="crp://test/platform-evidence/projects/project-a/source-a-r1")


def test_delayed_command_replay_keeps_the_original_server_timestamp(tmp_path) -> None:
    repo = authority(tmp_path)
    first = grant(repo, command_id="grant-delayed")
    replay = repo.grant(
        project_id="project-a", permission_id="permission-a", source_id="source-a", platform="bilibili",
        source_manifest_ref="crp://test/source-manifests/projects/project-a/source-a-r1",
        source_manifest_revision="r1",
        metadata_evidence_ref="crp://test/platform-evidence/projects/project-a/source-a-r1",
        actor_id="user-a", command_id="grant-delayed",
        created_at="2026-08-25T01:12:03Z", expected_revision=0,
    )
    assert replay == first
    assert replay.created_at == "2026-08-25T01:02:03Z"


def test_current_uses_direct_head_reads_without_listing_revision_collection(tmp_path, monkeypatch) -> None:
    repo = authority(tmp_path)
    granted = grant(repo)

    def reject_list(_self, _collection):
        raise AssertionError("provider hot path must not list permission revisions")

    monkeypatch.setattr(type(repo._object_store), "list", reject_list)
    assert repo.current(project_id="project-a", permission_id="permission-a") == granted
    assert repo.current_for_source(
        project_id="project-a", source_id="source-a",
        metadata_evidence_ref=granted.metadata_evidence_ref,
    ) == granted


def test_missing_head_is_rebuilt_from_direct_sequential_revision_reads(tmp_path, monkeypatch) -> None:
    repo = authority(tmp_path)
    grant(repo)
    revoked = repo.revoke(
        project_id="project-a", permission_id="permission-a", actor_id="user-a",
        command_id="revoke-repair", created_at="2026-08-25T01:06:03Z", expected_revision=1,
    )
    assert repo._object_store.delete("source_permission_heads", "project-a~permission-a")

    def reject_list(_self, _collection):
        raise AssertionError("head recovery must use deterministic revision ids")

    monkeypatch.setattr(type(repo._object_store), "list", reject_list)
    assert repo.current(project_id="project-a", permission_id="permission-a") == revoked


def test_head_cannot_mask_a_missing_immediate_immutable_predecessor(tmp_path) -> None:
    repo = authority(tmp_path)
    grant(repo)
    repo.revoke(
        project_id="project-a", permission_id="permission-a", actor_id="user-a",
        command_id="revoke-corrupt", created_at="2026-08-25T01:07:03Z", expected_revision=1,
    )
    assert repo._object_store.delete("source_permissions", "project-a~permission-a~r1")
    with pytest.raises(SourcePermissionError, match="chain is incomplete"):
        repo.current(project_id="project-a", permission_id="permission-a")


def test_head_cannot_mask_a_missing_older_immutable_revision(tmp_path) -> None:
    repo = authority(tmp_path)
    grant(repo)
    repo.revoke(
        project_id="project-a", permission_id="permission-a", actor_id="user-a",
        command_id="revoke-r2", created_at="2026-08-25T01:08:03Z", expected_revision=1,
    )
    grant(repo, command_id="grant-r3", expected_revision=2)
    assert repo._object_store.delete("source_permissions", "project-a~permission-a~r1")
    with pytest.raises(SourcePermissionError, match="chain is incomplete"):
        repo.current(project_id="project-a", permission_id="permission-a")
