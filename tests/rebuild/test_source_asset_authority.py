from __future__ import annotations

import hashlib

import pytest

from core.aggregate_repository_factory import (
    AUTHORITY_DATABASE_NAME,
    SOURCE_ASSET_AUTHORITY_MEMBERS,
    STRUCTURED_DATABASE_NAME,
    TARGET_IDENTITY,
    AggregateRepositoryFactory,
)
from core.storage_provider import (
    AggregateAuthorityEvidence,
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteSourceAssetMappingAdapter,
    SQLiteStructuredRecordStore,
    SourceAssetAuthorityReaderError,
    create_vault_backup,
    read_source_asset_authority,
    restore_vault_backup,
)


def _fixture(tmp_path):
    store = JsonObjectStore(tmp_path / ".rebuild-data")
    content = b"source asset authority"
    sha256 = hashlib.sha256(content).hexdigest()
    asset_id = f"original-file-{sha256[:16]}"
    asset_ref = f"crp-ref-default-assets-originals-{asset_id}"
    vault_ref = f"assets/originals/{sha256[:2]}/{asset_id}.txt"
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
        "provenance": "test",
    }
    store.write("workbench_original_assets", asset_id, asset, expected_revision=0)
    store.write("source_asset_links", str(link["id"]), link, expected_revision=0)
    return store, asset, link


def test_json_and_sqlite_readers_preserve_logical_asset_identity(tmp_path) -> None:
    store, asset, link = _fixture(tmp_path)
    json_snapshot = read_source_asset_authority(
        json_store=store,
        sqlite_records=None,
        authority_identity="json:object-store-v1",
    )
    records = SQLiteStructuredRecordStore(tmp_path / "structured.sqlite3")
    SQLiteSourceAssetMappingAdapter(records).stage_legacy_mappings(
        assets=(asset,),
        links=(link,),
    )
    sqlite_snapshot = read_source_asset_authority(
        json_store=store,
        sqlite_records=records,
        authority_identity="sqlite:structured-records-v1",
    )

    assert json_snapshot.assets == sqlite_snapshot.assets
    assert json_snapshot.links == sqlite_snapshot.links
    assert json_snapshot.blobs[0].sha256 == sqlite_snapshot.blobs[0].sha256
    assert json_snapshot.blobs[0].active_vault_ref == asset["vault_ref"]
    assert sqlite_snapshot.blobs[0].active_vault_ref == (
        f"assets/blobs/{asset['sha256'][:2]}/{asset['sha256']}"
    )
    assert sqlite_snapshot.blobs[0].legacy_vault_refs == (asset["vault_ref"],)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing-blob", "missing blob"),
        ("dangling-link", "missing asset"),
        ("hash-drift", "identity does not match"),
        ("object-id-drift", "object id does not match"),
        ("legacy-ref-drift", "legacy ref is absent"),
        ("byte-count-drift", "byte counts do not match"),
    ],
)
def test_sqlite_reader_fails_closed_for_incomplete_or_drifted_mapping(
    tmp_path,
    mutation: str,
    message: str,
) -> None:
    store, asset, link = _fixture(tmp_path)
    records = SQLiteStructuredRecordStore(tmp_path / "structured.sqlite3")
    SQLiteSourceAssetMappingAdapter(records).stage_legacy_mappings(
        assets=(asset,),
        links=(link,),
    )
    if mutation == "missing-blob":
        blob = records.read("asset_blobs", str(asset["sha256"]))
        assert blob is not None
        with records.begin() as uow:
            uow.delete(
                "asset_blobs",
                str(asset["sha256"]),
                expected_revision=blob.revision,
            )
            uow.commit()
    elif mutation == "dangling-link":
        current = records.read("source_asset_links", str(link["id"]))
        assert current is not None
        with records.begin() as uow:
            uow.put(
                "source_asset_links",
                str(link["id"]),
                {**current.payload, "asset_id": "missing"},
                expected_revision=current.revision,
            )
            uow.commit()
    elif mutation == "hash-drift":
        current = records.read("source_asset_links", str(link["id"]))
        assert current is not None
        with records.begin() as uow:
            uow.put(
                "source_asset_links",
                str(link["id"]),
                {**current.payload, "content_hash": "f" * 64},
                expected_revision=current.revision,
            )
            uow.commit()
    elif mutation == "object-id-drift":
        current = records.read("original_assets", str(asset["id"]))
        assert current is not None
        with records.begin() as uow:
            uow.put(
                "original_assets",
                "different-id",
                current.payload,
                expected_revision=0,
            )
            uow.delete(
                "original_assets",
                str(asset["id"]),
                expected_revision=current.revision,
            )
            uow.commit()
    elif mutation == "legacy-ref-drift":
        current = records.read("asset_blobs", str(asset["sha256"]))
        assert current is not None
        with records.begin() as uow:
            uow.put(
                "asset_blobs",
                str(asset["sha256"]),
                {**current.payload, "legacy_vault_refs": ["assets/originals/other.bin"]},
                expected_revision=current.revision,
            )
            uow.commit()
    elif mutation == "byte-count-drift":
        current = records.read("original_assets", str(asset["id"]))
        assert current is not None
        with records.begin() as uow:
            uow.put(
                "original_assets",
                f"{asset['id']}-duplicate",
                {
                    **current.payload,
                    "legacy_asset_id": f"{asset['id']}-duplicate",
                    "legacy_asset_ref": f"{asset['asset_ref']}-duplicate",
                    "byte_count": int(asset["byte_count"]) + 1,
                },
                expected_revision=0,
            )
            uow.commit()

    with pytest.raises(SourceAssetAuthorityReaderError, match=message):
        read_source_asset_authority(
            json_store=store,
            sqlite_records=records,
            authority_identity="sqlite:structured-records-v1",
        )


def test_reader_is_body_free_and_does_not_mutate_either_authority(tmp_path) -> None:
    store, asset, link = _fixture(tmp_path)
    records = SQLiteStructuredRecordStore(tmp_path / "structured.sqlite3")
    SQLiteSourceAssetMappingAdapter(records).stage_legacy_mappings(
        assets=(asset,),
        links=(link,),
    )
    json_before = tuple(store.list("workbench_original_assets"))
    sqlite_before = tuple(records.list("original_assets"))

    snapshot = read_source_asset_authority(
        json_store=store,
        sqlite_records=records,
        authority_identity="sqlite:structured-records-v1",
    )

    assert not hasattr(snapshot.assets[0], "display_name")
    assert tuple(store.list("workbench_original_assets")) == json_before
    assert tuple(records.list("original_assets")) == sqlite_before


def test_verified_backup_restore_preserves_active_compound_and_blob_mapping(
    tmp_path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    store, asset, link = _fixture(vault)
    blob_path = vault / f"assets/blobs/{asset['sha256'][:2]}/{asset['sha256']}"
    blob_path.parent.mkdir(parents=True)
    blob_path.write_bytes(b"source asset authority")
    records = SQLiteStructuredRecordStore(
        vault / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    )
    SQLiteSourceAssetMappingAdapter(records).stage_legacy_mappings(
        assets=(asset,),
        links=(link,),
    )
    evidence = AggregateAuthorityEvidence(
        migration_id="source-asset-backup-v1",
        source_fingerprint="a" * 64,
        target_fingerprint="b" * 64,
        target_identity=TARGET_IDENTITY,
    )
    authority = SQLiteAggregateAuthorityStore(
        vault / ".rebuild-data" / AUTHORITY_DATABASE_NAME
    )
    for member in SOURCE_ASSET_AUTHORITY_MEMBERS:
        initial = authority.create_json_active(
            namespace_id="default",
            aggregate=member,
            reason="backup fixture",
        )
        staged = authority.transition(
            namespace_id="default",
            aggregate=member,
            expected_revision=initial.revision,
            to_state="sqlite_staged",
            evidence=evidence,
            reason="backup fixture staged",
        )
        authority.transition(
            namespace_id="default",
            aggregate=member,
            expected_revision=staged.revision,
            to_state="sqlite_active",
            evidence=evidence,
            reason="backup fixture active",
        )
        with records.begin() as uow:
            uow.put(
                "aggregate_authority_targets",
                f"default~{member}",
                {
                    "namespace_id": "default",
                    "aggregate": member,
                    "target_identity": TARGET_IDENTITY,
                    "source_fingerprint": evidence.source_fingerprint,
                    "target_fingerprint": evidence.target_fingerprint,
                    "migration_id": evidence.migration_id,
                },
                expected_revision=0,
            )
            uow.commit()

    backup = create_vault_backup(
        source_root=vault,
        backups_root=tmp_path / "backups",
        snapshot_id="source-asset-active-v1",
    )
    restored = tmp_path / "restored"
    restore_vault_backup(
        snapshot_root=backup.snapshot_root,
        target_root=restored,
    )
    restored_store = JsonObjectStore(restored / ".rebuild-data")
    resolution = AggregateRepositoryFactory(
        runtime_root=restored,
        namespace_id="default",
        json_store=restored_store,
    ).source_asset_authority_resolution()
    snapshot = read_source_asset_authority(
        json_store=restored_store,
        sqlite_records=resolution.records,
        authority_identity=resolution.authority_identity,
    )

    assert resolution.authority_identity == TARGET_IDENTITY
    assert snapshot.assets[0].asset_id == asset["id"]
    assert snapshot.links[0].link_id == link["id"]
    assert snapshot.blobs[0].active_vault_ref == (
        f"assets/blobs/{asset['sha256'][:2]}/{asset['sha256']}"
    )
    assert (
        restored / snapshot.blobs[0].active_vault_ref
    ).read_bytes() == b"source asset authority"
