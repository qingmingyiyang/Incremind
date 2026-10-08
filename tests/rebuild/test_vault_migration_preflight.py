from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.storage_provider import (
    SQLiteMigrationLedger,
    VaultMigrationPreflightConflict,
    VaultMigrationPreflightError,
    build_vault_migration_preflight,
)


def _legacy_root(tmp_path: Path) -> Path:
    root = tmp_path / "legacy"
    object_path = root / ".rebuild-data" / "objects" / "default" / "jobs" / "job-vault-1.json"
    object_path.parent.mkdir(parents=True)
    object_path.write_text(json.dumps({"id": "job-vault-1", "status": "failed"}), encoding="utf-8")
    (root / "library").mkdir()
    (root / "library" / "note.txt").write_text("legacy note", encoding="utf-8")
    return root


def test_preflight_creates_dry_run_without_creating_or_writing_target(tmp_path: Path) -> None:
    legacy = _legacy_root(tmp_path)
    target = tmp_path / "formal" / "vault"
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")

    plan = build_vault_migration_preflight(
        legacy_root=legacy,
        target_vault_root=target,
        ledger=ledger,
        migration_id="legacy-to-vault-v1",
        target_schema_version=2,
        backup_pointer="snapshot:legacy-before-vault-v1",
        now="2026-07-11T12:00:00+00:00",
    )

    assert target.exists() is False
    assert plan.migration.state == "dry_run_ready"
    assert plan.source_file_count == 2
    assert len(plan.source_fingerprint) == 64
    assert plan.rollback_plan == "restore:snapshot:legacy-before-vault-v1"
    assert [item.collection for item in plan.migration.inventory.collections] == ["library", "rebuild_data"]


def test_preflight_rejects_nonempty_target_and_root_overlap(tmp_path: Path) -> None:
    legacy = _legacy_root(tmp_path)
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")
    target = tmp_path / "vault"
    target.mkdir()
    (target / "data").mkdir()

    with pytest.raises(VaultMigrationPreflightConflict, match="not empty"):
        build_vault_migration_preflight(
            legacy_root=legacy,
            target_vault_root=target,
            ledger=ledger,
            migration_id="legacy-to-vault-v1",
            target_schema_version=2,
            backup_pointer="snapshot:legacy",
        )

    with pytest.raises(VaultMigrationPreflightConflict, match="inside source"):
        build_vault_migration_preflight(
            legacy_root=legacy,
            target_vault_root=legacy / "vault",
            ledger=ledger,
            migration_id="legacy-to-vault-v2",
            target_schema_version=2,
            backup_pointer="snapshot:legacy",
        )


def test_preflight_requires_backup_pointer_and_preserves_idempotent_plan(tmp_path: Path) -> None:
    legacy = _legacy_root(tmp_path)
    target = tmp_path / "vault"
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")

    with pytest.raises(VaultMigrationPreflightError, match="backup pointer"):
        build_vault_migration_preflight(
            legacy_root=legacy,
            target_vault_root=target,
            ledger=ledger,
            migration_id="legacy-to-vault-v1",
            target_schema_version=2,
            backup_pointer="",
        )

    first = build_vault_migration_preflight(
        legacy_root=legacy,
        target_vault_root=target,
        ledger=ledger,
        migration_id="legacy-to-vault-v1",
        target_schema_version=2,
        backup_pointer="snapshot:legacy",
        now="2026-07-11T12:00:00+00:00",
    )
    repeated = build_vault_migration_preflight(
        legacy_root=legacy,
        target_vault_root=target,
        ledger=ledger,
        migration_id="legacy-to-vault-v1",
        target_schema_version=2,
        backup_pointer="snapshot:legacy",
        now="2026-07-11T12:01:00+00:00",
    )
    assert repeated == first
