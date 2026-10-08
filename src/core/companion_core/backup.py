from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from .errors import (
    CompanionIntegrityError,
    CompanionRepositoryError,
    CompanionRestoreConflict,
    CompanionSchemaTooNew,
)
from .models import CompanionBackupReceipt, CompanionRestorePreflight, CompanionRestoreReceipt
from .repository import CompanionRepository
from .schema import SCHEMA_VERSION


_MANIFEST_SCHEMA_VERSION = "1.0.0"


class CompanionBackupService:
    """Consistent backup and preflight-bound restore for one Companion repository."""

    def __init__(self, repository: CompanionRepository) -> None:
        self.repository = repository

    def create_backup(self, backup_path: Path) -> CompanionBackupReceipt:
        target = backup_path.expanduser().absolute()
        manifest_path = _manifest_path(target)
        _require_new_backup_target(target, manifest_path, database_path=self.repository.database_path)
        created_at = self.repository._now_utc()

        with self.repository._maintenance_lock:
            self.repository._ensure_initialized()
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".companion-backup-",
                suffix=".sqlite3.tmp",
                dir=target.parent,
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            source = self.repository._open_connection()
            destination: sqlite3.Connection | None = None
            try:
                destination = sqlite3.connect(temporary)
                source.backup(destination)
                destination.commit()
                integrity = destination.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or str(integrity[0]).lower() != "ok":
                    raise CompanionIntegrityError("companion backup integrity check failed")
                version = int(destination.execute("PRAGMA user_version").fetchone()[0])
                if version != SCHEMA_VERSION:
                    raise CompanionIntegrityError("companion backup schema version is incomplete")
                destination.close()
                destination = None
                _fsync_file(temporary)
                size = temporary.stat().st_size
                fingerprint = _sha256_file(temporary)
                os.replace(temporary, target)
                manifest = {
                    "schema_version": _MANIFEST_SCHEMA_VERSION,
                    "database_schema_version": version,
                    "created_at": created_at,
                    "size_bytes": size,
                    "sha256": fingerprint,
                }
                _write_json_atomically(manifest_path, manifest)
                return CompanionBackupReceipt(
                    backup_path=target,
                    manifest_path=manifest_path,
                    fingerprint=fingerprint,
                    size_bytes=size,
                    database_schema_version=version,
                    created_at=created_at,
                )
            except Exception:
                if target.exists() and not manifest_path.exists():
                    target.unlink()
                raise
            finally:
                source.close()
                if destination is not None:
                    destination.close()
                if temporary.exists():
                    temporary.unlink()

    def preflight_restore(self, backup_path: Path) -> CompanionRestorePreflight:
        source = backup_path.expanduser().absolute()
        _require_directory_boundary(source.parent)
        if source.resolve(strict=False) == self.repository.database_path.resolve(strict=False):
            raise CompanionRestoreConflict("live companion database cannot be used as its own backup")
        manifest = _read_and_verify_manifest(source)
        source_version = _verified_database_version(source)
        manifest_version = manifest["database_schema_version"]
        if source_version != manifest_version:
            raise CompanionIntegrityError("companion backup schema version does not match manifest")
        if source_version > SCHEMA_VERSION:
            raise CompanionSchemaTooNew(
                f"companion backup schema {source_version} is newer than supported {SCHEMA_VERSION}"
            )
        target_version = _database_version_if_present(self.repository.database_path)
        if target_version is not None and target_version > SCHEMA_VERSION:
            raise CompanionSchemaTooNew(
                f"companion target schema {target_version} is newer than supported {SCHEMA_VERSION}"
            )
        return CompanionRestorePreflight(
            backup_path=source,
            fingerprint=str(manifest["sha256"]),
            size_bytes=int(manifest["size_bytes"]),
            source_schema_version=source_version,
            target_schema_version=target_version,
            requires_migration=source_version < SCHEMA_VERSION,
        )

    def restore_backup(
        self,
        backup_path: Path,
        *,
        expected_fingerprint: str,
        rollback_directory: Path,
    ) -> CompanionRestoreReceipt:
        if not _is_sha256(expected_fingerprint):
            raise CompanionRestoreConflict("expected restore fingerprint is invalid")
        rollback_root = rollback_directory.expanduser().absolute()
        _require_directory_boundary(rollback_root)

        with self.repository._maintenance_lock:
            preflight = self.preflight_restore(backup_path)
            if preflight.fingerprint != expected_fingerprint:
                raise CompanionRestoreConflict("companion backup changed after preflight")
            rollback_backup: CompanionBackupReceipt | None = None
            target_existed = self.repository.database_path.is_file()
            if target_existed:
                rollback_root.mkdir(parents=True, exist_ok=True)
                rollback_name = (
                    f"companion.pre-restore-{self.repository._now().strftime('%Y%m%dT%H%M%S%fZ')}-"
                    f"{expected_fingerprint[:8]}.sqlite3"
                )
                rollback_backup = self.create_backup(rollback_root / rollback_name)

            self.repository.database_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".companion-restore-",
                suffix=".sqlite3.tmp",
                dir=self.repository.database_path.parent,
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                shutil.copyfile(preflight.backup_path, temporary)
                _fsync_file(temporary)
                if _sha256_file(temporary) != expected_fingerprint:
                    raise CompanionRestoreConflict("companion restore copy fingerprint changed")
                self._checkpoint_target_if_present()
                os.replace(temporary, self.repository.database_path)
                _remove_wal_sidecars(self.repository.database_path)
                self.repository._initialized = False
                status = self.repository.initialize()
                completed_at = self.repository._now_utc()
                return CompanionRestoreReceipt(
                    restored_fingerprint=expected_fingerprint,
                    restored_schema_version=status.schema_version,
                    rollback_backup=rollback_backup,
                    completed_at=completed_at,
                )
            except Exception as exc:
                self.repository._initialized = False
                try:
                    if rollback_backup is not None:
                        shutil.copyfile(rollback_backup.backup_path, temporary)
                        _fsync_file(temporary)
                        os.replace(temporary, self.repository.database_path)
                        _remove_wal_sidecars(self.repository.database_path)
                        self.repository.initialize()
                    elif not target_existed:
                        if self.repository.database_path.exists():
                            self.repository.database_path.unlink()
                        _remove_wal_sidecars(self.repository.database_path)
                except Exception as rollback_error:
                    raise CompanionIntegrityError(
                        "companion restore failed and automatic rollback did not complete"
                    ) from rollback_error
                if isinstance(exc, CompanionRepositoryError):
                    raise
                raise CompanionIntegrityError("companion restore did not complete") from exc
            finally:
                if temporary.exists():
                    temporary.unlink()

    def _checkpoint_target_if_present(self) -> None:
        if not self.repository.database_path.is_file():
            return
        connection = self.repository._open_connection()
        try:
            result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if result is None or int(result[0]) != 0:
                raise CompanionRestoreConflict("companion database is busy and cannot be restored")
        finally:
            connection.close()


def _manifest_path(backup_path: Path) -> Path:
    return backup_path.with_name(f"{backup_path.name}.manifest.json")


def _require_new_backup_target(target: Path, manifest: Path, *, database_path: Path) -> None:
    _require_directory_boundary(target.parent)
    if target.resolve(strict=False) == database_path.resolve(strict=False):
        raise CompanionRepositoryError("companion backup cannot overwrite the live database")
    if target.exists() or target.is_symlink() or manifest.exists() or manifest.is_symlink():
        raise CompanionRestoreConflict("companion backup target already exists")


def _require_directory_boundary(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.exists() and candidate.is_symlink():
            raise CompanionRepositoryError("companion backup path cannot traverse a symlink")
    if path.exists() and not path.is_dir():
        raise CompanionRepositoryError("companion backup directory is not a directory")


def _read_and_verify_manifest(backup_path: Path) -> dict[str, object]:
    if not backup_path.is_file() or backup_path.is_symlink():
        raise CompanionIntegrityError("companion backup file is missing or unsafe")
    manifest_path = _manifest_path(backup_path)
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise CompanionIntegrityError("companion backup manifest is missing or unsafe")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompanionIntegrityError("companion backup manifest is unreadable") from exc
    required = {
        "schema_version",
        "database_schema_version",
        "created_at",
        "size_bytes",
        "sha256",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise CompanionIntegrityError("companion backup manifest fields are invalid")
    if manifest["schema_version"] != _MANIFEST_SCHEMA_VERSION:
        raise CompanionIntegrityError("companion backup manifest version is unsupported")
    if not isinstance(manifest["database_schema_version"], int) or manifest["database_schema_version"] < 0:
        raise CompanionIntegrityError("companion backup schema version is invalid")
    if not isinstance(manifest["size_bytes"], int) or manifest["size_bytes"] < 1:
        raise CompanionIntegrityError("companion backup size is invalid")
    if not isinstance(manifest["created_at"], str) or not _is_sha256(manifest["sha256"]):
        raise CompanionIntegrityError("companion backup metadata is invalid")
    if backup_path.stat().st_size != manifest["size_bytes"]:
        raise CompanionIntegrityError("companion backup size does not match manifest")
    if _sha256_file(backup_path) != manifest["sha256"]:
        raise CompanionIntegrityError("companion backup fingerprint does not match manifest")
    return manifest


def _verified_database_version(path: Path) -> int:
    connection = _open_readonly(path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise CompanionIntegrityError("companion backup integrity check failed")
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def _database_version_if_present(path: Path) -> int | None:
    if path.is_symlink():
        raise CompanionIntegrityError("companion target database path is unsafe")
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise CompanionIntegrityError("companion target database path is unsafe")
    connection = _open_readonly(path)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def _open_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise CompanionIntegrityError("companion database cannot be opened read-only") from exc


def _remove_wal_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            candidate.unlink()


def _write_json_atomically(path: Path, payload: dict[str, object]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".companion-manifest-",
        suffix=".json.tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)
