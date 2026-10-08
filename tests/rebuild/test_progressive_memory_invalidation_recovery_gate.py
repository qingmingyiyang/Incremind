from __future__ import annotations

from pathlib import Path

from core.product_core.memory_projection_authority_contract import (
    authority_snapshot_fingerprint,
)
from core.product_core.memory_projection_repository import (
    ObjectStoreMemoryProjectionRepository,
)
from core.product_core.progressive_memory_scale_benchmark import (
    _synthetic_snapshot,
)
from core.product_core.progressive_recall_shadow import (
    route_progressive_memory_r0,
)
from core.storage_provider import JsonObjectStore
from core.storage_provider.vault_backup_restore import (
    create_vault_backup,
    restore_vault_backup,
)


def _store(vault_root: Path) -> JsonObjectStore:
    return JsonObjectStore(
        vault_root / ".rebuild-data",
        legacy_root=vault_root / "library",
    )


def _activate(
    repository: ObjectStoreMemoryProjectionRepository,
    snapshot,
    *,
    job_id: str,
) -> str:
    fingerprint = authority_snapshot_fingerprint(snapshot)
    repository.begin_rebuild(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=fingerprint,
        job_id=job_id,
        updated_at="2026-07-26T15:00:00+08:00",
    )
    artifact_id = repository.stage_projection(
        snapshot.build(generated_at="2026-07-26T15:00:00+08:00")
    )
    repository.activate_staged(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=fingerprint,
        job_id=job_id,
        artifact_id=artifact_id,
        updated_at="2026-07-26T15:01:00+08:00",
    )
    return fingerprint


def test_verified_vault_backup_restore_preserves_matching_projection_only(
    tmp_path: Path,
) -> None:
    active = tmp_path / "active-vault"
    active.mkdir()
    snapshot, topic = _synthetic_snapshot(1_000)
    active_repository = ObjectStoreMemoryProjectionRepository(_store(active))
    fingerprint = _activate(
        active_repository,
        snapshot,
        job_id="backup-source-job",
    )
    backup = create_vault_backup(
        source_root=active,
        backups_root=tmp_path / "backups",
        snapshot_id="progressive-memory-gate",
    )

    restored_root = tmp_path / "restored-vault"
    restore_vault_backup(
        snapshot_root=backup.snapshot_root,
        target_root=restored_root,
    )
    restored_repository = ObjectStoreMemoryProjectionRepository(
        _store(restored_root)
    )
    restored = restored_repository.load_current(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=fingerprint,
    )
    route = route_progressive_memory_r0(
        read_result=restored,
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=fingerprint,
        query=topic,
    )

    assert restored.status == "fresh"
    assert restored.fallback_to_authority is False
    assert route.status == "routed"
    assert route.reason_code == "high_confidence"


def test_restored_projection_fails_closed_for_newer_authority_until_rebuilt(
    tmp_path: Path,
) -> None:
    active = tmp_path / "active-vault"
    active.mkdir()
    restored_snapshot, topic = _synthetic_snapshot(1_000)
    restored_repository = ObjectStoreMemoryProjectionRepository(_store(active))
    old_fingerprint = _activate(
        restored_repository,
        restored_snapshot,
        job_id="old-authority-job",
    )
    backup = create_vault_backup(
        source_root=active,
        backups_root=tmp_path / "backups",
        snapshot_id="progressive-memory-stale-gate",
    )
    restored_root = tmp_path / "restored-vault"
    restore_vault_backup(
        snapshot_root=backup.snapshot_root,
        target_root=restored_root,
    )
    repository = ObjectStoreMemoryProjectionRepository(_store(restored_root))
    current_snapshot, _ = _synthetic_snapshot(1_100)
    current_fingerprint = authority_snapshot_fingerprint(current_snapshot)

    stale = repository.load_current(
        project_id=current_snapshot.project_id,
        authority_identity=current_snapshot.authority_identity,
        authority_fingerprint=current_fingerprint,
    )
    route = route_progressive_memory_r0(
        read_result=stale,
        project_id=current_snapshot.project_id,
        authority_identity=current_snapshot.authority_identity,
        authority_fingerprint=current_fingerprint,
        query=topic,
    )

    assert current_fingerprint != old_fingerprint
    assert stale.status == "stale"
    assert stale.projection is None
    assert stale.fallback_to_authority is True
    assert route.status == "fallback"

    _activate(repository, current_snapshot, job_id="current-authority-job")
    rebuilt = repository.load_current(
        project_id=current_snapshot.project_id,
        authority_identity=current_snapshot.authority_identity,
        authority_fingerprint=current_fingerprint,
    )
    assert rebuilt.status == "fresh"
    assert rebuilt.projection is not None
