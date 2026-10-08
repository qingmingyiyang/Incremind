from __future__ import annotations

import pytest

from core.storage_provider import (
    SQLiteSourceAssetMappingAdapter,
    SQLiteSourceAssetMappingError,
    SQLiteStructuredRecordStore,
    SQLiteUnitOfWorkConflict,
)


def _adapter(tmp_path) -> SQLiteSourceAssetMappingAdapter:
    return SQLiteSourceAssetMappingAdapter(SQLiteStructuredRecordStore(tmp_path / "adapter.sqlite3"))


def _asset(asset_id: str, sha256: str, vault_ref: str) -> dict[str, object]:
    return {
        "id": asset_id,
        "asset_ref": f"crp-ref-default-assets-originals-{asset_id}",
        "sha256": sha256,
        "vault_ref": vault_ref,
        "byte_count": 7,
        "metadata": {"source_kind": "file"},
    }


def _link(link_id: str, source_id: str, asset: dict[str, object]) -> dict[str, object]:
    return {
        "id": link_id,
        "source_id": source_id,
        "asset_id": asset["id"],
        "asset_ref": asset["asset_ref"],
        "content_hash": asset["sha256"],
        "role": "original",
        "provenance": "workbench_auto_intake",
    }


def test_adapter_stages_blob_asset_and_link_mappings_in_one_uow(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    sha256 = "a" * 64
    file_asset = _asset("original-file-aaaaaaaaaaaaaaaa", sha256, "assets/originals/aa/file.txt")
    image_asset = _asset("original-image-aaaaaaaaaaaaaaaa", sha256, "assets/originals/aa/image.png")

    result = adapter.stage_legacy_mappings(
        assets=(file_asset, image_asset),
        links=(
            _link("source-asset-source-file-original-file", "source-file", file_asset),
            _link("source-asset-source-image-original-image", "source-image", image_asset),
        ),
    )

    assert len(result.blobs) == 1
    assert result.blobs[0].object_id == sha256
    assert result.blobs[0].payload["canonical_vault_ref"] == f"assets/blobs/{sha256[:2]}/{sha256}"
    assert result.blobs[0].payload["legacy_vault_refs"] == [
        "assets/originals/aa/file.txt",
        "assets/originals/aa/image.png",
    ]
    assert [record.object_id for record in result.assets] == [file_asset["id"], image_asset["id"]]
    assert [record.payload["source_id"] for record in result.links] == ["source-file", "source-image"]

    reloaded = _adapter(tmp_path)
    assert reloaded.records.read("asset_blobs", sha256) == result.blobs[0]
    assert len(reloaded.records.list("original_assets")) == 2
    assert len(reloaded.records.list("source_asset_links")) == 2


def test_invalid_link_reference_leaves_no_prior_blob_or_asset_records(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    sha256 = "b" * 64
    asset = _asset("original-file-bbbbbbbbbbbbbbbb", sha256, "assets/originals/bb/file.txt")
    link = _link("source-asset-source-bad-original", "source-bad", asset)
    link["asset_id"] = "original-file-missing"

    with pytest.raises(SQLiteSourceAssetMappingError, match="unknown legacy asset"):
        adapter.stage_legacy_mappings(assets=(asset,), links=(link,))

    assert adapter.records.list("asset_blobs") == ()
    assert adapter.records.list("original_assets") == ()
    assert adapter.records.list("source_asset_links") == ()


def test_repeated_create_and_unsafe_legacy_vault_ref_fail_closed(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    sha256 = "c" * 64
    asset = _asset("original-file-cccccccccccccccc", sha256, "assets/originals/cc/file.txt")
    link = _link("source-asset-source-c-original", "source-c", asset)
    adapter.stage_legacy_mappings(assets=(asset,), links=(link,))

    with pytest.raises(SQLiteUnitOfWorkConflict, match="expected revision 0, found 1"):
        adapter.stage_legacy_mappings(assets=(asset,), links=(link,))

    unsafe = _asset("original-file-dddddddddddddddd", "d" * 64, "../outside.bin")
    with pytest.raises(SQLiteSourceAssetMappingError, match="vault_ref"):
        adapter.stage_legacy_mappings(assets=(unsafe,), links=())
