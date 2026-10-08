from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore


def _client(root: Path) -> TestClient:
    root.mkdir(parents=True, exist_ok=True)
    return TestClient(create_app(SimpleNamespace(root_dir=root)))


def _store(root: Path) -> JsonObjectStore:
    return JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library")


def _expired_source(root: Path, suffix: str = "api") -> tuple[JsonObjectStore, str]:
    store = _store(root)
    source_id = f"source-retention-{suffix}"
    store.write(
        "sources",
        source_id,
        {
            "schema_version": "1.0.0",
            "id": source_id,
            "kind": "text",
            "title": "只应存在于安全备份的原始正文",
            "content": "RETENTION_PRIVATE_CANARY_不应进入计划或回执",
            "library_lifecycle": {
                "status": "deleted",
                "operation_id": f"library-delete-{suffix}",
                "deleted_at": "2026-07-01T00:00:00Z",
                "undo_expires_at": "2026-07-08T00:00:00Z",
                "restored_at": None,
            },
        },
        expected_revision=0,
    )
    store.write(
        "source_content_reads",
        f"read-{suffix}",
        {
            "schema_version": "1.0.0",
            "id": f"read-{suffix}",
            "source_id": source_id,
            "status": "completed",
            "byte_count": 52,
        },
        expected_revision=0,
    )
    return store, source_id


def test_source_purge_plan_executes_with_backup_and_survives_restart(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    store, source_id = _expired_source(root)

    with _client(root) as client:
        listed = client.get("/api/rebuild/retention/source-purge/candidates")
        assert listed.status_code == 200
        assert listed.json() == {
            "items": [
                {
                    "source_id": source_id,
                    "title": "只应存在于安全备份的原始正文",
                    "revision": 1,
                    "deleted_at": "2026-07-01T00:00:00Z",
                    "undo_expires_at": "2026-07-08T00:00:00Z",
                    "retention_elapsed": True,
                }
            ]
        }
        assert "RETENTION_PRIVATE_CANARY" not in listed.text
        assert str(root) not in listed.text
        planned = client.post(
            "/api/rebuild/retention/source-purge/plan",
            json={"source_id": source_id},
        )
        assert planned.status_code == 200, planned.text
        plan = planned.json()
        assert plan["purge_supported"] is True
        assert plan["snapshot_id"].startswith("snap-retention-")
        assert plan["items"] == [
            {
                "aggregate_type": "source",
                "object_id": source_id,
                "authority": "json_object_store",
                "revision": 1,
                "eligible": True,
                "eligible_after": "2026-07-08T00:00:00Z",
                "blockers": [],
                "owned_records": [
                    {
                        "authority": "json_object_store",
                        "collection": "source_content_reads",
                        "object_id": "read-api",
                        "revision": 1,
                    },
                    {
                        "authority": "json_object_store",
                        "collection": "sources",
                        "object_id": source_id,
                        "revision": 1,
                    },
                ],
                "inbound_references": [],
            }
        ]
        assert "RETENTION_PRIVATE_CANARY" not in planned.text
        assert str(root) not in planned.text

        unconfirmed = client.post(
            "/api/rebuild/retention/source-purge",
            json={
                "source_id": source_id,
                "plan_id": plan["plan_id"],
                "expected_revision": 1,
                "confirm": False,
            },
        )
        assert unconfirmed.status_code == 409
        assert store.read_including_deleted("sources", source_id) is not None

        executed = client.post(
            "/api/rebuild/retention/source-purge",
            json={
                "source_id": source_id,
                "plan_id": plan["plan_id"],
                "expected_revision": 1,
                "confirm": True,
            },
        )
        assert executed.status_code == 200, executed.text
        result = executed.json()
        assert result["status"] == "completed"
        assert result["deleted_count"] == 2
        assert result["idempotent"] is False
        assert "RETENTION_PRIVATE_CANARY" not in executed.text

    with _client(root) as restarted:
        replayed = restarted.post(
            "/api/rebuild/retention/source-purge",
            json={
                "source_id": source_id,
                "plan_id": plan["plan_id"],
                "expected_revision": 1,
                "confirm": True,
            },
        )
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["idempotent"] is True

    assert store.read_including_deleted("sources", source_id) is None
    assert store.read("source_content_reads", "read-api") is None
    receipts = store.list("source_retention_purge_receipts")
    assert len(receipts) == 1
    assert "content" not in receipts[0]
    assert "title" not in receipts[0]


def test_source_purge_removes_owned_generated_audio_with_media_records(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault-media"
    store, source_id = _expired_source(root, suffix="media")
    media_root = root / "generated-audio"
    audio_path = media_root / source_id / "track.wav"
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"RIFF-api-owned")
    store.write(
        "video_audio_extractor_settings",
        "default",
        {"id": "default", "output_root": str(media_root)},
        expected_revision=0,
    )
    store.write(
        "media_processing_jobs",
        "job-media",
        {"id": "job-media", "source_id": source_id, "status": "completed"},
        expected_revision=0,
    )
    store.write(
        "media_processing_outputs",
        "output-media",
        {
            "id": "output-media",
            "source_id": source_id,
            "job_id": "job-media",
            "audio_asset_id": "audio-media",
            "status": "completed",
        },
        expected_revision=0,
    )
    store.write(
        "audio_asset_refs",
        "audio-media",
        {
            "id": "audio-media",
            "source_id": source_id,
            "path": str(audio_path),
            "path_scope": "local_generated_audio_track",
            "size_bytes": audio_path.stat().st_size,
            "status": "available",
        },
        expected_revision=0,
    )

    with _client(root) as client:
        planned = client.post(
            "/api/rebuild/retention/source-purge/plan",
            json={"source_id": source_id},
        )
        assert planned.status_code == 200, planned.text
        plan = planned.json()
        collections = {
            item["collection"] for item in plan["items"][0]["owned_records"]
        }
        assert {
            "media_processing_jobs",
            "media_processing_outputs",
            "audio_asset_refs",
        }.issubset(collections)
        executed = client.post(
            "/api/rebuild/retention/source-purge",
            json={
                "source_id": source_id,
                "plan_id": plan["plan_id"],
                "expected_revision": 1,
                "confirm": True,
            },
        )
        assert executed.status_code == 200, executed.text

    assert not audio_path.exists()
    assert store.read("audio_asset_refs", "audio-media") is None
    assert store.read("media_processing_outputs", "output-media") is None
    assert store.read("media_processing_jobs", "job-media") is None


def test_source_purge_rejects_inventory_drift_without_writes(tmp_path: Path) -> None:
    root = tmp_path / "drift"
    store, source_id = _expired_source(root, "drift")
    with _client(root) as client:
        planned = client.post(
            "/api/rebuild/retention/source-purge/plan",
            json={"source_id": source_id},
        )
        assert planned.status_code == 200, planned.text
        plan = planned.json()
        current = store.read("source_content_reads", "read-drift")
        store.write(
            "source_content_reads",
            "read-drift",
            {**current, "byte_count": 99},
            expected_revision=1,
        )

        rejected = client.post(
            "/api/rebuild/retention/source-purge",
            json={
                "source_id": source_id,
                "plan_id": plan["plan_id"],
                "expected_revision": 1,
                "confirm": True,
            },
        )
        assert rejected.status_code == 409
        assert store.read_including_deleted("sources", source_id) is not None
        assert store.read("source_content_reads", "read-drift") is not None
        assert store.list("source_retention_purge_intents") == ()
        assert store.list("source_retention_purge_receipts") == ()


def test_source_purge_scans_sqlite_references_and_fails_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sqlite-reference"
    store, source_id = _expired_source(root, "sqlite")
    records = SQLiteStructuredRecordStore(
        root / ".rebuild-data" / "structured-records.sqlite3"
    )
    with records.begin() as uow:
        uow.put(
            "memory_publications",
            "published-retention-reference",
            {
                "id": "published-retention-reference",
                "source_id": source_id,
                "summary": "不应出现在计划响应中",
            },
            expected_revision=0,
        )
        uow.commit()

    with _client(root) as client:
        planned = client.post(
            "/api/rebuild/retention/source-purge/plan",
            json={"source_id": source_id},
        )
        assert planned.status_code == 200, planned.text
        plan = planned.json()
        assert plan["items"][0]["eligible"] is False
        assert "inbound_references_present" in plan["items"][0]["blockers"]
        assert plan["items"][0]["inbound_references"] == [
            {
                "authority": "sqlite_structured_record",
                "collection": "memory_publications",
                "object_id": "published-retention-reference",
                "field_path": "$.source_id",
            }
        ]
        assert "不应出现在计划响应中" not in planned.text

        rejected = client.post(
            "/api/rebuild/retention/source-purge",
            json={
                "source_id": source_id,
                "plan_id": plan["plan_id"],
                "expected_revision": 1,
                "confirm": True,
            },
        )
        assert rejected.status_code == 409
    assert store.read_including_deleted("sources", source_id) is not None
