from __future__ import annotations

import gc
import shutil
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.vault_recovery_cli import main as recovery_cli_main
from core.storage_provider import (
    JsonObjectStore,
    load_vault_recovery_operation,
)


def _client(root: Path) -> TestClient:
    root.mkdir(parents=True, exist_ok=True)
    return TestClient(create_app(SimpleNamespace(root_dir=root)))


def _vault(root: Path) -> Path:
    return root / ".rebuild-data"


def _management(root: Path) -> Path:
    vault = _vault(root)
    return vault.parent / f".{vault.name}-recovery"


def test_settings_manual_snapshot_returns_real_restore_verification(tmp_path):
    from backend.memory_app.backup import verification_metadata, verify_runtime_backup
    from backend.memory_app.app import create_app as create_memory_app
    root = tmp_path / 'manual-verification'
    legacy = _client(root).app
    with TestClient(create_memory_app(runtime_root=root, legacy_app=legacy)) as client:
        response = client.post('/api/rebuild/memory-snapshots', json={})
        assert response.status_code == 200, response.text
        snapshot = _management(root) / 'snapshots' / response.json()['snapshot_id']
        assert response.json()['verified'] is True
        assert verification_metadata(snapshot)['verified'] is True
        row, = client.get('/api/rebuild/memory-snapshots').json()['snapshots']
        assert row['verified'] is True and row['verification_reason'] is None
        payload = next((snapshot / 'payload').rglob('*.sqlite3'))
        payload.write_bytes(b'corrupt synthetic database')
        assert verify_runtime_backup(snapshot)['verified'] is False
        row, = client.get('/api/rebuild/memory-snapshots').json()['snapshots']
        assert row['verified'] is False and row['verification_reason'] == 'backup_verification_failed'
        job, = client.get('/api/v2/jobs').json()['items']
        assert job['target'] == {'type': 'backup', 'id': snapshot.name}


def test_live_sqlite_backup_has_a_rollback_preview(tmp_path: Path) -> None:
    root = tmp_path / "live"
    with _client(root) as client:
        db = _vault(root) / "live.sqlite3"
        connection = sqlite3.connect(db)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE live_values(n INTEGER)")
        connection.commit()
        stop, ready = threading.Event(), threading.Event()
        def write():
            writer = sqlite3.connect(db)
            try:
                while not stop.wait(.002):
                    writer.execute("INSERT INTO live_values VALUES(1)")
                    writer.commit()
                    ready.set()
            finally:
                writer.close()
        thread = threading.Thread(target=write)
        thread.start()
        try:
            assert ready.wait(3)
            created = client.post("/api/rebuild/memory-snapshots", json={})
            assert created.status_code == 200, created.text
        finally:
            stop.set()
            thread.join(3)
            connection.close()
        assert not thread.is_alive()
        snapshot = created.json()
        preview = client.post(f"/api/rebuild/memory-snapshots/{snapshot['snapshot_id']}/rollback-plan", json={})
        assert preview.status_code == 200, preview.text
        assert preview.json()["target_vault_fingerprint"] == snapshot["vault_fingerprint"]


def test_snapshot_source_drift_returns_safe_reason_code(tmp_path: Path, monkeypatch) -> None:
    from core.storage_provider import vault_backup_restore as backup
    root = tmp_path / "drift"
    with _client(root) as client:
        note = _vault(root) / "note.txt"
        note.write_text("before")
        original = backup._copy_file
        def drift(source, destination):
            original(source, destination)
            if source == note:
                note.write_text("after")
        monkeypatch.setattr(backup, "_copy_file", drift)
        result = client.post("/api/rebuild/memory-snapshots", json={})
        assert result.status_code == 400
        assert result.json() == {"detail": "backup_source_changed"}
        assert str(root) not in result.text


def test_full_vault_snapshot_prepare_offline_adopt_and_restart(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    authority = _vault(root) / "library" / "原始资料.txt"
    authority.parent.mkdir(parents=True)
    authority.write_text("快照正文 🐉\n", encoding="utf-8")

    with _client(root) as client:
        created = client.post(
            "/api/rebuild/memory-snapshots",
            json={"label": "完整恢复点", "notes": "Unicode"},
        )
        assert created.status_code == 200, created.text
        snapshot = created.json()
        assert snapshot["restorable"] is True
        assert len(snapshot["vault_fingerprint"]) == 64
        assert snapshot["backup_file_count"] >= 1
        assert str(root) not in created.text

        snapshot_root = _management(root) / "snapshots" / snapshot["snapshot_id"]
        assert (snapshot_root / "vault-backup-manifest.json").is_file()
        assert (snapshot_root / "recovery-point.json").is_file()
        assert (snapshot_root / "payload" / "library" / "原始资料.txt").read_text(
            encoding="utf-8"
        ) == "快照正文 🐉\n"

        authority.write_text("恢复后不应保留\n", encoding="utf-8")
        added = _vault(root) / "library" / "later.txt"
        added.write_text("later", encoding="utf-8")
        plan = client.post(
            f"/api/rebuild/memory-snapshots/{snapshot['snapshot_id']}/rollback-plan",
            json={},
        )
        assert plan.status_code == 200, plan.text
        rollback = plan.json()
        assert len(rollback["current_vault_fingerprint"]) == 64
        assert rollback["target_vault_fingerprint"] == snapshot["vault_fingerprint"]

        prepared = client.post(
            f"/api/rebuild/memory-snapshots/{snapshot['snapshot_id']}/rollback",
            json={"rollback_id": rollback["rollback_id"], "confirm": True},
        )
        assert prepared.status_code == 202, prepared.text
        receipt = prepared.json()
        assert receipt["status"] == "prepared_restart_required"
        assert receipt["restore_executed"] is False
        assert str(root) not in prepared.text

    operations = _management(root) / "operations"
    operation = load_vault_recovery_operation(
        operations_root=operations, operation_id=receipt["operation_id"]
    )
    assert operation.state == "prepared"
    # This test runs the offline CLI in the same interpreter as TestClient.
    # Collect unreachable SQLite handles after application shutdown, as process
    # exit would, before Windows is asked to rename the old Vault directory.
    gc.collect()
    assert recovery_cli_main(
        ["--working-root", str(_vault(root)), "--operation-id", receipt["operation_id"]]
    ) == 0
    assert '"status":"adopted"' in capsys.readouterr().out
    adopted = load_vault_recovery_operation(
        operations_root=operations, operation_id=receipt["operation_id"]
    )
    assert adopted.state == "adopted"
    assert authority.read_text(encoding="utf-8") == "快照正文 🐉\n"
    assert not added.exists()
    assert Path(adopted.rollback_root, "library", "later.txt").read_text(
        encoding="utf-8"
    ) == "later"

    with _client(root) as restarted:
        status = restarted.get("/api/rebuild/vault-status")
        assert status.status_code == 200
        snapshots = restarted.get("/api/rebuild/memory-snapshots")
        assert snapshots.status_code == 200
        assert snapshots.json()["snapshots"][0]["snapshot_id"] == snapshot["snapshot_id"]
        assert snapshots.json()["snapshots"][0]["restorable"] is True
        repeat_plan = restarted.post(
            f"/api/rebuild/memory-snapshots/{snapshot['snapshot_id']}/rollback-plan",
            json={},
        )
        assert repeat_plan.status_code == 200


def test_restore_rejects_stale_plan_before_staging(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    _vault(root).mkdir()
    (_vault(root) / "seed.txt").write_text("one", encoding="utf-8")
    with _client(root) as client:
        snapshot = client.post(
            "/api/rebuild/memory-snapshots", json={"label": "base", "notes": ""}
        ).json()
        (_vault(root) / "seed.txt").write_text("two", encoding="utf-8")
        plan = client.post(
            f"/api/rebuild/memory-snapshots/{snapshot['snapshot_id']}/rollback-plan",
            json={},
        ).json()
        (_vault(root) / "changed-after-plan.txt").write_text("changed", encoding="utf-8")
        rejected = client.post(
            f"/api/rebuild/memory-snapshots/{snapshot['snapshot_id']}/rollback",
            json={"rollback_id": plan["rollback_id"], "confirm": True},
        )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"
        assert not (_management(root) / "operations").exists()


def test_legacy_manifest_only_snapshot_is_visible_but_not_restorable(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    with _client(root) as client:
        store = JsonObjectStore(
            root / ".rebuild-data", legacy_root=root / "library", namespace_id="default"
        )
        store.write(
            "memory_snapshots",
            "snap-legacy",
            {
                "schema_version": "1.0.0",
                "id": "snap-legacy",
                "created_at": "2026-07-01T00:00:00+00:00",
                "label": "旧状态标记",
                "namespace_id": "default",
                "layer_fingerprints": {},
                "layer_counts": {},
                "notes": "",
            },
            expected_revision=None,
        )
        listed = client.get("/api/rebuild/memory-snapshots")
        assert listed.status_code == 200
        assert listed.json()["snapshots"][0]["restorable"] is False
        rejected = client.post(
            "/api/rebuild/memory-snapshots/snap-legacy/rollback-plan", json={}
        )
        assert rejected.status_code == 400
        assert "manifest-only" in rejected.text


def test_recovery_point_retention_prunes_only_owned_full_backups(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    with _client(root) as client:
        store = JsonObjectStore(
            _vault(root), legacy_root=root / "library", namespace_id="default"
        )
        store.write(
            "memory_snapshots",
            "snap-legacy-retained",
            {
                "schema_version": "1.0.0",
                "id": "snap-legacy-retained",
                "created_at": "2020-01-01T00:00:00+00:00",
                "label": "旧版记录",
                "namespace_id": "default",
                "layer_fingerprints": {},
                "layer_counts": {},
                "notes": "",
            },
            expected_revision=None,
        )
        snapshots_root = _management(root) / "snapshots"
        snapshots_root.mkdir(parents=True, exist_ok=True)
        unrelated = snapshots_root / "operator-note"
        unrelated.mkdir()
        (unrelated / "keep.txt").write_text("not owned", encoding="utf-8")

        created_ids: list[str] = []
        for index in range(7):
            (_vault(root) / "generation.txt").write_text(
                f"generation-{index}", encoding="utf-8"
            )
            response = client.post(
                "/api/rebuild/memory-snapshots",
                json={"label": f"恢复点 {index}", "notes": ""},
            )
            assert response.status_code == 200, response.text
            created_ids.append(response.json()["snapshot_id"])
            if index == 0:
                plan = client.post(
                    f"/api/rebuild/memory-snapshots/{created_ids[0]}/rollback-plan",
                    json={},
                )
                assert plan.status_code == 200, plan.text
                prepared = client.post(
                    f"/api/rebuild/memory-snapshots/{created_ids[0]}/rollback",
                    json={
                        "rollback_id": plan.json()["rollback_id"],
                        "confirm": True,
                    },
                )
                assert prepared.status_code == 202, prepared.text

        listed = client.get("/api/rebuild/memory-snapshots")
        assert listed.status_code == 200
        items = listed.json()["snapshots"]
        full_ids = {
            item["snapshot_id"] for item in items if item["restorable"] is True
        }
        assert full_ids == set(created_ids)
        assert any(
            item["snapshot_id"] == "snap-legacy-retained"
            and item["restorable"] is False
            for item in items
        )
        assert unrelated.joinpath("keep.txt").read_text(encoding="utf-8") == "not owned"
        assert (snapshots_root / created_ids[0]).exists()
        assert (snapshots_root / created_ids[1]).exists()


def test_missing_external_backup_is_not_reported_as_restorable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    with _client(root) as client:
        created = client.post(
            "/api/rebuild/memory-snapshots",
            json={"label": "稍后丢失的恢复点", "notes": ""},
        )
        assert created.status_code == 200
        snapshot_id = created.json()["snapshot_id"]
        shutil.rmtree(_management(root) / "snapshots" / snapshot_id)

        listed = client.get("/api/rebuild/memory-snapshots")
        item = next(
            value
            for value in listed.json()["snapshots"]
            if value["snapshot_id"] == snapshot_id
        )
        assert item["restorable"] is False

        plan = client.post(
            f"/api/rebuild/memory-snapshots/{snapshot_id}/rollback-plan",
            json={},
        )
        assert plan.status_code == 400
        assert "payload or catalog is missing" in plan.text


def _confirm_after_sqlite_change(tmp_path: Path, *, change_record: bool, wal: bool = False):
    root = tmp_path / "vault"
    with _client(root) as client:
        database = _vault(root) / "confirmation.sqlite3"
        with sqlite3.connect(database) as connection:
            if wal:
                connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT)")
            connection.execute("INSERT INTO notes VALUES(1, 'before')")
            connection.execute("CREATE INDEX note_body ON notes(body)")
        created = client.post("/api/rebuild/memory-snapshots", json={})
        assert created.status_code == 200, created.text
        snapshot_id = created.json()["snapshot_id"]
        preview = client.post(f"/api/rebuild/memory-snapshots/{snapshot_id}/rollback-plan", json={})
        assert preview.status_code == 200, preview.text
        before = database.read_bytes()
        with sqlite3.connect(database) as connection:
            if change_record:
                connection.execute("UPDATE notes SET body='after' WHERE id=1")
            else:
                # Rebuilding the same index changes schema/header counters only.
                connection.execute("DROP INDEX note_body")
                connection.execute("CREATE INDEX note_body ON notes(body)")
        if wal:
            assert database.with_name(database.name + "-wal").stat().st_size > 0
        else:
            assert database.read_bytes() != before
        result = client.post(
            f"/api/rebuild/memory-snapshots/{snapshot_id}/rollback",
            json={"rollback_id": preview.json()["rollback_id"], "confirm": True},
        )
        if change_record:
            assert result.status_code == 200, result.text
            assert result.json()["status"] == "rejected"
            assert result.json()["reason"] == "rollback_id 与当前恢复计划不一致，已拒绝恢复。"
            assert not (_management(root) / "operations").exists()
        else:
            assert result.status_code == 202, result.text
            assert result.json()["status"] == "prepared_restart_required"
            assert result.json()["restore_executed"] is False


def test_restore_confirmation_accepts_sqlite_volatile_changes(tmp_path: Path) -> None:
    _confirm_after_sqlite_change(tmp_path, change_record=False)


def test_restore_confirmation_rejects_changed_sqlite_record(tmp_path: Path) -> None:
    _confirm_after_sqlite_change(tmp_path, change_record=True)


def test_restore_confirmation_rejects_uncheckpointed_sqlite_record(tmp_path: Path) -> None:
    _confirm_after_sqlite_change(tmp_path, change_record=True, wal=True)
