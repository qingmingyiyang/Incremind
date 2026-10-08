from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.companion_core import (
    CompanionBackupService,
    CompanionIntegrityError,
    CompanionRepository,
    CompanionRepositoryError,
    CompanionRestoreConflict,
    CompanionSchemaTooNew,
)


FIXED_NOW = datetime(2026, 7, 19, 6, 0, tzinfo=timezone.utc)


def _repository(path: Path) -> CompanionRepository:
    return CompanionRepository(path, now=lambda: FIXED_NOW)


def _earn(repository: CompanionRepository, *, suffix: str, delta: int, second: int) -> None:
    repository.record_wallet_transaction(
        transaction_id=f"transaction:{suffix}",
        idempotency_key=f"reward:{suffix}",
        reason=f"reward {suffix}",
        delta=delta,
        created_at=f"2026-07-19T06:00:{second:02d}+00:00",
    )


def _manifest_path(backup: Path) -> Path:
    return backup.with_name(f"{backup.name}.manifest.json")


def _rewrite_manifest_hash(backup: Path) -> None:
    manifest_path = _manifest_path(backup)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["size_bytes"] = backup.stat().st_size
    manifest["sha256"] = hashlib.sha256(backup.read_bytes()).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def test_online_backup_has_verified_manifest_and_preflight(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "live" / "companion.sqlite3")
    _earn(repository, suffix="focus:001", delta=5, second=1)
    service = CompanionBackupService(repository)
    backup = tmp_path / "backups" / "companion.sqlite3"

    receipt = service.create_backup(backup)
    preflight = service.preflight_restore(backup)

    assert receipt.backup_path == backup
    assert receipt.manifest_path == _manifest_path(backup)
    assert receipt.fingerprint == hashlib.sha256(backup.read_bytes()).hexdigest()
    assert receipt.size_bytes == backup.stat().st_size
    assert receipt.database_schema_version == 10
    assert preflight.fingerprint == receipt.fingerprint
    assert preflight.source_schema_version == 10
    assert preflight.target_schema_version == 10
    assert preflight.requires_migration is False


def test_backup_never_overwrites_live_or_existing_target(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "live" / "companion.sqlite3")
    repository.initialize()
    service = CompanionBackupService(repository)

    with pytest.raises(CompanionRepositoryError, match="live database"):
        service.create_backup(repository.database_path)

    backup = tmp_path / "backup.sqlite3"
    service.create_backup(backup)
    with pytest.raises(CompanionRestoreConflict, match="already exists"):
        service.create_backup(backup)


def test_live_database_cannot_be_used_as_restore_source(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "live.sqlite3")
    repository.initialize()
    service = CompanionBackupService(repository)

    with pytest.raises(CompanionRestoreConflict, match="own backup"):
        service.preflight_restore(repository.database_path)


def test_preflight_rejects_tampered_backup(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "live.sqlite3")
    repository.initialize()
    service = CompanionBackupService(repository)
    backup = tmp_path / "backup.sqlite3"
    service.create_backup(backup)
    with backup.open("ab") as stream:
        stream.write(b"tampered")

    with pytest.raises(CompanionIntegrityError, match="size"):
        service.preflight_restore(backup)


def test_preflight_rejects_future_schema_even_with_matching_manifest(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "live.sqlite3")
    repository.initialize()
    service = CompanionBackupService(repository)
    backup = tmp_path / "future.sqlite3"
    service.create_backup(backup)
    connection = sqlite3.connect(backup)
    connection.execute("PRAGMA user_version=99")
    connection.commit()
    connection.close()
    manifest = json.loads(_manifest_path(backup).read_text(encoding="utf-8"))
    manifest["database_schema_version"] = 99
    _manifest_path(backup).write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _rewrite_manifest_hash(backup)

    with pytest.raises(CompanionSchemaTooNew):
        service.preflight_restore(backup)


def test_restore_creates_rollback_snapshot_and_restores_bound_backup(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "live" / "companion.sqlite3")
    service = CompanionBackupService(repository)
    _earn(repository, suffix="before-backup", delta=5, second=1)
    repository.create_session(
        session_id="session:review", context_epoch=1, prompt_revision=1,
        profile_revision=1, started_at="2026-07-19T06:00:00+00:00",
    )
    repository.append_message(
        message_id="message:review", request_id="request:review", session_id="session:review",
        context_epoch=1, role="assistant", status="completed", content="回顾内容",
        created_at="2026-07-19T06:00:01+00:00", provider_mode="remote",
        memory_review={
            "review_scope": "today_memory_review", "status": "recalled", "matched_count": 1,
            "memory_ids": ["atom-backup"], "generated": True,
        },
    )
    backup = tmp_path / "backups" / "chosen.sqlite3"
    chosen = service.create_backup(backup)
    _earn(repository, suffix="after-backup", delta=3, second=2)
    assert repository.wallet_integrity().snapshot_balance == 8

    restored = service.restore_backup(
        backup,
        expected_fingerprint=chosen.fingerprint,
        rollback_directory=tmp_path / "rollback",
    )

    assert restored.restored_fingerprint == chosen.fingerprint
    assert restored.restored_schema_version == 10
    assert restored.rollback_backup is not None
    assert restored.rollback_backup.backup_path.is_file()
    assert restored.rollback_backup.manifest_path.is_file()
    assert repository.wallet_integrity().snapshot_balance == 5
    assert repository.get_message("message:review").memory_review["memory_ids"] == ["atom-backup"]
    rollback_repository = _repository(restored.rollback_backup.backup_path)
    assert rollback_repository.wallet_integrity().snapshot_balance == 8


def test_restore_rechecks_fingerprint_after_preflight(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "live.sqlite3")
    repository.initialize()
    service = CompanionBackupService(repository)
    backup = tmp_path / "backup.sqlite3"
    receipt = service.create_backup(backup)
    service.preflight_restore(backup)
    with backup.open("ab") as stream:
        stream.write(b"changed-after-preflight")

    with pytest.raises(CompanionIntegrityError):
        service.restore_backup(
            backup,
            expected_fingerprint=receipt.fingerprint,
            rollback_directory=tmp_path / "rollback",
        )


def test_restore_failure_automatically_recovers_original_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path / "live" / "companion.sqlite3")
    service = CompanionBackupService(repository)
    _earn(repository, suffix="backup-state", delta=5, second=1)
    backup = tmp_path / "backups" / "chosen.sqlite3"
    chosen = service.create_backup(backup)
    _earn(repository, suffix="live-state", delta=3, second=2)
    original_initialize = repository.initialize
    calls = 0

    def fail_first_restore_initialize():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CompanionIntegrityError("simulated post-replace migration failure")
        return original_initialize()

    monkeypatch.setattr(repository, "initialize", fail_first_restore_initialize)

    with pytest.raises(CompanionIntegrityError, match="simulated"):
        service.restore_backup(
            backup,
            expected_fingerprint=chosen.fingerprint,
            rollback_directory=tmp_path / "rollback",
        )

    assert repository.wallet_integrity().snapshot_balance == 8
    assert calls == 2


def test_restore_rejects_wrong_bound_fingerprint_without_touching_target(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "live.sqlite3")
    _earn(repository, suffix="live", delta=7, second=1)
    service = CompanionBackupService(repository)
    backup = tmp_path / "backup.sqlite3"
    service.create_backup(backup)

    with pytest.raises(CompanionRestoreConflict, match="changed after preflight"):
        service.restore_backup(
            backup,
            expected_fingerprint="0" * 64,
            rollback_directory=tmp_path / "rollback",
        )

    assert repository.wallet_integrity().snapshot_balance == 7
