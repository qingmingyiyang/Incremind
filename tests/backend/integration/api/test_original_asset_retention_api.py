from __future__ import annotations

import hashlib
import json
import base64
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
    SQLiteStructuredRecordStore,
    SourceAssetRuntimeStore,
)


CANARY = b"ORIGINAL_ASSET_RETENTION_PRIVATE_CANARY"
SHA256 = hashlib.sha256(CANARY).hexdigest()
ASSET_ID = f"original-file-{SHA256[:16]}"
VAULT_REF = f"assets/originals/{SHA256[:2]}/{ASSET_ID}.txt"


def _client(root: Path) -> TestClient:
    root.mkdir(parents=True, exist_ok=True)
    return TestClient(create_app(SimpleNamespace(root_dir=root)))


def _store(root: Path) -> JsonObjectStore:
    return JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library")


def _asset(root: Path) -> tuple[JsonObjectStore, Path]:
    store = _store(root)
    path = root / "library" / VAULT_REF
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(CANARY)
    store.write(
        "workbench_original_assets",
        ASSET_ID,
        {
            "schema_version": "1.0.0",
            "id": ASSET_ID,
            "kind": "workbench_original_asset",
            "display_name": "private-source.txt",
            "media_type": "text/plain",
            "byte_count": len(CANARY),
            "sha256": SHA256,
            "vault_ref": VAULT_REF,
            "link_status": "pending",
            "orphan_reason": "awaiting_source_capture",
            "created_at": "2026-06-01T00:00:00Z",
        },
        expected_revision=0,
    )
    return store, path


def _activate_empty_source_asset_authority(
    root: Path,
) -> tuple[SourceAssetRuntimeStore, SQLiteStructuredRecordStore]:
    evidence = AggregateAuthorityEvidence(
        migration_id="source-asset-retention-cutover-v1",
        source_fingerprint="a" * 64,
        target_fingerprint="b" * 64,
        target_identity=TARGET_IDENTITY,
    )
    json_store = _store(root)
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
            reason="retention cutover fixture",
        )
        staged = authority.transition(
            namespace_id="default",
            aggregate=member,
            expected_revision=initial.revision,
            to_state="sqlite_staged",
            evidence=evidence,
            reason="retention cutover fixture",
        )
        authority.transition(
            namespace_id="default",
            aggregate=member,
            expected_revision=staged.revision,
            to_state="sqlite_active",
            evidence=evidence,
            reason="retention cutover fixture",
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
    return (
        SourceAssetRuntimeStore(
            json_store=json_store,
            sqlite_records=records,
            library_root=root / "library",
            authority_identity=TARGET_IDENTITY,
        ),
        records,
    )


def test_original_asset_retention_api_backs_up_purges_and_replays(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    store, original = _asset(root)
    with _client(root) as client:
        reconciled = client.post(
            "/api/rebuild/retention/original-assets/reconcile"
        )
        assert reconciled.status_code == 200, reconciled.text
        assert reconciled.json()["items"] == [
            {
                "asset_id": ASSET_ID,
                "status": "orphaned",
                "revision": 2,
                "orphaned_at": reconciled.json()["items"][0]["orphaned_at"],
            }
        ]
        current = dict(store.read("workbench_original_assets", ASSET_ID) or {})
        current["orphaned_at"] = "2026-07-01T00:00:00Z"
        store.write(
            "workbench_original_assets",
            ASSET_ID,
            current,
            expected_revision=2,
        )
        restored_authority = dict(
            store.read("workbench_original_assets", ASSET_ID) or {}
        )

        listed = client.get(
            "/api/rebuild/retention/original-assets/candidates"
        )
        assert listed.status_code == 200
        assert listed.json() == {
            "items": [
                {
                    "asset_id": ASSET_ID,
                    "display_name": "private-source.txt",
                    "media_type": "text/plain",
                    "byte_count": len(CANARY),
                    "revision": 3,
                    "link_status": "orphaned",
                    "orphaned_at": "2026-07-01T00:00:00Z",
                    "eligible_after": "2026-07-08T00:00:00Z",
                    "retention_elapsed": True,
                }
            ]
        }
        assert CANARY.decode() not in listed.text
        assert str(root) not in listed.text

        planned = client.post(
            "/api/rebuild/retention/original-assets/plan",
            json={"asset_id": ASSET_ID},
        )
        assert planned.status_code == 200, planned.text
        plan = planned.json()
        assert plan["eligible"] is True
        assert plan["blockers"] == []
        assert plan["byte_action"] == "delete"
        assert plan["revision"] == 3
        assert plan["snapshot_id"].startswith("snap-asset-retention-")
        assert plan["asset_backup_id"].startswith("asset-backup-")
        assert CANARY.decode() not in planned.text
        assert str(root) not in planned.text

        unconfirmed = client.post(
            "/api/rebuild/retention/original-assets",
            json={
                "asset_id": ASSET_ID,
                "plan_id": plan["plan_id"],
                "expected_revision": 3,
                "confirm": False,
            },
        )
        assert unconfirmed.status_code == 409
        assert original.read_bytes() == CANARY

        executed = client.post(
            "/api/rebuild/retention/original-assets",
            json={
                "asset_id": ASSET_ID,
                "plan_id": plan["plan_id"],
                "expected_revision": 3,
                "confirm": True,
            },
        )
        assert executed.status_code == 200, executed.text
        assert executed.json()["status"] == "completed"
        assert executed.json()["idempotent"] is False
        assert CANARY.decode() not in executed.text

    with _client(root) as restarted:
        replay = restarted.post(
            "/api/rebuild/retention/original-assets",
            json={
                "asset_id": ASSET_ID,
                "plan_id": plan["plan_id"],
                "expected_revision": 3,
                "confirm": True,
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent"] is True

    assert not original.exists()
    assert store.read("workbench_original_assets", ASSET_ID) is None
    receipts = store.list("original_asset_retention_receipts")
    assert len(receipts) == 1
    assert CANARY.decode() not in json.dumps(receipts, ensure_ascii=False)
    management = root / "..rebuild-data-recovery"
    plan_files = list(
        (management / "operations" / "asset-plans").glob("*.json")
    )
    assert len(plan_files) == 1
    assert CANARY.decode() not in plan_files[0].read_text(encoding="utf-8")
    backup_payload = (
        management
        / "original-asset-backups"
        / plan["asset_backup_id"]
        / "payload.bin"
    )
    assert backup_payload.read_bytes() == CANARY
    store.write(
        "workbench_original_assets",
        ASSET_ID,
        restored_authority,
        expected_revision=0,
    )
    with _client(root) as recovery_client:
        restored = recovery_client.post(
            (
                "/api/rebuild/retention/original-assets/backups/"
                f"{plan['asset_backup_id']}/restore"
            ),
            json={"asset_id": ASSET_ID, "confirm": True},
        )
        assert restored.status_code == 200, restored.text
        assert restored.json() == {
            "status": "restored",
            "asset_id": ASSET_ID,
            "backup_id": plan["asset_backup_id"],
            "byte_count": len(CANARY),
            "idempotent": False,
        }
        replay_restore = recovery_client.post(
            (
                "/api/rebuild/retention/original-assets/backups/"
                f"{plan['asset_backup_id']}/restore"
            ),
            json={"asset_id": ASSET_ID, "confirm": True},
        )
        assert replay_restore.status_code == 200
        assert replay_restore.json()["idempotent"] is True
    assert original.read_bytes() == CANARY


def test_original_asset_retention_rejects_link_drift_after_plan(
    tmp_path: Path,
) -> None:
    root = tmp_path / "drift"
    store, original = _asset(root)
    current = dict(store.read("workbench_original_assets", ASSET_ID) or {})
    current.update(
        {
            "link_status": "orphaned",
            "orphaned_at": "2026-07-01T00:00:00Z",
            "orphan_reason": "no_active_source_asset_links",
        }
    )
    store.write(
        "workbench_original_assets",
        ASSET_ID,
        current,
        expected_revision=1,
    )
    with _client(root) as client:
        planned = client.post(
            "/api/rebuild/retention/original-assets/plan",
            json={"asset_id": ASSET_ID},
        )
        assert planned.status_code == 200, planned.text
        plan = planned.json()
        store.write(
            "source_asset_links",
            "link-late",
            {
                "id": "link-late",
                "source_id": "source-late",
                "asset_id": ASSET_ID,
            },
            expected_revision=0,
        )
        rejected = client.post(
            "/api/rebuild/retention/original-assets",
            json={
                "asset_id": ASSET_ID,
                "plan_id": plan["plan_id"],
                "expected_revision": 2,
                "confirm": True,
            },
        )
        assert rejected.status_code == 409
        assert "inventory changed" in rejected.json()["detail"]
    assert original.read_bytes() == CANARY
    assert store.read("workbench_original_assets", ASSET_ID) is not None
    assert store.list("original_asset_retention_intents") == ()
    assert store.list("original_asset_retention_receipts") == ()


def test_sqlite_active_retention_purges_replays_and_restores_canonical_blob(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sqlite-vault"
    root.mkdir()
    runtime, records = _activate_empty_source_asset_authority(root)
    content = b"SQLITE_RETENTION_CUTOVER_PRIVATE_CANARY"

    with _client(root) as client:
        uploaded = client.post(
            "/api/rebuild/workbench/original-asset",
            json={
                "display_name": "retention.txt",
                "media_type": "text/plain",
                "size_bytes": len(content),
                "content_base64": base64.b64encode(content).decode("ascii"),
                "source_kind": "file",
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        payload = uploaded.json()
        asset_id = str(payload["asset_id"])
        vault_ref = str(payload["vault_ref"])
        canonical = root / "library" / vault_ref
        assert vault_ref.startswith("assets/blobs/")

        reconciled = client.post(
            "/api/rebuild/retention/original-assets/reconcile"
        )
        assert reconciled.status_code == 200, reconciled.text
        current = dict(runtime.read("workbench_original_assets", asset_id) or {})
        current["orphaned_at"] = "2026-07-01T00:00:00Z"
        runtime.write(
            "workbench_original_assets",
            asset_id,
            current,
            expected_revision=runtime.revision(
                "workbench_original_assets", asset_id
            ),
        )
        restored_authority = dict(
            runtime.read("workbench_original_assets", asset_id) or {}
        )
        expected_revision = runtime.revision(
            "workbench_original_assets", asset_id
        )

        planned = client.post(
            "/api/rebuild/retention/original-assets/plan",
            json={"asset_id": asset_id},
        )
        assert planned.status_code == 200, planned.text
        plan = planned.json()
        assert plan["eligible"] is True
        assert plan["byte_action"] == "delete"

        executed = client.post(
            "/api/rebuild/retention/original-assets",
            json={
                "asset_id": asset_id,
                "plan_id": plan["plan_id"],
                "expected_revision": expected_revision,
                "confirm": True,
            },
        )
        assert executed.status_code == 200, executed.text
        assert executed.json()["idempotent"] is False

    with _client(root) as restarted:
        replay = restarted.post(
            "/api/rebuild/retention/original-assets",
            json={
                "asset_id": asset_id,
                "plan_id": plan["plan_id"],
                "expected_revision": expected_revision,
                "confirm": True,
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent"] is True

    assert not canonical.exists()
    assert records.list("original_assets") == ()
    assert records.list("asset_blobs") == ()
    assert _store(root).list("workbench_original_assets") == ()
    assert _store(root).list("source_asset_links") == ()

    runtime.write(
        "workbench_original_assets",
        asset_id,
        restored_authority,
        expected_revision=0,
    )
    with _client(root) as recovery:
        restored = recovery.post(
            (
                "/api/rebuild/retention/original-assets/backups/"
                f"{plan['asset_backup_id']}/restore"
            ),
            json={"asset_id": asset_id, "confirm": True},
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["idempotent"] is False

    assert canonical.read_bytes() == content
