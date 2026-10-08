from __future__ import annotations

import hashlib

import pytest

from core.storage_provider import (
    SQLiteMigrationLedger,
    SQLiteSourceAssetMappingAdapter,
    SQLiteStructuredRecordStore,
    SourceAssetMigrationExecutionError,
    execute_source_asset_fixture_migration,
    plan_source_asset_migration_dry_run,
    scan_source_asset_inventory,
)
from core.storage_provider import JsonObjectStore


def _fixture(tmp_path):
    rebuild_root = tmp_path / "legacy" / ".rebuild-data"
    library_root = tmp_path / "legacy" / "library"
    store = JsonObjectStore(rebuild_root, legacy_root=library_root)
    content = b"executor fixture"
    sha256 = hashlib.sha256(content).hexdigest()
    vault_ref = f"assets/originals/{sha256[:2]}/original-file-{sha256[:16]}.txt"
    path = library_root / vault_ref
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    asset_id = f"original-file-{sha256[:16]}"
    source_id = "source-executor"
    asset = {"id": asset_id, "asset_ref": f"crp-ref-default-assets-originals-{asset_id}", "sha256": sha256, "byte_count": len(content), "vault_ref": vault_ref, "metadata": {}}
    link = {"id": f"source-asset-{source_id}-{asset_id}", "source_id": source_id, "asset_id": asset_id, "asset_ref": asset["asset_ref"], "content_hash": sha256, "role": "original", "provenance": "test"}
    store.write("sources", source_id, {"id": source_id}, expected_revision=0)
    store.write("workbench_original_assets", asset_id, asset, expected_revision=0)
    store.write("source_asset_links", str(link["id"]), link, expected_revision=0)
    inventory = scan_source_asset_inventory(rebuild_root, library_root=library_root, namespace_id="default")
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")
    plan = plan_source_asset_migration_dry_run(ledger=ledger, migration_id="fixture-source-assets-v1", target_schema_version=2, inventory=inventory, rollback_pointer="snapshot:fixture")
    adapter = SQLiteSourceAssetMappingAdapter(SQLiteStructuredRecordStore(tmp_path / "mapping.sqlite3"))
    return rebuild_root, library_root, asset, link, ledger, plan, adapter, path


def test_executor_copies_canonical_blob_validates_and_writes_mapping(tmp_path) -> None:
    rebuild_root, library_root, asset, link, ledger, plan, adapter, source_path = _fixture(tmp_path)
    target = tmp_path / "target-library"

    result = execute_source_asset_fixture_migration(
        rebuild_root=rebuild_root,
        legacy_library_root=library_root,
        target_library_root=target,
        ledger=ledger,
        dry_run=plan,
        assets=(asset,),
        links=(link,),
        adapter=adapter,
    )

    canonical = target / "assets" / "blobs" / asset["sha256"][:2] / asset["sha256"]
    assert result.copied_blob_refs == (f"assets/blobs/{asset['sha256'][:2]}/{asset['sha256']}",)
    assert canonical.read_bytes() == source_path.read_bytes()
    assert len(result.mapping.assets) == 1
    assert source_path.read_bytes() == b"executor fixture"


def test_executor_rejects_nonempty_target_and_rolls_back_new_target_on_mapping_failure(tmp_path) -> None:
    rebuild_root, library_root, asset, link, ledger, plan, adapter, _source_path = _fixture(tmp_path)
    target = tmp_path / "target-library"
    target.mkdir()
    (target / "existing.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(SourceAssetMigrationExecutionError, match="target is not empty"):
        execute_source_asset_fixture_migration(rebuild_root=rebuild_root, legacy_library_root=library_root, target_library_root=target, ledger=ledger, dry_run=plan, assets=(asset,), links=(link,), adapter=adapter)
    assert (target / "existing.txt").read_text(encoding="utf-8") == "keep"

    class _FailingAdapter:
        def stage_legacy_mappings(self, **_kwargs):
            raise RuntimeError("mapping failed")

    empty_target = tmp_path / "empty-target"
    with pytest.raises(SourceAssetMigrationExecutionError, match="mapping failed"):
        execute_source_asset_fixture_migration(rebuild_root=rebuild_root, legacy_library_root=library_root, target_library_root=empty_target, ledger=ledger, dry_run=plan, assets=(asset,), links=(link,), adapter=_FailingAdapter())
    assert empty_target.exists() is False


def test_executor_rejects_target_nested_in_legacy_library(tmp_path) -> None:
    rebuild_root, library_root, asset, link, ledger, plan, adapter, _source_path = _fixture(tmp_path)

    with pytest.raises(SourceAssetMigrationExecutionError, match="inside source"):
        execute_source_asset_fixture_migration(rebuild_root=rebuild_root, legacy_library_root=library_root, target_library_root=library_root / "target", ledger=ledger, dry_run=plan, assets=(asset,), links=(link,), adapter=adapter)
