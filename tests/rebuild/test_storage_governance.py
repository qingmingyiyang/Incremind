from __future__ import annotations

import json

import pytest

from core.product_core import storage_governance
from core.product_core.storage_governance import (
    StorageGovernanceError,
    build_storage_governance_report,
)


def _write(path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_storage_governance_reports_real_bytes_without_paths_or_content(
    tmp_path,
) -> None:
    active = tmp_path / ".rebuild-data"
    recovery = tmp_path / "..rebuild-data-recovery"
    assets = tmp_path / "library" / "assets"
    _write(active / "objects" / "default" / "sources" / "a.json", 10)
    _write(active / "structured-records.sqlite3", 11)
    _write(active / "memory-projections" / "r0.json", 12)
    _write(active / "runtime.json", 13)
    _write(recovery / "snapshots" / "snap-1" / "manifest.json", 14)
    _write(
        recovery / "original-asset-backups" / "asset-1" / "blob.bin",
        15,
    )
    _write(recovery / "operations" / "receipt.json", 16)
    _write(assets / "originals" / "aa" / "asset.bin", 17)

    report = build_storage_governance_report(
        active_vault_root=active,
        recovery_root=recovery,
        library_asset_root=assets,
        source_candidates=[
            {
                "source_id": "source-a",
                "retention_elapsed": True,
                "private_body": "PRIVATE-STORAGE-CANARY",
            },
            {
                "source_id": "source-b",
                "retention_elapsed": False,
            },
        ],
        original_asset_candidates=[
            {
                "asset_id": "asset-a",
                "link_status": "orphaned",
                "retention_elapsed": True,
                "byte_count": 17,
            },
            {
                "asset_id": "asset-linked",
                "link_status": "linked",
                "retention_elapsed": True,
                "byte_count": 999,
            },
        ],
        archived_document_count=2,
        recovery_point_count=1,
        backup_retention_count=5,
    )

    payload = report.to_payload()
    categories = {
        item["category_id"]: item
        for item in payload["categories"]
    }
    assert payload["total_bytes"] == sum(range(10, 18))
    assert payload["total_files"] == 8
    assert payload["known_reclaimable_bytes"] == 17
    assert categories["object_store"]["candidate_count"] == 1
    assert (
        categories["object_store"]["reclaimable_bytes_known"]
        is False
    )
    assert categories["original_assets"]["candidate_count"] == 1
    assert categories["original_assets"]["reclaimable_bytes"] == 17
    assert payload["lifecycle_summary"] == {
        "source_trash_count": 2,
        "source_purge_ready_count": 1,
        "original_asset_candidate_count": 1,
        "original_asset_reclaimable_bytes": 17,
        "archived_document_count": 2,
        "recovery_point_count": 1,
        "backup_retention_count": 5,
    }
    assert payload["content_included"] is False
    assert payload["paths_included"] is False
    assert payload["writes_performed"] is False
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "PRIVATE-STORAGE-CANARY" not in serialized
    assert str(tmp_path) not in serialized


def test_storage_governance_skips_symlinks_and_never_counts_targets(
    tmp_path,
    monkeypatch,
) -> None:
    active = tmp_path / ".rebuild-data"
    active.mkdir()
    simulated_reparse = active / "projection-link.bin"
    simulated_reparse.write_bytes(b"PRIVATE" * 200)
    original = storage_governance._stat_is_reparse
    monkeypatch.setattr(
        storage_governance,
        "_stat_is_reparse",
        lambda metadata: (
            metadata.st_size == simulated_reparse.stat().st_size
            or original(metadata)
        ),
    )

    payload = build_storage_governance_report(
        active_vault_root=active,
        recovery_root=tmp_path / "recovery",
        library_asset_root=tmp_path / "assets",
        source_candidates=[],
        original_asset_candidates=[],
        archived_document_count=None,
        recovery_point_count=0,
        backup_retention_count=5,
    ).to_payload()

    assert payload["total_bytes"] == 0
    assert payload["total_files"] == 0
    assert "active_vault_reparse_skipped" in payload["blockers"]
    assert "document_inventory_unavailable" in payload["blockers"]


def test_storage_governance_rejects_a_reparse_root_before_resolution(
    tmp_path,
    monkeypatch,
) -> None:
    active = tmp_path / ".rebuild-data"
    _write(active / "outside-canary.bin", 500)
    monkeypatch.setattr(
        storage_governance,
        "_has_reparse_ancestor",
        lambda path: path == active,
    )

    payload = build_storage_governance_report(
        active_vault_root=active,
        recovery_root=tmp_path / "recovery",
        library_asset_root=tmp_path / "assets",
        source_candidates=[],
        original_asset_candidates=[],
        archived_document_count=0,
        recovery_point_count=0,
        backup_retention_count=5,
    ).to_payload()

    assert payload["total_bytes"] == 0
    assert "active_vault_root_unsafe" in payload["blockers"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("archived_document_count", -1),
        ("recovery_point_count", -1),
        ("backup_retention_count", True),
    ],
)
def test_storage_governance_rejects_invalid_counts(
    tmp_path,
    field,
    value,
) -> None:
    kwargs = {
        "active_vault_root": tmp_path / "active",
        "recovery_root": tmp_path / "recovery",
        "library_asset_root": tmp_path / "assets",
        "source_candidates": [],
        "original_asset_candidates": [],
        "archived_document_count": 0,
        "recovery_point_count": 0,
        "backup_retention_count": 5,
    }
    kwargs[field] = value

    with pytest.raises(StorageGovernanceError):
        build_storage_governance_report(**kwargs)
