from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.product_core import (
    StoreWorkbenchOriginalAsset,
    WorkbenchOriginalAssetError,
    link_workbench_original_asset_to_source,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


class _RejectFirstAssetRecordWrite:
    def __init__(self, store: JsonObjectStore) -> None:
        self._store = store
        self._rejected = False

    def read(self, collection: str, object_id: str):
        return self._store.read(collection, object_id)

    def list(self, collection: str):
        return self._store.list(collection)

    def write(self, collection: str, object_id: str, payload, expected_revision):
        if collection == "workbench_original_assets" and not self._rejected:
            self._rejected = True
            raise RuntimeError("asset record persistence failed")
        return self._store.write(collection, object_id, payload, expected_revision)


def test_legacy_asset_bytes_can_remain_without_a_structured_record_when_record_write_fails(tmp_path: Path) -> None:
    store = _store(tmp_path)
    content = b"phase2 asset record failure"
    assets_root = tmp_path / "library" / "assets" / "originals"
    use_case = StoreWorkbenchOriginalAsset(
        object_store=_RejectFirstAssetRecordWrite(store),
        assets_root=assets_root,
    )

    with pytest.raises(RuntimeError, match="asset record persistence failed"):
        use_case.execute(
            display_name="phase2.txt",
            media_type="text/plain",
            size_bytes=len(content),
            content_base64=base64.b64encode(content).decode("ascii"),
        )

    digest = hashlib.sha256(content).hexdigest()
    asset_id = f"original-file-{digest[:16]}"
    assert (assets_root / digest[:2] / f"{asset_id}.txt").read_bytes() == content
    assert store.read("workbench_original_assets", asset_id) is None


def test_legacy_link_compensation_leaves_link_when_asset_reverse_index_update_fails(tmp_path: Path) -> None:
    store = _store(tmp_path)
    content = b"phase2 link failure"
    original = StoreWorkbenchOriginalAsset(
        object_store=store,
        assets_root=tmp_path / "library" / "assets" / "originals",
    ).execute(
        display_name="phase2.pdf",
        media_type="application/pdf",
        size_bytes=len(content),
        content_base64=base64.b64encode(content).decode("ascii"),
    )
    failing_store = _RejectFirstAssetRecordWrite(store)

    with pytest.raises(WorkbenchOriginalAssetError, match="could not be linked"):
        link_workbench_original_asset_to_source(
            object_store=failing_store,
            namespace_id="default",
            asset_ref=original.asset_ref,
            source_id="source-phase2-link",
            source_uri="crp://default/sources/source-phase2-link",
            now="2026-07-11T00:00:00Z",
        )

    links = store.list("source_asset_links")
    asset = store.read("workbench_original_assets", original.asset_id)
    assert len(links) == 1
    assert links[0]["source_id"] == "source-phase2-link"
    assert asset is not None
    assert asset["link_status"] == "orphaned"
    assert asset["orphan_reason"] == "source_asset_link_failed"
    assert asset.get("linked_source_ids") in (None, [])


def test_legacy_source_registration_deduplicates_same_capture_metadata_without_capture_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registrar = ObjectStoreSourceRegistrar(store)
    submission = SourceSubmission(
        kind="file",
        title="same capture",
        display_name="same.txt",
        media_type="text/plain",
        size_bytes=4,
        file_reference="crp-ref-default-assets-originals-same",
    )

    first = registrar.register(submission)
    repeated = registrar.register(submission)

    assert repeated["id"] == first["id"]
    assert len(store.list("sources")) == 1


def test_local_file_selection_records_the_same_occurrence_and_recording_time(
    tmp_path: Path,
) -> None:
    source = ObjectStoreSourceRegistrar(
        _store(tmp_path),
        created_at="2026-09-01T12:00:00+00:00",
    ).register(SourceSubmission(
        kind="file",
        title="local selection",
        display_name="notes.txt",
        media_type="text/plain",
        size_bytes=5,
        file_reference="crp-ref-default-assets-originals-notes",
    ))

    assert source["schema_version"] == "1.1.0"
    assert source["occurred_at"] == "2026-09-01T12:00:00+00:00"
    assert source["recorded_at"] == "2026-09-01T12:00:00+00:00"


def test_same_bytes_with_different_suffixes_reuse_one_managed_blob(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assets_root = tmp_path / "library" / "assets" / "originals"
    use_case = StoreWorkbenchOriginalAsset(object_store=store, assets_root=assets_root)
    content = b"same bytes different names"

    text_asset = use_case.execute(
        display_name="same.txt",
        media_type="text/plain",
        size_bytes=len(content),
        content_base64=base64.b64encode(content).decode("ascii"),
    )
    pdf_asset = use_case.execute(
        display_name="same.pdf",
        media_type="application/pdf",
        size_bytes=len(content),
        content_base64=base64.b64encode(content).decode("ascii"),
    )

    assert pdf_asset.asset_id == text_asset.asset_id
    assert pdf_asset.asset_ref == text_asset.asset_ref
    assert pdf_asset.vault_ref == text_asset.vault_ref
    assert len(tuple(assets_root.rglob(f"{text_asset.asset_id}.*"))) == 1
    stored = store.read("workbench_original_assets", text_asset.asset_id)
    assert stored is not None
    assert stored["vault_ref"] == text_asset.vault_ref
