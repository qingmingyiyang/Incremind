from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.routes.product import (
    recovery_points as product_recovery_points,
    repositories as product_repositories,
)


def _write(path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PRIVATE-STORAGE-CANARY" + b"x" * size)


def test_storage_governance_returns_body_free_actual_usage(
    tmp_path,
    monkeypatch,
) -> None:
    class Store:
        def list_including_deleted(self, collection):
            assert collection == "sources"
            return [{
                "id": "source-deleted",
                "title": "PRIVATE-STORAGE-CANARY",
                "library_lifecycle": {
                    "status": "deleted",
                    "deleted_at": "2020-01-01T00:00:00Z",
                    "undo_expires_at": "2020-01-08T00:00:00Z",
                },
            }]

        def list(self, collection):
            assert collection == "workbench_original_assets"
            return [{
                "id": "asset-orphan",
                "display_name": "PRIVATE-STORAGE-CANARY",
                "byte_count": 23,
                "link_status": "orphaned",
                "orphaned_at": "2020-01-01T00:00:00Z",
            }]

        def revision(self, collection, object_id):
            del collection, object_id
            return 4

    class Documents:
        def list(self, *, include_archived):
            assert include_archived is True
            return [
                {"id": "document-a", "status": "archived"},
                {"id": "document-b", "status": "active"},
            ]

    _write(tmp_path / ".rebuild-data" / "runtime.bin", 5)
    _write(
        tmp_path
        / "..rebuild-data-recovery"
        / "snapshots"
        / "snap-a"
        / "payload.bin",
        7,
    )
    _write(
        tmp_path / "library" / "assets" / "originals" / "asset.bin",
        11,
    )
    monkeypatch.setattr(
        product_repositories,
        "_object_store",
        lambda _root: (
            Store(),
            SimpleNamespace(
                namespace_id="default",
                backup_retention_count=5,
            ),
        ),
    )
    monkeypatch.setattr(
        product_repositories,
        "_document_repository",
        lambda *_args, **_kwargs: Documents(),
    )
    monkeypatch.setattr(
        product_recovery_points,
        "_external_recovery_point_records",
        lambda _root: ({"id": "snap-a"},),
    )
    client = TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))

    response = client.get(
        "/api/rebuild/retention/storage-governance"
    )

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["writes_performed"] is False
    assert body["network_called"] is False
    assert body["content_included"] is False
    assert body["paths_included"] is False
    assert body["known_reclaimable_bytes"] == 23
    assert body["lifecycle_summary"] == {
        "source_trash_count": 1,
        "source_purge_ready_count": 1,
        "original_asset_candidate_count": 1,
        "original_asset_reclaimable_bytes": 23,
        "archived_document_count": 1,
        "recovery_point_count": 1,
        "backup_retention_count": 5,
    }
    serialized = json.dumps(body, ensure_ascii=False)
    assert "PRIVATE-STORAGE-CANARY" not in serialized
    assert str(tmp_path) not in serialized
    assert body["cleanup_capabilities"]["source"][
        "requires_confirmation"
    ] is True
    assert body["cleanup_capabilities"]["document"][
        "physical_cleanup"
    ] is False
