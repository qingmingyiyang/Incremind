from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.aggregate_repository_factory import (
    AUTHORITY_DATABASE_NAME,
    SOURCE_ASSET_AUTHORITY_MEMBERS,
    STRUCTURED_DATABASE_NAME,
    TARGET_IDENTITY,
)
from core.product_core import (
    StoreWorkbenchOriginalAsset,
    WorkbenchOriginalAssetError,
    link_workbench_original_asset_to_source,
)
from core.storage_provider import (
    JsonObjectStore,
    AggregateAuthorityEvidence,
    SQLiteAggregateAuthorityStore,
    SQLiteStructuredRecordStore,
    SourceAssetRuntimeStore,
)


def _sqlite_runtime(tmp_path) -> tuple[
    SourceAssetRuntimeStore,
    JsonObjectStore,
    SQLiteStructuredRecordStore,
]:
    json_store = JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
    )
    records = SQLiteStructuredRecordStore(
        tmp_path / ".rebuild-data" / "structured.sqlite3"
    )
    return (
        SourceAssetRuntimeStore(
            json_store=json_store,
            sqlite_records=records,
            library_root=tmp_path / "library",
            authority_identity="sqlite:structured-records-v1",
        ),
        json_store,
        records,
    )


def _activate_empty_source_asset_authority(root) -> SQLiteStructuredRecordStore:
    evidence = AggregateAuthorityEvidence(
        migration_id="write-cutover-api-v1",
        source_fingerprint="a" * 64,
        target_fingerprint="b" * 64,
        target_identity=TARGET_IDENTITY,
    )
    authority = SQLiteAggregateAuthorityStore(
        root / ".rebuild-data" / AUTHORITY_DATABASE_NAME
    )
    records = SQLiteStructuredRecordStore(
        root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    )
    for member in SOURCE_ASSET_AUTHORITY_MEMBERS:
        initial = authority.create_json_active(
            namespace_id="default",
            aggregate=member,
            reason="write cutover fixture",
        )
        staged = authority.transition(
            namespace_id="default",
            aggregate=member,
            expected_revision=initial.revision,
            to_state="sqlite_staged",
            evidence=evidence,
            reason="write cutover fixture",
        )
        authority.transition(
            namespace_id="default",
            aggregate=member,
            expected_revision=staged.revision,
            to_state="sqlite_active",
            evidence=evidence,
            reason="write cutover fixture",
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
    return records


def _store_asset(runtime, tmp_path, *, source_kind: str = "file"):
    content = b"write cutover shared bytes"
    result = StoreWorkbenchOriginalAsset(
        object_store=runtime,
        assets_root=tmp_path / "library" / "assets" / "originals",
    ).execute(
        display_name=f"{source_kind}.txt",
        media_type="text/plain",
        size_bytes=len(content),
        content_base64=base64.b64encode(content).decode("ascii"),
        source_kind=source_kind,
    )
    return content, result


def test_sqlite_active_upload_writes_only_content_addressed_authority(tmp_path) -> None:
    runtime, json_store, records = _sqlite_runtime(tmp_path)

    content, result = _store_asset(runtime, tmp_path)
    digest = hashlib.sha256(content).hexdigest()

    assert result.vault_ref == f"assets/blobs/{digest[:2]}/{digest}"
    assert (tmp_path / "library" / result.vault_ref).read_bytes() == content
    assert json_store.list("workbench_original_assets") == ()
    assert json_store.list("source_asset_links") == ()
    assert len(records.list("asset_blobs")) == 1
    assert len(records.list("original_assets")) == 1
    stored = runtime.read("workbench_original_assets", result.asset_id)
    assert stored is not None
    assert stored["vault_ref"] == result.vault_ref
    assert stored["sha256"] == digest


def test_sqlite_active_link_updates_asset_and_link_in_one_uow(tmp_path) -> None:
    runtime, json_store, records = _sqlite_runtime(tmp_path)
    _content, result = _store_asset(runtime, tmp_path)

    linked = link_workbench_original_asset_to_source(
        object_store=runtime,
        namespace_id="default",
        asset_ref=result.asset_ref,
        source_id="source-cutover",
        source_uri="crp://default/sources/source-cutover",
        now="2026-07-29T00:00:00Z",
    )

    asset = runtime.read("workbench_original_assets", result.asset_id)
    assert linked.source_id == "source-cutover"
    assert asset is not None
    assert asset["link_status"] == "linked"
    assert asset["linked_source_ids"] == ["source-cutover"]
    assert len(records.list("source_asset_links")) == 1
    assert json_store.list("source_asset_links") == ()


def test_sqlite_active_conflicting_link_rolls_back_asset_projection(tmp_path) -> None:
    runtime, _json_store, _records = _sqlite_runtime(tmp_path)
    _content, result = _store_asset(runtime, tmp_path)
    link_id = f"source-asset-source-cutover-{result.asset_id}"
    runtime.write(
        "source_asset_links",
        link_id,
        {
            "id": link_id,
            "source_id": "different-source",
            "source_uri": "crp://default/sources/different-source",
            "asset_id": result.asset_id,
            "asset_ref": result.asset_ref,
            "content_hash": result.sha256,
            "size_bytes": result.byte_count,
            "role": "original",
            "created_at": "2026-07-29T00:00:00Z",
            "provenance": "test",
        },
        expected_revision=0,
    )

    with pytest.raises(WorkbenchOriginalAssetError, match="could not be linked"):
        link_workbench_original_asset_to_source(
            object_store=runtime,
            namespace_id="default",
            asset_ref=result.asset_ref,
            source_id="source-cutover",
            source_uri="crp://default/sources/source-cutover",
            now="2026-07-29T00:00:00Z",
        )

    asset = runtime.read("workbench_original_assets", result.asset_id)
    assert asset is not None
    assert asset["link_status"] == "pending"
    assert asset.get("linked_source_ids") in (None, [])


def test_sqlite_active_shared_blob_mapping_survives_first_asset_delete(tmp_path) -> None:
    runtime, _json_store, records = _sqlite_runtime(tmp_path)
    _content, first = _store_asset(runtime, tmp_path, source_kind="file")
    _content, second = _store_asset(runtime, tmp_path, source_kind="image")

    assert first.asset_id != second.asset_id
    assert first.vault_ref == second.vault_ref
    assert len(records.list("asset_blobs")) == 1

    assert runtime.delete("workbench_original_assets", first.asset_id) is True
    assert len(records.list("asset_blobs")) == 1
    assert runtime.delete("workbench_original_assets", second.asset_id) is True
    assert records.list("asset_blobs") == ()


def test_json_active_runtime_preserves_legacy_layout(tmp_path) -> None:
    json_store = JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
    )
    runtime = SourceAssetRuntimeStore(
        json_store=json_store,
        sqlite_records=None,
        library_root=tmp_path / "library",
        authority_identity="json:object-store-v1",
    )

    _content, result = _store_asset(runtime, tmp_path)

    assert result.vault_ref.startswith("assets/originals/")
    assert json_store.read("workbench_original_assets", result.asset_id) is not None


def test_real_api_upload_uses_fixed_sqlite_authority_without_json_mixed_write(
    tmp_path,
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    records = _activate_empty_source_asset_authority(root)
    content = b"real api write cutover"

    with TestClient(create_app(SimpleNamespace(root_dir=root))) as client:
        response = client.post(
            "/api/rebuild/workbench/original-asset",
            json={
                "display_name": "cutover.txt",
                "media_type": "text/plain",
                "size_bytes": len(content),
                "content_base64": base64.b64encode(content).decode("ascii"),
                "source_kind": "file",
            },
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["vault_ref"].startswith("assets/blobs/")
    assert (root / "library" / payload["vault_ref"]).read_bytes() == content
    json_store = JsonObjectStore(
        root / ".rebuild-data",
        legacy_root=root / "library",
    )
    assert json_store.list("workbench_original_assets") == ()
    assert json_store.list("source_asset_links") == ()
    assert len(records.list("original_assets")) == 1
    assert len(records.list("asset_blobs")) == 1
