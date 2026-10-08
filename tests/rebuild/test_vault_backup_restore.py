from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from core.storage_provider import (
    SQLiteMigrationLedger,
    VaultBackupRestoreConflict,
    VaultBackupRestoreError,
    build_vault_migration_preflight,
    create_vault_backup,
    execute_preflight_bound_vault_migration,
    restore_vault_backup,
    verify_vault_backup,
)


def _legacy_root(tmp_path: Path) -> Path:
    root = tmp_path / "legacy"
    (root / ".rebuild-data" / "objects" / "default" / "sources").mkdir(parents=True)
    (root / ".rebuild-data" / "objects" / "default" / "sources" / "source-1.json").write_text('{"id":"source-1"}\n', encoding="utf-8")
    (root / "library").mkdir()
    (root / "library" / "note.md").write_text("private local note\n", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "settings.toml").write_text("[storage]\nmode='local'\n", encoding="utf-8")
    return root


def _preflight(tmp_path: Path, legacy: Path, target: Path):
    return build_vault_migration_preflight(
        legacy_root=legacy,
        target_vault_root=target,
        ledger=SQLiteMigrationLedger(tmp_path / "ledger.sqlite3"),
        migration_id="vault-drill-001",
        target_schema_version=1,
        backup_pointer="snapshot:vault-drill-001",
        now="2026-07-12T00:00:00Z",
    )


def test_backup_restore_round_trip_is_verified_and_keeps_source_unchanged(tmp_path: Path) -> None:
    legacy = _legacy_root(tmp_path)
    before = (legacy / "library" / "note.md").read_text(encoding="utf-8")

    snapshot = create_vault_backup(
        source_root=legacy,
        backups_root=tmp_path / "backups",
        snapshot_id="vault-drill-001",
    )
    verified = verify_vault_backup(snapshot.snapshot_root)
    restored = restore_vault_backup(snapshot_root=snapshot.snapshot_root, target_root=tmp_path / "restored")

    assert verified.source_fingerprint == snapshot.source_fingerprint
    assert restored.source_fingerprint == snapshot.source_fingerprint
    assert restored.file_count == snapshot.file_count == 3
    assert (tmp_path / "restored" / "library" / "note.md").read_text(encoding="utf-8") == before
    assert (legacy / "library" / "note.md").read_text(encoding="utf-8") == before


def test_backup_and_fingerprint_ignore_disposable_sqlite_shared_memory(
    tmp_path: Path,
) -> None:
    legacy = _legacy_root(tmp_path)
    transient = legacy / ".rebuild-data" / "runtime.sqlite3-shm"
    transient.write_bytes(b"disposable shared-memory index")

    snapshot = create_vault_backup(
        source_root=legacy,
        backups_root=tmp_path / "backups",
        snapshot_id="vault-shm-001",
    )
    transient.unlink()
    restored = restore_vault_backup(
        snapshot_root=snapshot.snapshot_root,
        target_root=tmp_path / "restored",
    )

    assert restored.file_count == 3
    assert not (tmp_path / "restored" / ".rebuild-data" / transient.name).exists()


def test_backup_checkpoints_wal_before_excluding_sqlite_sidecars(
    tmp_path: Path,
) -> None:
    legacy = _legacy_root(tmp_path)
    database = legacy / ".rebuild-data" / "runtime.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE durable_value (value TEXT NOT NULL)")
    connection.execute("INSERT INTO durable_value VALUES (?)", ("已提交 WAL 数据",))
    connection.commit()
    try:
        snapshot = create_vault_backup(
            source_root=legacy,
            backups_root=tmp_path / "backups",
            snapshot_id="vault-wal-001",
        )
    finally:
        connection.close()
    restored_root = tmp_path / "restored"
    restore_vault_backup(
        snapshot_root=snapshot.snapshot_root,
        target_root=restored_root,
    )

    restored = sqlite3.connect(restored_root / ".rebuild-data" / "runtime.sqlite3")
    try:
        assert restored.execute("SELECT value FROM durable_value").fetchone() == (
            "已提交 WAL 数据",
        )
    finally:
        restored.close()
    assert not tuple((snapshot.snapshot_root / "payload").rglob("*-wal"))
    assert not tuple((snapshot.snapshot_root / "payload").rglob("*-shm"))


def test_corrupt_snapshot_fails_before_restore_target_is_created(tmp_path: Path) -> None:
    legacy = _legacy_root(tmp_path)
    snapshot = create_vault_backup(source_root=legacy, backups_root=tmp_path / "backups", snapshot_id="vault-corrupt-001")
    (snapshot.snapshot_root / "payload" / "library" / "note.md").write_text("tampered\n", encoding="utf-8")
    target = tmp_path / "restored"

    with pytest.raises(VaultBackupRestoreError, match="hash"):
        restore_vault_backup(snapshot_root=snapshot.snapshot_root, target_root=target)

    assert not target.exists()
    assert (legacy / "library" / "note.md").read_text(encoding="utf-8") == "private local note\n"


def test_restore_rejects_nonempty_target_without_overwriting_it(tmp_path: Path) -> None:
    legacy = _legacy_root(tmp_path)
    snapshot = create_vault_backup(source_root=legacy, backups_root=tmp_path / "backups", snapshot_id="vault-target-001")
    target = tmp_path / "restored"
    target.mkdir()
    (target / "keep.txt").write_text("do not overwrite", encoding="utf-8")

    with pytest.raises(VaultBackupRestoreConflict, match="empty"):
        restore_vault_backup(snapshot_root=snapshot.snapshot_root, target_root=target)

    assert (target / "keep.txt").read_text(encoding="utf-8") == "do not overwrite"


def test_preflight_bound_migration_copies_verified_backup_only_when_source_is_unchanged(tmp_path: Path) -> None:
    legacy = _legacy_root(tmp_path)
    target = tmp_path / "formal-vault"
    preflight = _preflight(tmp_path, legacy, target)
    snapshot = create_vault_backup(source_root=legacy, backups_root=tmp_path / "backups", snapshot_id="vault-migration-001")

    result = execute_preflight_bound_vault_migration(
        migration=preflight,
        legacy_root=legacy,
        snapshot_root=snapshot.snapshot_root,
        target_vault_root=target,
    )

    assert result.migration_id == "vault-drill-001"
    assert result.source_fingerprint == snapshot.source_fingerprint
    assert (target / ".rebuild-data" / "objects" / "default" / "sources" / "source-1.json").is_file()
    assert (legacy / "library" / "note.md").read_text(encoding="utf-8") == "private local note\n"


def test_migration_rejects_source_drift_before_creating_target(tmp_path: Path) -> None:
    legacy = _legacy_root(tmp_path)
    target = tmp_path / "formal-vault"
    preflight = _preflight(tmp_path, legacy, target)
    snapshot = create_vault_backup(source_root=legacy, backups_root=tmp_path / "backups", snapshot_id="vault-drift-001")
    (legacy / "library" / "note.md").write_text("changed after preflight\n", encoding="utf-8")

    with pytest.raises(VaultBackupRestoreConflict, match="changed after preflight"):
        execute_preflight_bound_vault_migration(
            migration=preflight,
            legacy_root=legacy,
            snapshot_root=snapshot.snapshot_root,
            target_vault_root=target,
        )

    assert not target.exists()


def test_failed_backup_leaves_only_new_incomplete_snapshot_and_never_changes_source(tmp_path: Path) -> None:
    legacy = _legacy_root(tmp_path)

    def fail_copy(source: Path, destination: Path) -> None:
        destination.write_bytes(source.read_bytes())
        raise OSError("injected copy failure")

    with pytest.raises(VaultBackupRestoreError, match="did not complete"):
        create_vault_backup(
            source_root=legacy,
            backups_root=tmp_path / "backups",
            snapshot_id="vault-failure-001",
            copy_file=fail_copy,
        )

    snapshot = tmp_path / "backups" / "vault-failure-001"
    assert (snapshot / "vault-backup-incomplete.json").is_file()
    assert (legacy / "library" / "note.md").read_text(encoding="utf-8") == "private local note\n"


def test_backup_rejects_backup_root_inside_source_before_creating_snapshot(tmp_path: Path) -> None:
    legacy = _legacy_root(tmp_path)
    backup_root = legacy / "backups"

    with pytest.raises(VaultBackupRestoreConflict, match="cannot overlap"):
        create_vault_backup(source_root=legacy, backups_root=backup_root, snapshot_id="vault-overlap-001")

    assert not backup_root.exists()


def test_restore_rejects_target_inside_backup_snapshot(tmp_path: Path) -> None:
    legacy = _legacy_root(tmp_path)
    snapshot = create_vault_backup(source_root=legacy, backups_root=tmp_path / "backups", snapshot_id="vault-overlap-002")
    target = snapshot.snapshot_root / "new-target"

    with pytest.raises(VaultBackupRestoreConflict, match="cannot overlap"):
        restore_vault_backup(snapshot_root=snapshot.snapshot_root, target_root=target)

    assert not target.exists()


def test_backup_source_drift_during_copy_is_marked_incomplete(tmp_path: Path) -> None:
    legacy = _legacy_root(tmp_path)
    copied = False

    def drift_copy(source: Path, destination: Path) -> None:
        nonlocal copied
        destination.write_bytes(source.read_bytes())
        if not copied:
            copied = True
            (legacy / "library" / "note.md").write_text("changed during backup\n", encoding="utf-8")

    with pytest.raises(VaultBackupRestoreError, match="did not complete"):
        create_vault_backup(
            source_root=legacy,
            backups_root=tmp_path / "backups",
            snapshot_id="vault-drift-during-copy-001",
            copy_file=drift_copy,
        )

    assert (tmp_path / "backups" / "vault-drift-during-copy-001" / "vault-backup-incomplete.json").is_file()
