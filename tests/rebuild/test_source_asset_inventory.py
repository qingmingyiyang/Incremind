from __future__ import annotations

import hashlib

import pytest

from core.storage_provider import (
    MigrationLedgerConflict,
    SQLiteMigrationLedger,
    plan_source_asset_migration_dry_run,
    scan_source_asset_inventory,
)
from core.storage_provider import JsonObjectStore


def _legacy_fixture(tmp_path):
    rebuild_root = tmp_path / ".rebuild-data"
    library_root = tmp_path / "library"
    store = JsonObjectStore(rebuild_root, legacy_root=library_root)
    content = b"source asset inventory fixture"
    sha256 = hashlib.sha256(content).hexdigest()
    vault_ref = f"assets/originals/{sha256[:2]}/original-file-{sha256[:16]}.txt"
    blob_path = library_root / vault_ref
    blob_path.parent.mkdir(parents=True)
    blob_path.write_bytes(content)
    source_id = "source-file-inventory"
    asset_id = f"original-file-{sha256[:16]}"
    store.write("sources", source_id, {"id": source_id}, expected_revision=0)
    store.write(
        "workbench_original_assets",
        asset_id,
        {
            "id": asset_id,
            "asset_ref": f"crp-ref-default-assets-originals-{asset_id}",
            "sha256": sha256,
            "byte_count": len(content),
            "vault_ref": vault_ref,
        },
        expected_revision=0,
    )
    store.write(
        "source_asset_links",
        f"source-asset-{source_id}-{asset_id}",
        {
            "id": f"source-asset-{source_id}-{asset_id}",
            "source_id": source_id,
            "asset_id": asset_id,
            "asset_ref": f"crp-ref-default-assets-originals-{asset_id}",
            "content_hash": sha256,
        },
        expected_revision=0,
    )
    return rebuild_root, library_root, blob_path


def test_inventory_is_path_free_deterministic_and_ledger_dry_run_is_idempotent(tmp_path) -> None:
    rebuild_root, library_root, _blob_path = _legacy_fixture(tmp_path)

    first = scan_source_asset_inventory(rebuild_root, library_root=library_root, namespace_id="default")
    repeated = scan_source_asset_inventory(rebuild_root, library_root=library_root, namespace_id="default")
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")
    plan = plan_source_asset_migration_dry_run(
        ledger=ledger,
        migration_id="json-to-sqlite-source-assets-v1",
        target_schema_version=2,
        inventory=first,
        rollback_pointer="snapshot:source-assets-v1",
    )

    assert first == repeated
    assert first.blob_count == 1
    assert first.combined_inventory.object_count == 4
    assert [item.collection for item in first.combined_inventory.collections] == [
        "asset_blob_files",
        "source_asset_links",
        "sources",
        "workbench_original_assets",
    ]
    assert str(tmp_path) not in repr(first)
    assert plan == plan_source_asset_migration_dry_run(
        ledger=ledger,
        migration_id="json-to-sqlite-source-assets-v1",
        target_schema_version=2,
        inventory=repeated,
        rollback_pointer="snapshot:source-assets-v1",
    )


def test_inventory_rejects_missing_or_mismatched_asset_blob_and_bad_link(tmp_path) -> None:
    rebuild_root, library_root, blob_path = _legacy_fixture(tmp_path)
    blob_path.unlink()
    with pytest.raises(ValueError, match="blob is missing"):
        scan_source_asset_inventory(rebuild_root, library_root=library_root, namespace_id="default")

    rebuild_root, library_root, blob_path = _legacy_fixture(tmp_path / "mismatch")
    blob_path.write_bytes(b"different bytes")
    with pytest.raises(ValueError, match="hash mismatch"):
        scan_source_asset_inventory(rebuild_root, library_root=library_root, namespace_id="default")

    rebuild_root, library_root, _blob_path = _legacy_fixture(tmp_path / "bad-link")
    store = JsonObjectStore(rebuild_root, legacy_root=library_root)
    link = store.list("source_asset_links")[0]
    link["source_id"] = "missing-source"
    store.write("source_asset_links", str(link["id"]), link, expected_revision=None)
    with pytest.raises(ValueError, match="unknown source"):
        scan_source_asset_inventory(rebuild_root, library_root=library_root, namespace_id="default")


def test_ledger_rejects_changed_combined_inventory_for_same_migration_id(tmp_path) -> None:
    rebuild_root, library_root, blob_path = _legacy_fixture(tmp_path)
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")
    initial = scan_source_asset_inventory(rebuild_root, library_root=library_root, namespace_id="default")
    plan_source_asset_migration_dry_run(
        ledger=ledger,
        migration_id="json-to-sqlite-source-assets-v1",
        target_schema_version=2,
        inventory=initial,
        rollback_pointer="snapshot:source-assets-v1",
    )
    blob_path.write_bytes(b"changed source asset blob")
    changed = scan_source_asset_inventory(rebuild_root, library_root=library_root, namespace_id="default", validate_assets=False)

    with pytest.raises(MigrationLedgerConflict, match="input fingerprint"):
        plan_source_asset_migration_dry_run(
            ledger=ledger,
            migration_id="json-to-sqlite-source-assets-v1",
            target_schema_version=2,
            inventory=changed,
            rollback_pointer="snapshot:source-assets-v1",
        )
