from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.memory_core import ObjectStoreMemoryStore
from core.storage_provider import JsonObjectStore


def _store(tmp_path):
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _seed(store, object_id="memory-a"):
    store.write("memory_scenarios", object_id, {
        "schema_version": "1.0.0", "id": object_id, "project_id": "project-a",
        "revision": 1, "summary": "old summary", "trust_status": "user_confirmed",
        "source_refs": [{"source_id": "source-a", "locator": "line:1"}],
    }, expected_revision=0)


def test_memory_lifecycle_http_supersede_redact_and_restore(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        superseded = client.post("/api/rebuild/memory/scenario/memory-a/supersede", json={
            "project_id": "project-a", "expected_revision": 1,
            "expected_storage_revision": 1, "changes": {"summary": "corrected summary"},
            "reason": "user correction", "confirm": True,
        })
        assert superseded.status_code == 200
        assert superseded.json()["revision"] == 2

        redacted = client.post("/api/rebuild/memory/scenario/memory-a/redact", json={
            "project_id": "project-a", "expected_revision": 2,
            "expected_storage_revision": 2, "reason": "hide this memory",
            "mode": "soft", "confirm": True,
        })
        assert redacted.status_code == 200
        assert ObjectStoreMemoryStore(store).get("scenario", "memory-a") is None

        restored = client.post("/api/rebuild/memory/scenario/memory-a/restore", json={
            "project_id": "project-a", "expected_revision": 3,
            "expected_storage_revision": 3, "confirm": True,
        })
        assert restored.status_code == 200
        assert restored.json()["revision"] == 4
        assert ObjectStoreMemoryStore(store).get("scenario", "memory-a")["summary"] == "corrected summary"


def test_memory_lifecycle_http_rejects_stale_and_unguarded_hard_delete(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        stale = client.post("/api/rebuild/memory/scenario/memory-a/supersede", json={
            "project_id": "project-a", "expected_revision": 9,
            "expected_storage_revision": 1, "changes": {"summary": "stale"},
            "reason": "stale", "confirm": True,
        })
        assert stale.status_code == 409
        hard = client.post("/api/rebuild/memory/scenario/memory-a/redact", json={
            "project_id": "project-a", "expected_revision": 1,
            "expected_storage_revision": 1, "reason": "privacy",
            "mode": "hard", "confirm": True, "hard_confirmation": "DELETE",
        })
        assert hard.status_code == 400
        assert ObjectStoreMemoryStore(store).get("scenario", "memory-a") is not None


def test_memory_lifecycle_http_lineage_batch_confirm_replay_and_undo(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    _seed(store, "memory-b")
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        heads = client.get("/api/rebuild/memory/lifecycle", params={"project_id": "project-a"})
        assert heads.status_code == 200
        assert [item["object_id"] for item in heads.json()["items"]] == ["memory-a", "memory-b"]
        assert {item["storage_revision"] for item in heads.json()["items"]} == {1}

        lineage = client.post("/api/rebuild/memory/lineage", json={
            "project_id": "project-a",
            "memory_ref": "crp://default/memory/scenario/memory-a@r1",
            "consumer_kind": "document",
            "consumer_ref": "crp://documents/project-a/document-a",
        })
        assert lineage.status_code == 201

        preview = client.post("/api/rebuild/memory/batches/soft-redact/preview", json={
            "project_id": "project-a",
            "reason": "remove selected memories",
            "items": [
                {
                    "layer": "scenario", "object_id": "memory-a",
                    "expected_revision": 1, "expected_storage_revision": 1,
                },
                {
                    "layer": "scenario", "object_id": "memory-b",
                    "expected_revision": 1, "expected_storage_revision": 1,
                },
            ],
        })
        assert preview.status_code == 200
        preview_body = preview.json()
        assert len(preview_body["preview_token"]) == 64
        assert ObjectStoreMemoryStore(store).get("scenario", "memory-a") is not None

        confirmed = client.post("/api/rebuild/memory/batches/soft-redact/confirm", json={
            "preview_token": preview_body["preview_token"], "confirm": True,
        })
        assert confirmed.status_code == 200
        operation = confirmed.json()
        assert operation["status"] == "completed"
        assert len(operation["completed"]) == 2
        assert ObjectStoreMemoryStore(store).list("scenario") == ()
        lineage_record = store.read("memory_lineage_refs", lineage.json()["lineage_id"])
        assert lineage_record is not None and lineage_record["status"] == "stale"

        replay = client.post("/api/rebuild/memory/batches/soft-redact/confirm", json={
            "preview_token": preview_body["preview_token"], "confirm": True,
        })
        assert replay.status_code == 200
        assert replay.json()["id"] == operation["id"]

        undone = client.post(
            f"/api/rebuild/memory/batches/{operation['id']}/undo", json={"confirm": True},
        )
        assert undone.status_code == 200
        assert undone.json()["status"] == "undone"
        assert {item["id"] for item in ObjectStoreMemoryStore(store).list("scenario")} == {
            "memory-a", "memory-b",
        }


def test_memory_lifecycle_http_batch_resume_and_confirmation_guards(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        preview = client.post("/api/rebuild/memory/batches/soft-redact/preview", json={
            "project_id": "project-a", "reason": "recover interrupted batch",
            "items": [{
                "layer": "scenario", "object_id": "memory-a",
                "expected_revision": 1, "expected_storage_revision": 1,
            }],
        }).json()
        rejected = client.post("/api/rebuild/memory/batches/soft-redact/confirm", json={
            "preview_token": preview["preview_token"], "confirm": False,
        })
        assert rejected.status_code == 400
        missing = client.post("/api/rebuild/memory/batches/soft-redact/confirm", json={
            "preview_token": "0" * 64, "confirm": True,
        })
        assert missing.status_code == 400

        operation_id = f"memory-batch-{preview['preview_token'][:20]}"
        store.write("memory_lifecycle_batches", operation_id, {
            "schema_version": "1.0.0", "id": operation_id, "action": "soft_redact",
            "project_id": "project-a", "preview_token": preview["preview_token"],
            "reason": preview["reason"], "occurred_at": preview["occurred_at"],
            "status": "applying", "completed": [], "items": preview["items"],
        }, expected_revision=0)
        resumed = client.post(
            f"/api/rebuild/memory/batches/{operation_id}/resume", json={"confirm": True},
        )
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "completed"
        assert ObjectStoreMemoryStore(store).get("scenario", "memory-a") is None
