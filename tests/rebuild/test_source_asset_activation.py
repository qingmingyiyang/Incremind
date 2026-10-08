from __future__ import annotations

import hashlib
import shutil

import pytest

from core.aggregate_repository_factory import (
    AUTHORITY_DATABASE_NAME,
    SOURCE_ASSET_AUTHORITY_MEMBERS,
    STRUCTURED_DATABASE_NAME,
    AggregateRepositoryFactory,
)
from core.storage_provider import (
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteSourceAssetMappingAdapter,
    SQLiteStructuredRecordStore,
    SourceAssetActivationError,
    activate_staged_source_asset_vault,
    create_vault_backup,
)


def _fixture(tmp_path):
    source = tmp_path / "source-vault"
    library = source / "library"
    store = JsonObjectStore(source / ".rebuild-data", legacy_root=library)
    content = b"activation fixture"
    sha256 = hashlib.sha256(content).hexdigest()
    asset_id = f"original-file-{sha256[:16]}"
    asset_ref = f"crp-ref-default-assets-originals-{asset_id}"
    vault_ref = f"assets/originals/{sha256[:2]}/{asset_id}.txt"
    original = library / vault_ref
    original.parent.mkdir(parents=True)
    original.write_bytes(content)
    asset = {
        "id": asset_id,
        "asset_ref": asset_ref,
        "sha256": sha256,
        "byte_count": len(content),
        "vault_ref": vault_ref,
        "metadata": {"source_kind": "file"},
    }
    link = {
        "id": f"source-asset-source-1-{asset_id}",
        "source_id": "source-1",
        "asset_id": asset_id,
        "asset_ref": asset_ref,
        "content_hash": sha256,
        "role": "original",
        "provenance": "activation-test",
    }
    store.write("sources", "source-1", {"id": "source-1"}, expected_revision=0)
    store.write("workbench_original_assets", asset_id, asset, expected_revision=0)
    store.write("source_asset_links", str(link["id"]), link, expected_revision=0)
    backup = create_vault_backup(
        source_root=source,
        backups_root=tmp_path / "backups",
        snapshot_id="source-asset-pre-activation",
    )
    target = tmp_path / "target-vault"
    shutil.copytree(source, target)
    records = SQLiteStructuredRecordStore(
        target / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    )
    SQLiteSourceAssetMappingAdapter(records).stage_legacy_mappings(
        assets=(asset,),
        links=(link,),
    )
    canonical = target / "library" / f"assets/blobs/{sha256[:2]}/{sha256}"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(content)
    return source, target, backup.snapshot_root, asset, link, records


def test_activation_selects_sqlite_and_is_idempotent(tmp_path) -> None:
    source, target, backup, asset, _link, _records = _fixture(tmp_path)

    first = activate_staged_source_asset_vault(
        source_vault_root=source,
        target_vault_root=target,
        backup_snapshot_root=backup,
        namespace_id="default",
        migration_id="source-asset-activation-v1",
    )
    authority = SQLiteAggregateAuthorityStore(
        target / ".rebuild-data" / AUTHORITY_DATABASE_NAME
    )
    revisions = tuple(
        authority.get("default", member).revision
        for member in SOURCE_ASSET_AUTHORITY_MEMBERS
    )
    second = activate_staged_source_asset_vault(
        source_vault_root=source,
        target_vault_root=target,
        backup_snapshot_root=backup,
        namespace_id="default",
        migration_id="source-asset-activation-v1",
    )

    assert first.state == "sqlite_active"
    assert first.idempotent is False
    assert first.blob_count == first.asset_count == first.link_count == 1
    assert second.idempotent is True
    assert second.target_fingerprint == first.target_fingerprint
    assert tuple(
        authority.get("default", member).revision
        for member in SOURCE_ASSET_AUTHORITY_MEMBERS
    ) == revisions
    resolution = AggregateRepositoryFactory(
        runtime_root=target,
        namespace_id="default",
        json_store=JsonObjectStore(
            target / ".rebuild-data",
            legacy_root=target / "library",
        ),
    ).source_asset_authority_resolution()
    assert resolution.records is not None
    assert asset["id"] in {
        item.object_id for item in resolution.records.list("original_assets")
    }


def test_activation_failure_after_staging_rolls_all_members_back_to_json(
    tmp_path,
) -> None:
    source, target, backup, _asset, _link, _records = _fixture(tmp_path)

    def fail(stage: str) -> None:
        assert stage == "sqlite_staged"
        raise RuntimeError("injected activation failure")

    with pytest.raises(SourceAssetActivationError, match="aborted before cutover"):
        activate_staged_source_asset_vault(
            source_vault_root=source,
            target_vault_root=target,
            backup_snapshot_root=backup,
            namespace_id="default",
            migration_id="source-asset-activation-fault-v1",
            fault_hook=fail,
        )

    authority = SQLiteAggregateAuthorityStore(
        target / ".rebuild-data" / AUTHORITY_DATABASE_NAME
    )
    assert {
        authority.get("default", member).state
        for member in SOURCE_ASSET_AUTHORITY_MEMBERS
    } == {"json_active"}

    retried = activate_staged_source_asset_vault(
        source_vault_root=source,
        target_vault_root=target,
        backup_snapshot_root=backup,
        namespace_id="default",
        migration_id="source-asset-activation-fault-v1",
    )
    assert retried.state == "sqlite_active"
    assert retried.idempotent is False


def test_activation_rejects_blob_drift_before_creating_authority(tmp_path) -> None:
    source, target, backup, asset, _link, _records = _fixture(tmp_path)
    canonical = (
        target
        / "library"
        / f"assets/blobs/{asset['sha256'][:2]}/{asset['sha256']}"
    )
    canonical.write_bytes(b"drift")

    with pytest.raises(SourceAssetActivationError, match="hash does not match"):
        activate_staged_source_asset_vault(
            source_vault_root=source,
            target_vault_root=target,
            backup_snapshot_root=backup,
            namespace_id="default",
            migration_id="source-asset-activation-drift-v1",
        )

    assert not (
        target / ".rebuild-data" / AUTHORITY_DATABASE_NAME
    ).exists()


def test_activation_rejects_backup_or_clone_drift(tmp_path) -> None:
    source, target, backup, _asset, _link, _records = _fixture(tmp_path)
    (source / "untracked.txt").write_text("changed after backup", encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        activate_staged_source_asset_vault(
            source_vault_root=source,
            target_vault_root=target,
            backup_snapshot_root=backup,
            namespace_id="default",
            migration_id="source-asset-activation-backup-drift-v1",
        )
