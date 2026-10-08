from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import core.product_core.asset_ownership_graph as ownership_graph_module
from core.product_core import (
    BuildAssetOwnershipGraph,
    serialize_asset_ownership_graph,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / "objects", namespace_id="default")


def _write(store: JsonObjectStore, collection: str, object_id: str, payload: dict) -> None:
    store.write(collection, object_id, {"id": object_id, **payload}, expected_revision=0)


def test_graph_classifies_original_derivative_body_and_external_reference(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    audio_root = tmp_path / "generated-audio"
    audio_path = audio_root / "source-video" / "track.wav"
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"RIFF-test")
    body = "网页正文"
    body_bytes = body.encode()
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    original_hash = "a" * 64

    _write(
        store,
        "sources",
        "source-video",
        {"type": "video", "original_url": "https://private.invalid/video"},
    )
    _write(store, "sources", "source-web", {"type": "link"})
    _write(
        store,
        "workbench_original_assets",
        "asset-1",
        {
            "sha256": original_hash,
            "byte_count": 10,
            "vault_ref": "assets/originals/aa/file.bin",
            "link_status": "linked",
        },
    )
    _write(
        store,
        "source_asset_links",
        "link-1",
        {
            "source_id": "source-video",
            "asset_id": "asset-1",
            "content_hash": original_hash,
        },
    )
    _write(
        store,
        "media_processing_jobs",
        "job-1",
        {"source_id": "source-video", "status": "completed"},
    )
    _write(
        store,
        "media_processing_outputs",
        "output-1",
        {
            "job_id": "job-1",
            "source_id": "source-video",
            "audio_asset_id": "audio-1",
            "status": "completed",
        },
    )
    _write(
        store,
        "audio_asset_refs",
        "audio-1",
        {
            "source_id": "source-video",
            "path": str(audio_path),
            "path_scope": "local_generated_audio_track",
            "size_bytes": audio_path.stat().st_size,
            "status": "available",
        },
    )
    _write(
        store,
        "source_content_reads",
        "read-1",
        {
            "source_id": "source-web",
            "text": body,
            "text_sha256": body_hash,
            "byte_count": len(body_bytes),
            "status": "completed",
        },
    )

    graph = BuildAssetOwnershipGraph(
        store, allowed_media_roots=(audio_root,)
    ).execute()

    assert graph.complete is True
    classes = {node.object_id: node.storage_class for node in graph.nodes}
    assert classes["asset-1"] == "owned_original_bytes"
    blob = next(
        node for node in graph.nodes if node.authority == "original_blob_store"
    )
    assert blob.object_id == original_hash
    assert blob.owned_bytes is True
    assert classes["audio-1"] == "owned_media_derivative"
    assert classes["read-1"] == "structured_body"
    assert classes["source-video:external-url"] == "external_reference"
    payload = serialize_asset_ownership_graph(graph)
    serialized = str(payload)
    assert body not in serialized
    assert "private.invalid" not in serialized
    assert str(audio_path) not in serialized


def test_graph_fails_closed_for_dangling_and_unscoped_media(tmp_path: Path) -> None:
    store = _store(tmp_path)
    audio_path = tmp_path / "outside.wav"
    audio_path.write_bytes(b"wav")
    _write(store, "sources", "source-1", {"type": "audio"})
    _write(
        store,
        "source_asset_links",
        "dangling",
        {
            "source_id": "source-1",
            "asset_id": "missing",
            "content_hash": "b" * 64,
        },
    )
    _write(
        store,
        "audio_asset_refs",
        "audio-1",
        {
            "source_id": "source-1",
            "path": str(audio_path),
            "path_scope": "unknown",
            "size_bytes": 3,
            "status": "available",
        },
    )

    graph = BuildAssetOwnershipGraph(store).execute()

    assert graph.complete is False
    assert "dangling_source_asset_link:dangling" in graph.blockers
    audio = next(node for node in graph.nodes if node.object_id == "audio-1")
    assert "media_path_scope_unverified" in audio.blockers
    assert "media_storage_root_unconfigured" in audio.blockers


def test_graph_keeps_deleted_source_identity_for_retention_diagnostics(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    audio_root = tmp_path / "generated-audio"
    audio_path = audio_root / "source-deleted" / "track.wav"
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"RIFF-deleted")
    _write(
        store,
        "sources",
        "source-deleted",
        {
            "type": "audio",
            "library_lifecycle": {
                "status": "deleted",
                "deleted_at": "2026-07-01T00:00:00Z",
            },
        },
    )
    _write(
        store,
        "media_processing_jobs",
        "job-deleted",
        {"source_id": "source-deleted", "status": "completed"},
    )
    _write(
        store,
        "media_processing_outputs",
        "output-deleted",
        {
            "job_id": "job-deleted",
            "source_id": "source-deleted",
            "audio_asset_id": "audio-deleted",
            "status": "completed",
        },
    )
    _write(
        store,
        "audio_asset_refs",
        "audio-deleted",
        {
            "source_id": "source-deleted",
            "path": str(audio_path),
            "path_scope": "local_generated_audio_track",
            "size_bytes": audio_path.stat().st_size,
            "status": "available",
        },
    )

    graph = BuildAssetOwnershipGraph(
        store, allowed_media_roots=(audio_root,)
    ).execute()

    assert graph.complete is True
    source = next(
        node
        for node in graph.nodes
        if node.authority == "sources" and node.object_id == "source-deleted"
    )
    assert source.status == "deleted"
    assert any(
        edge.source_node_id == source.node_id
        and edge.relation == "produces_rebuildable_derivative"
        for edge in graph.edges
    )


def test_graph_fails_closed_for_media_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    audio_root = tmp_path / "generated-audio"
    audio_root.mkdir()
    target = audio_root / "target.wav"
    target.write_bytes(b"RIFF-target")
    linked = audio_root / "linked.wav"
    linked.write_bytes(target.read_bytes())
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda self: (
            self == linked
            or original_is_symlink(self)
        ),
    )
    _write(store, "sources", "source-linked", {"type": "audio"})
    _write(
        store,
        "media_processing_jobs",
        "job-linked",
        {"source_id": "source-linked", "status": "completed"},
    )
    _write(
        store,
        "media_processing_outputs",
        "output-linked",
        {
            "job_id": "job-linked",
            "source_id": "source-linked",
            "audio_asset_id": "audio-linked",
            "status": "completed",
        },
    )
    _write(
        store,
        "audio_asset_refs",
        "audio-linked",
        {
            "source_id": "source-linked",
            "path": str(linked),
            "path_scope": "local_generated_audio_track",
            "size_bytes": target.stat().st_size,
            "status": "available",
        },
    )

    graph = BuildAssetOwnershipGraph(
        store, allowed_media_roots=(audio_root,)
    ).execute()

    assert graph.complete is False
    audio = next(node for node in graph.nodes if node.object_id == "audio-linked")
    assert "media_path_linked_or_relative" in audio.blockers


def test_graph_fails_closed_for_reparse_media_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    audio_root = tmp_path / "generated-audio"
    audio_root.mkdir()
    audio_path = audio_root / "reparse.wav"
    audio_path.write_bytes(b"RIFF-reparse")
    _write(store, "sources", "source-reparse", {"type": "audio"})
    _write(
        store,
        "media_processing_jobs",
        "job-reparse",
        {"source_id": "source-reparse", "status": "completed"},
    )
    _write(
        store,
        "media_processing_outputs",
        "output-reparse",
        {
            "job_id": "job-reparse",
            "source_id": "source-reparse",
            "audio_asset_id": "audio-reparse",
            "status": "completed",
        },
    )
    _write(
        store,
        "audio_asset_refs",
        "audio-reparse",
        {
            "source_id": "source-reparse",
            "path": str(audio_path),
            "path_scope": "local_generated_audio_track",
            "size_bytes": audio_path.stat().st_size,
            "status": "available",
        },
    )
    monkeypatch.setattr(
        ownership_graph_module,
        "_is_reparse",
        lambda path: path == audio_path,
    )

    graph = BuildAssetOwnershipGraph(
        store, allowed_media_roots=(audio_root,)
    ).execute()

    assert graph.complete is False
    audio = next(node for node in graph.nodes if node.object_id == "audio-reparse")
    assert "media_path_linked_or_relative" in audio.blockers


def test_graph_marks_sqlite_mapping_as_non_authoritative_staging(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    digest = "c" * 64
    graph = BuildAssetOwnershipGraph(store).execute(
        sqlite_blobs=({"id": digest, "sha256": digest},),
        sqlite_assets=(
            {
                "id": "staged-asset",
                "blob_sha256": digest,
                "byte_count": 8,
            },
        ),
        sqlite_links=({"id": "staged-link", "asset_id": "staged-asset"},),
    )

    assert graph.complete is False
    blob = next(node for node in graph.nodes if node.authority == "sqlite.asset_blobs")
    assert blob.storage_class == "migration_staging"
    assert blob.owned_bytes is False
    assert "migration_staging_not_production_authority" in blob.blockers
    edge = next(
        edge for edge in graph.edges if edge.relation == "maps_to_content_addressed_blob"
    )
    assert edge.authoritative is False


def test_graph_detects_content_hash_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(store, "sources", "source-1", {"type": "file"})
    _write(
        store,
        "workbench_original_assets",
        "asset-1",
        {
            "sha256": "d" * 64,
            "byte_count": 1,
            "vault_ref": "assets/originals/dd/file",
        },
    )
    _write(
        store,
        "source_asset_links",
        "link-1",
        {
            "source_id": "source-1",
            "asset_id": "asset-1",
            "content_hash": "e" * 64,
        },
    )
    _write(
        store,
        "source_content_reads",
        "read-1",
        {
            "source_id": "source-1",
            "text": "actual",
            "text_sha256": "f" * 64,
            "byte_count": 6,
        },
    )

    graph = BuildAssetOwnershipGraph(store).execute()

    assert "source_asset_hash_conflict:link-1" in graph.blockers
    read = next(node for node in graph.nodes if node.object_id == "read-1")
    assert "content_read_hash_conflict" in read.blockers


def test_graph_aggregates_shared_original_bytes_by_sha(tmp_path: Path) -> None:
    store = _store(tmp_path)
    digest = "1" * 64
    for index in (1, 2):
        source_id = f"source-{index}"
        asset_id = f"asset-{index}"
        _write(store, "sources", source_id, {"type": "file"})
        _write(
            store,
            "workbench_original_assets",
            asset_id,
            {
                "sha256": digest,
                "byte_count": 4,
                "vault_ref": "assets/originals/11/shared.bin",
            },
        )
        _write(
            store,
            "source_asset_links",
            f"link-{index}",
            {
                "source_id": source_id,
                "asset_id": asset_id,
                "content_hash": digest,
            },
        )

    graph = BuildAssetOwnershipGraph(store).execute()

    blobs = [
        node for node in graph.nodes if node.authority == "original_blob_store"
    ]
    assert len(blobs) == 1
    byte_edges = [
        edge for edge in graph.edges if edge.relation == "maps_to_owned_bytes"
    ]
    assert len(byte_edges) == 2


def test_graph_uses_injected_source_asset_authority_instead_of_legacy_json(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    stale_digest = "2" * 64
    active_digest = "3" * 64
    _write(store, "sources", "source-1", {"type": "file"})
    _write(
        store,
        "workbench_original_assets",
        "stale-asset",
        {
            "sha256": stale_digest,
            "byte_count": 5,
            "vault_ref": "assets/originals/22/stale.bin",
        },
    )

    graph = BuildAssetOwnershipGraph(
        store,
        source_asset_records=(
            {
                "id": "active-asset",
                "asset_ref": "crp-ref-default-assets-originals-active-asset",
                "sha256": active_digest,
                "byte_count": 6,
                "vault_ref": f"assets/blobs/33/{active_digest}",
                "metadata": {},
                "link_status": "linked",
            },
        ),
        source_asset_link_records=(
            {
                "id": "active-link",
                "source_id": "source-1",
                "asset_id": "active-asset",
                "asset_ref": "crp-ref-default-assets-originals-active-asset",
                "content_hash": active_digest,
                "role": "original",
                "provenance": "sqlite-cutover-test",
            },
        ),
        source_asset_authority="sqlite.original_assets",
        source_blob_authority="sqlite.asset_blobs",
    ).execute()

    assert not any(node.object_id == "stale-asset" for node in graph.nodes)
    asset = next(node for node in graph.nodes if node.object_id == "active-asset")
    blob = next(node for node in graph.nodes if node.object_id == active_digest)
    assert asset.authority == "sqlite.original_assets"
    assert blob.authority == "sqlite.asset_blobs"
    assert blob.storage_class == "owned_original_bytes"
    assert blob.blockers == ()
