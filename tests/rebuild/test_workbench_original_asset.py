import base64
import hashlib

import pytest

from core.product_core import (
    OrchestrateWorkbenchAutoIntake,
    ResolveWorkbenchOriginalAsset,
    StoreWorkbenchOriginalAsset,
    WorkbenchOriginalAssetError,
    serialize_workbench_original_asset,
)
from core.ingestion_core import ObjectStoreSourceRegistrar
from core.job_runner import ObjectStoreJobRepository
from core.storage_provider import JsonObjectStore


def test_workbench_original_asset_stores_uploaded_bytes_hash_and_metadata(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    content = b"fixture"
    result = StoreWorkbenchOriginalAsset(
        object_store=store,
        assets_root=tmp_path / "library" / "assets" / "originals",
    ).execute(
        display_name="round7-note.pdf",
        media_type="application/pdf",
        size_bytes=len(content),
        content_base64=base64.b64encode(content).decode("ascii"),
        source_kind="file",
    )

    expected_hash = hashlib.sha256(content).hexdigest()
    assert result.status == "stored"
    assert result.asset_id == f"original-file-{expected_hash[:16]}"
    assert result.asset_ref == f"crp-ref-default-assets-originals-{result.asset_id}"
    assert result.sha256 == expected_hash
    assert result.byte_count == len(content)
    assert result.storage_mode == "stored_original"
    assert result.availability == "available"
    assert result.metadata["content_hash_basis"] == "uploaded_original_bytes_sha256"
    assert (tmp_path / "library" / "assets" / "originals" / expected_hash[:2] / f"{result.asset_id}.pdf").read_bytes() == content

    stored = store.read("workbench_original_assets", result.asset_id)
    assert stored is not None
    assert stored["asset_ref"] == result.asset_ref
    assert stored["vault_ref"] == result.vault_ref
    assert "path" not in stored
    assert stored["link_status"] == "pending"
    assert stored["orphan_reason"] == "awaiting_source_capture"
    assert stored["sha256"] == expected_hash

    payload = serialize_workbench_original_asset(result)
    assert payload["asset_ref"] == result.asset_ref
    assert payload["vault_ref"] == result.vault_ref
    assert "path" not in payload
    assert payload["metadata"]["source_kind"] == "file"


def test_repeated_browser_upload_preserves_existing_source_links(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    content = b"same managed original"
    use_case = StoreWorkbenchOriginalAsset(
        object_store=store,
        assets_root=tmp_path / "library" / "assets" / "originals",
    )
    first = use_case.execute(
        display_name="same.txt",
        media_type="text/plain",
        size_bytes=len(content),
        content_base64=base64.b64encode(content).decode("ascii"),
    )
    linked = dict(store.read("workbench_original_assets", first.asset_id))
    linked.update({
        "link_status": "linked",
        "orphan_reason": None,
        "linked_source_ids": ["source-file-existing"],
    })
    store.write("workbench_original_assets", first.asset_id, linked, expected_revision=None)

    repeated = use_case.execute(
        display_name="same.txt",
        media_type="text/plain",
        size_bytes=len(content),
        content_base64=base64.b64encode(content).decode("ascii"),
    )

    assert repeated.asset_id == first.asset_id
    stored = store.read("workbench_original_assets", first.asset_id)
    assert stored["link_status"] == "linked"
    assert stored["orphan_reason"] is None
    assert stored["linked_source_ids"] == ["source-file-existing"]


def test_auto_intake_links_original_asset_to_source_without_path_leak(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    content = b"linked fixture"
    original = StoreWorkbenchOriginalAsset(
        object_store=store,
        assets_root=tmp_path / "library" / "assets" / "originals",
    ).execute(
        display_name="linked.pdf",
        media_type="application/pdf",
        size_bytes=len(content),
        content_base64=base64.b64encode(content).decode("ascii"),
    )
    orchestrator = OrchestrateWorkbenchAutoIntake(
        object_store=store,
        source_registrar=ObjectStoreSourceRegistrar(store),
        job_repository=ObjectStoreJobRepository(store),
        fetch_url=lambda _url: "",
    )

    result = orchestrator.execute(
        media_type="application/pdf",
        file_name="linked.pdf",
        title="linked.pdf",
        original_asset_ref=original.asset_ref,
    )

    assert result.status == "accepted"
    source_id = result.items[0].source_id
    source = store.read("sources", source_id)
    assert source is not None
    assert source["metadata"]["file_reference"] == original.asset_ref
    links = store.list("source_asset_links")
    assert len(links) == 1
    assert links[0]["source_id"] == source_id
    assert links[0]["asset_id"] == original.asset_id
    stored = store.read("workbench_original_assets", original.asset_id)
    assert stored is not None
    assert stored["linked_source_ids"] == [source_id]
    assert stored["link_status"] == "linked"
    assert not any(str(tmp_path) in str(value) for value in (links[0], stored))


def test_workbench_original_asset_rejects_invalid_base64_and_size_mismatch(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    use_case = StoreWorkbenchOriginalAsset(
        object_store=store,
        assets_root=tmp_path / "library" / "assets" / "originals",
    )

    with pytest.raises(WorkbenchOriginalAssetError, match="valid base64"):
        use_case.execute(
            display_name="bad.bin",
            media_type="application/octet-stream",
            size_bytes=1,
            content_base64="not-base64",
        )

    with pytest.raises(WorkbenchOriginalAssetError, match="size"):
        use_case.execute(
            display_name="bad.bin",
            media_type="application/octet-stream",
            size_bytes=99,
            content_base64=base64.b64encode(b"fixture").decode("ascii"),
        )


def test_streamed_original_asset_fails_closed_when_existing_bytes_drift(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    assets_root = tmp_path / "library" / "assets" / "originals"
    content = b"verified-stream"
    sha256 = hashlib.sha256(content).hexdigest()
    asset_id = f"original-file-{sha256[:16]}"
    existing = assets_root / sha256[:2] / f"{asset_id}.bin"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"corrupt-stream")
    staged = tmp_path / "file-grant-staged.part"
    staged.write_bytes(content)

    with pytest.raises(WorkbenchOriginalAssetError, match="existing original asset bytes"):
        StoreWorkbenchOriginalAsset(object_store=store, assets_root=assets_root).execute_staged_file(
            display_name="fixture.bin",
            media_type="application/octet-stream",
            size_bytes=len(content),
            sha256=sha256,
            staged_path=staged,
        )

    assert existing.read_bytes() == b"corrupt-stream"
    assert staged.read_bytes() == content
    assert store.read("workbench_original_assets", asset_id) is None


def test_original_asset_availability_rechecks_bytes_and_link(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    content = b"availability-fixture"
    original = StoreWorkbenchOriginalAsset(
        object_store=store,
        assets_root=tmp_path / "library" / "assets" / "originals",
    ).execute(
        display_name="proof.txt",
        media_type="text/plain",
        size_bytes=len(content),
        content_base64=base64.b64encode(content).decode("ascii"),
    )
    store.write("source_asset_links", "link-1", {
        "id": "link-1", "source_id": "source-1", "asset_id": original.asset_id, "role": "original",
    }, expected_revision=0)
    resolver = ResolveWorkbenchOriginalAsset(object_store=store, library_root=tmp_path / "library")

    available = resolver.for_source("source-1")
    assert available is not None
    assert available.status == "available"
    assert available.path is not None

    available.path.write_bytes(b"same-size-corruption!!")
    assert resolver.for_source("source-1").status == "drifted"  # type: ignore[union-attr]
    available.path.unlink()
    missing = resolver.for_source("source-1")
    assert missing is not None
    assert (missing.status, missing.reason) == ("missing", "original_file_missing")


def test_original_asset_availability_rejects_escape_and_unlinked_asset(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    store.write("workbench_original_assets", "asset-escape", {
        "id": "asset-escape", "vault_ref": "../outside.txt", "byte_count": outside.stat().st_size,
        "sha256": digest, "display_name": "outside.txt", "media_type": "text/plain",
    }, expected_revision=0)
    resolver = ResolveWorkbenchOriginalAsset(object_store=store, library_root=tmp_path / "library")

    unlinked = resolver.for_asset("asset-escape")
    assert (unlinked.status, unlinked.reason) == ("unavailable", "source_asset_link_missing")
    store.write("source_asset_links", "link-escape", {
        "id": "link-escape", "source_id": "source-escape", "asset_id": "asset-escape", "role": "original",
    }, expected_revision=0)
    escaped = resolver.for_asset("asset-escape")
    assert (escaped.status, escaped.reason) == ("unavailable", "vault_ref_invalid")
    assert outside.read_text(encoding="utf-8") == "private"
