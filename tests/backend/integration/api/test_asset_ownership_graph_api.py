from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.aggregate_repository_factory import (
    AUTHORITY_DATABASE_NAME,
    SOURCE_ASSET_AUTHORITY_MEMBERS,
    STRUCTURED_DATABASE_NAME,
    TARGET_IDENTITY,
)
from core.storage_provider import (
    AggregateAuthorityEvidence,
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteSourceAssetMappingAdapter,
    SQLiteStructuredRecordStore,
)


CANARY = "ASSET_OWNERSHIP_PRIVATE_BODY_CANARY"


def _store(root: Path) -> JsonObjectStore:
    return JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library")


def _write(store: JsonObjectStore, collection: str, object_id: str, payload: dict) -> None:
    store.write(collection, object_id, {"id": object_id, **payload}, expected_revision=0)


def _activate_source_asset_authority(
    root: Path,
    *,
    members: tuple[str, ...] = SOURCE_ASSET_AUTHORITY_MEMBERS,
) -> None:
    evidence = AggregateAuthorityEvidence(
        migration_id="source-asset-api-v1",
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
    for member in members:
        initial = authority.create_json_active(
            namespace_id="default",
            aggregate=member,
            reason="api fixture",
        )
        staged = authority.transition(
            namespace_id="default",
            aggregate=member,
            expected_revision=initial.revision,
            to_state="sqlite_staged",
            evidence=evidence,
            reason="api fixture staged",
        )
        authority.transition(
            namespace_id="default",
            aggregate=member,
            expected_revision=staged.revision,
            to_state="sqlite_active",
            evidence=evidence,
            reason="api fixture active",
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


def test_asset_ownership_api_is_body_free_and_read_only(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    store = _store(root)
    media_root = root / "generated"
    audio = media_root / "source-1" / "track.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"RIFF")
    text_hash = hashlib.sha256(CANARY.encode()).hexdigest()
    _write(store, "sources", "source-1", {"type": "video"})
    _write(
        store,
        "video_audio_extractor_settings",
        "default",
        {"output_root": str(media_root)},
    )
    _write(
        store,
        "media_processing_jobs",
        "job-1",
        {"source_id": "source-1", "status": "completed"},
    )
    _write(
        store,
        "media_processing_outputs",
        "output-1",
        {
            "source_id": "source-1",
            "job_id": "job-1",
            "audio_asset_id": "audio-1",
            "status": "completed",
        },
    )
    _write(
        store,
        "audio_asset_refs",
        "audio-1",
        {
            "source_id": "source-1",
            "path": str(audio),
            "path_scope": "local_generated_audio_track",
            "size_bytes": 4,
            "status": "available",
        },
    )
    _write(
        store,
        "source_content_reads",
        "read-1",
        {
            "source_id": "source-1",
            "text": CANARY,
            "text_sha256": text_hash,
            "byte_count": len(CANARY.encode()),
            "status": "completed",
        },
    )
    with TestClient(create_app(SimpleNamespace(root_dir=root))) as client:
        before = sorted(
            (path.relative_to(root), path.read_bytes())
            for path in root.rglob("*")
            if path.is_file() and path.suffix not in {".sqlite3-shm", ".sqlite3-wal"}
        )
        response = client.get("/api/rebuild/retention/asset-ownership")
        after = sorted(
            (path.relative_to(root), path.read_bytes())
            for path in root.rglob("*")
            if path.is_file() and path.suffix not in {".sqlite3-shm", ".sqlite3-wal"}
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["complete"] is True
    assert payload["blocker_count"] == 0
    assert CANARY not in response.text
    assert str(root) not in response.text
    assert str(audio) not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert after == before


def test_asset_ownership_api_reports_fail_closed_blockers(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    store = _store(root)
    _write(store, "sources", "source-1", {"type": "file"})
    _write(
        store,
        "source_asset_links",
        "dangling",
        {
            "source_id": "source-1",
            "asset_id": "missing",
            "content_hash": "a" * 64,
        },
    )

    with TestClient(create_app(SimpleNamespace(root_dir=root))) as client:
        response = client.get("/api/rebuild/retention/asset-ownership")

    assert response.status_code == 200
    assert response.json()["complete"] is False
    assert "dangling_source_asset_link:dangling" in response.json()["blockers"]


def test_asset_ownership_api_reads_verified_sqlite_source_asset_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    store = _store(root)
    stale_digest = "c" * 64
    active_digest = "d" * 64
    active_asset_id = "active-asset"
    active_asset_ref = "crp-ref-default-assets-originals-active-asset"
    _write(store, "sources", "source-1", {"type": "file"})
    _write(
        store,
        "workbench_original_assets",
        "stale-asset",
        {
            "asset_ref": "crp-ref-default-assets-originals-stale-asset",
            "sha256": stale_digest,
            "byte_count": 5,
            "vault_ref": "assets/originals/cc/stale.bin",
        },
    )
    records = SQLiteStructuredRecordStore(
        root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    )
    SQLiteSourceAssetMappingAdapter(records).stage_legacy_mappings(
        assets=(
            {
                "id": active_asset_id,
                "asset_ref": active_asset_ref,
                "sha256": active_digest,
                "byte_count": 6,
                "vault_ref": "assets/originals/dd/active.bin",
                "metadata": {"source_kind": "file"},
            },
        ),
        links=(
            {
                "id": "active-link",
                "source_id": "source-1",
                "asset_id": active_asset_id,
                "asset_ref": active_asset_ref,
                "content_hash": active_digest,
                "role": "original",
                "provenance": "api-test",
            },
        ),
    )
    _activate_source_asset_authority(root)

    with TestClient(create_app(SimpleNamespace(root_dir=root))) as client:
        response = client.get("/api/rebuild/retention/asset-ownership")

    assert response.status_code == 200, response.text
    nodes = response.json()["nodes"]
    assert not any(node["object_id"] == "stale-asset" for node in nodes)
    assert any(
        node["authority"] == "sqlite.original_assets"
        and node["object_id"] == active_asset_id
        for node in nodes
    )
    assert any(
        node["authority"] == "sqlite.asset_blobs"
        and node["object_id"] == active_digest
        for node in nodes
    )


def test_asset_ownership_api_rejects_partially_active_source_asset_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    store = _store(root)
    _write(store, "sources", "source-1", {"type": "file"})
    _write(
        store,
        "workbench_original_assets",
        "legacy-asset",
        {
            "asset_ref": "crp-ref-default-assets-originals-legacy-asset",
            "sha256": "e" * 64,
            "byte_count": 5,
            "vault_ref": "assets/originals/ee/legacy.bin",
        },
    )
    _activate_source_asset_authority(
        root,
        members=(SOURCE_ASSET_AUTHORITY_MEMBERS[0],),
    )

    with TestClient(create_app(SimpleNamespace(root_dir=root))) as client:
        response = client.get("/api/rebuild/retention/asset-ownership")

    assert response.status_code == 409
    assert "partially SQLite active" in response.json()["detail"]
    assert "legacy-asset" not in response.text
