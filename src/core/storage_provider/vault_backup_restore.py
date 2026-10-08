from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import date, datetime, timezone
from uuid import uuid4
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .vault_migration_preflight import VaultMigrationPreflight, scan_legacy_vault_inventory


_SAFE_SNAPSHOT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_MANIFEST_NAME = "vault-backup-manifest.json"
_PAYLOAD_DIRECTORY = "payload"


class VaultBackupRestoreError(ValueError):
    """Raised when a controlled Vault backup, restore, or migration is unsafe."""

    def __init__(self, message: str, *, reason_code: str = "backup_failed") -> None:
        super().__init__(message)
        self.reason_code = reason_code


class VaultBackupRestoreConflict(VaultBackupRestoreError):
    """Raised when an existing root or immutable snapshot conflicts with an operation."""


@dataclass(frozen=True, slots=True)
class VaultBackupSnapshot:
    snapshot_id: str
    snapshot_root: Path
    source_fingerprint: str
    file_count: int


@dataclass(frozen=True, slots=True)
class VaultRestoreResult:
    snapshot_id: str
    target_root: Path
    source_fingerprint: str
    file_count: int


@dataclass(frozen=True, slots=True)
class VaultMigrationResult:
    migration_id: str
    target_root: Path
    source_fingerprint: str
    file_count: int


def create_vault_backup(
    *,
    source_root: Path,
    backups_root: Path,
    snapshot_id: str,
    copy_file: Callable[[Path, Path], None] | None = None,
    sqlite_online: bool = False,
    exclude_logs: bool = False,
    restore_verify: bool = False,
    automatic: bool = False,
    job_id: str | None = None,
) -> VaultBackupSnapshot:
    """Create a content-addressed immutable snapshot without changing the source.

    This explicit primitive is intentionally not wired to application startup or
    user data roots. A failed copy leaves only a newly-created incomplete snapshot
    diagnostic and never removes or changes source files.
    """

    source = _require_existing_root(source_root, label="backup source")
    backup_parent = _require_non_symlink_path(backups_root, label="backup root")
    _require_disjoint_roots(source, backup_parent, message="vault backup root cannot overlap source")
    if backup_parent.exists() and not backup_parent.is_dir():
        raise VaultBackupRestoreConflict("vault backup root must be a directory")
    _require_snapshot_id(snapshot_id)
    snapshot = backup_parent / snapshot_id
    if snapshot.exists() or snapshot.is_symlink():
        raise VaultBackupRestoreConflict("vault backup snapshot already exists")
    backup_parent.mkdir(parents=True, exist_ok=True)
    snapshot.mkdir()
    payload_root = snapshot / _PAYLOAD_DIRECTORY
    payload_root.mkdir()
    copier = copy_file or _copy_file
    try:
        # Live SQLite commits belong to the online snapshot, not a raw-file
        # comparison. All other files retain the original before/after check.
        files = _backup_inventory(source, sqlite_online=sqlite_online, exclude_logs=exclude_logs)
        databases = {relative: _file_identity(path) for relative, path in files if sqlite_online and _is_sqlite_file(path)}
        ordinary = tuple(item for item in files if item[0] not in databases)
        starting_fingerprint = _fingerprint_inventory(ordinary)
        manifest_files = []
        for relative, path in files:
            if relative in databases:
                destination = _safe_join(payload_root, relative)
                _make_directory(destination.parent)
                _backup_sqlite_file(path, destination, databases[relative])
                manifest_files.append({"path": relative, "size": _file_size(destination), "sha256": _sha256_file(destination)})
            else:
                manifest_files.extend(_copy_inventory(((relative, path),), destination_root=payload_root, copy_file=copier))
        source_fingerprint = _fingerprint_manifest_files(manifest_files)
        _write_manifest(
            snapshot,
            {
                "schema_version": "1.0.0",
                "snapshot_id": snapshot_id,
                "source_fingerprint": source_fingerprint,
                "files": manifest_files,
            },
        )
        ending_files = _backup_inventory(source, sqlite_online=sqlite_online, exclude_logs=exclude_logs)
        copied_ordinary = [item for item in manifest_files if item["path"] not in databases]
        if tuple(item[0] for item in ending_files) != tuple(item[0] for item in files):
            raise VaultBackupRestoreConflict("vault backup source changed while copying", reason_code="backup_source_changed")
        ending_fingerprint = _fingerprint_inventory(tuple(item for item in ending_files if item[0] not in databases))
        if starting_fingerprint != _fingerprint_manifest_files(copied_ordinary) or ending_fingerprint != starting_fingerprint:
            raise VaultBackupRestoreConflict("vault backup source changed while copying", reason_code="backup_source_changed")
        for relative, identity in databases.items():
            _require_file_identity(_safe_join(source, relative), identity)
    except Exception as error:
        _write_incomplete_marker(snapshot, reason=type(error).__name__)
        raise VaultBackupRestoreError("vault backup copy did not complete", reason_code=getattr(error, "reason_code", "backup_failed")) from error
    result = VaultBackupSnapshot(
        snapshot_id=snapshot_id,
        snapshot_root=snapshot,
        source_fingerprint=source_fingerprint,
        file_count=len(manifest_files),
    )
    if restore_verify:
        _write_verification(snapshot, {'schema_version': '1.0.0', 'snapshot_id': snapshot_id, 'job_id': job_id,
            'tables': _sqlite_tables(payload_root),
            'automatic': automatic, 'created_at': datetime.now(timezone.utc).isoformat(), 'verified': False,
            'reason_code': 'backup_verification_pending'})
        checked = verify_runtime_backup(snapshot)
        if not checked['verified']:
            raise VaultBackupRestoreError('backup restore verification failed', reason_code=checked['reason_code'])
    return result



def verify_vault_backup(snapshot_root: Path) -> VaultBackupSnapshot:
    snapshot = _require_existing_root(snapshot_root, label="backup snapshot")
    if (snapshot / "vault-backup-incomplete.json").exists():
        raise VaultBackupRestoreError("vault backup snapshot is incomplete")
    manifest = _read_manifest(snapshot)
    snapshot_id = _required_snapshot_id(manifest.get("snapshot_id"))
    files = _manifest_files(manifest.get("files"))
    expected_fingerprint = _required_fingerprint(manifest.get("source_fingerprint"))
    if _fingerprint_manifest_files(files) != expected_fingerprint:
        raise VaultBackupRestoreError("vault backup manifest fingerprint is invalid")
    payload = snapshot / _PAYLOAD_DIRECTORY
    _require_existing_root(payload, label="backup payload")
    actual_files = _inventory_files(payload)
    if tuple(item[0] for item in actual_files) != tuple(item["path"] for item in files):
        raise VaultBackupRestoreError("vault backup payload file set does not match manifest")
    for item in files:
        path = _safe_join(payload, item["path"])
        if _file_size(path) != item["size"] or _sha256_file(path) != item["sha256"]:
            raise VaultBackupRestoreError("vault backup payload hash does not match manifest")
    return VaultBackupSnapshot(snapshot_id, snapshot, expected_fingerprint, len(files))


def restore_vault_backup(
    *,
    snapshot_root: Path,
    target_root: Path,
    copy_file: Callable[[Path, Path], None] | None = None,
) -> VaultRestoreResult:
    """Restore a verified snapshot only into a missing or empty target root."""

    snapshot = verify_vault_backup(snapshot_root)
    target = _require_empty_target(target_root, label="restore target")
    _require_disjoint_roots(snapshot.snapshot_root, target, message="vault restore target cannot overlap backup")
    manifest = _read_manifest(snapshot.snapshot_root)
    files = _manifest_files(manifest.get("files"))
    payload = snapshot.snapshot_root / _PAYLOAD_DIRECTORY
    copier = copy_file or _copy_file
    target.mkdir(parents=True, exist_ok=True)
    try:
        for item in files:
            origin = _safe_join(payload, item["path"])
            destination = _safe_join(target, item["path"])
            _make_directory(destination.parent)
            copier(origin, destination)
            if _file_size(destination) != item["size"] or _sha256_file(destination) != item["sha256"]:
                raise VaultBackupRestoreError("vault restore copy verification failed")
    except Exception as error:
        _write_incomplete_marker(target, reason=type(error).__name__)
        raise VaultBackupRestoreError("vault restore did not complete") from error
    restored_fingerprint = _fingerprint_inventory(_durable_inventory_files(target))
    if restored_fingerprint != snapshot.source_fingerprint:
        raise VaultBackupRestoreError("vault restore fingerprint does not match snapshot")
    return VaultRestoreResult(snapshot.snapshot_id, target, restored_fingerprint, len(files))


def compare_vault_to_backup(*, snapshot_root: Path, vault_root: Path) -> VaultRestoreResult:
    """Verify an existing Vault tree against an immutable backup without writing either."""

    snapshot = verify_vault_backup(snapshot_root)
    vault = _require_existing_root(vault_root, label="vault comparison root")
    _require_disjoint_roots(snapshot.snapshot_root, vault, message="vault comparison root cannot overlap backup")
    fingerprint = _fingerprint_inventory(_durable_inventory_files(vault))
    if fingerprint != snapshot.source_fingerprint:
        raise VaultBackupRestoreError("vault comparison fingerprint does not match snapshot")
    return VaultRestoreResult(snapshot.snapshot_id, vault, fingerprint, snapshot.file_count)


def fingerprint_vault_root(vault_root: Path) -> str:
    """Return the verified full-tree fingerprint for an existing Vault root."""

    vault = _require_existing_root(vault_root, label="Vault fingerprint root")
    return _fingerprint_inventory(_durable_inventory_files(vault))


def fingerprint_vault_restore_source(vault_root: Path) -> str:
    """Bind a confirmation to logical SQLite contents, not storage counters.

    Physical backup manifests and offline adoption keep fingerprint_vault_root.
    This identity is only for comparing the live source across preview/confirm.
    """
    vault = _require_existing_root(vault_root, label="Vault restore source")
    files = _inventory_files(vault)
    entries = []
    for relative, path in files:
        identity = _file_identity(path)
        if _is_sqlite_file(path):
            entry = (relative, "sqlite-logical-v1", _sqlite_logical_fingerprint(path))
        else:
            entry = (relative, "file", _file_size(path), _sha256_file(path))
        _require_file_identity(path, identity)
        entries.append(entry)
    if tuple(name for name, _ in _inventory_files(vault)) != tuple(name for name, _ in files):
        raise VaultBackupRestoreConflict("Vault source file set changed", reason_code="backup_source_changed")
    return hashlib.sha256(_logical_json(entries)).hexdigest()


def _logical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _sqlite_logical_fingerprint(path: Path) -> str:
    """Read one SQLite snapshot, including schema, row identities and types."""
    def identifier(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def cell(value: object) -> tuple[str, object]:
        if value is None:
            return ("null", None)
        if isinstance(value, bytes):
            return ("blob", value.hex())
        if isinstance(value, float):
            return ("real", value.hex())
        if isinstance(value, int):
            return ("integer", value)
        return ("text", value)

    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            version = connection.execute("PRAGMA data_version").fetchone()[0]
            connection.execute("BEGIN")
            schema = connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
            ).fetchall()
            digest = hashlib.sha256()
            def append(value: object) -> None:
                digest.update(_logical_json(value))
                digest.update(b"\n")
            append(("sqlite-logical-v1", schema))
            for setting in ("user_version", "application_id", "encoding"):
                append((setting, connection.execute("PRAGMA " + setting).fetchone()[0]))
            tables = {row[1]: row for row in connection.execute("PRAGMA table_list") if row[0] == "main"}
            for kind, name, _table, _sql in schema:
                if kind != "table":
                    continue
                if tables[name][2] == "virtual":
                    # Built-in virtual tables persist in their shadow tables.
                    # Do not execute extension modules or external-content reads.
                    identifier_pattern = r'(?:"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\]]*\]|[a-zA-Z_][a-zA-Z_0-9]*)'
                    module = re.match(
                        r"^CREATE\s+VIRTUAL\s+TABLE\s+" + identifier_pattern
                        + r"\s+USING\s+(fts[345]|rtree(?:_i32)?)\s*\(", _sql or "", re.I,
                    )
                    if module is None:
                        raise VaultBackupRestoreError("unsupported virtual table in restore source")
                    continue
                columns = {row[1].lower() for row in connection.execute("PRAGMA table_xinfo(" + identifier(name) + ")")}
                # WITHOUT ROWID tables have their primary key in SELECT *.
                rowid = next((alias for alias in ("rowid", "_rowid_", "oid") if alias not in columns), None)
                has_rowid = not tables[name][4]
                if has_rowid and rowid is None:
                    raise VaultBackupRestoreError("restore source table hides every row identity alias")
                projection = (identifier(rowid) + ", *") if rowid and has_rowid else "*"
                rows = sorted(
                    _logical_json([cell(value) for value in row])
                    for row in connection.execute("SELECT " + projection + " FROM " + identifier(name))
                )
                append(("table", name, rowid if has_rowid else None, len(rows)))
                for row in rows:
                    digest.update(row)
                    digest.update(b"\n")
            connection.rollback()
            if connection.execute("PRAGMA data_version").fetchone()[0] != version:
                raise VaultBackupRestoreConflict("Vault SQLite changed during logical snapshot", reason_code="backup_source_changed")
            return digest.hexdigest()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as error:
        raise VaultBackupRestoreError("vault SQLite logical snapshot failed", reason_code="backup_sqlite_failed") from error


def execute_preflight_bound_vault_migration(
    *,
    migration: VaultMigrationPreflight,
    legacy_root: Path,
    snapshot_root: Path,
    target_vault_root: Path,
) -> VaultMigrationResult:
    """Copy a verified snapshot only after live legacy input still matches preflight.

    The function never deletes or switches the legacy root. It is deliberately an
    explicit operation so application startup cannot invoke it implicitly.
    """

    live_inventory = scan_legacy_vault_inventory(legacy_root)
    if live_inventory.fingerprint != migration.source_fingerprint:
        raise VaultBackupRestoreConflict("vault migration source changed after preflight")
    snapshot = verify_vault_backup(snapshot_root)
    live_tree_fingerprint = _fingerprint_inventory(
        _durable_inventory_files(
            _require_existing_root(legacy_root, label="migration source")
        )
    )
    if snapshot.source_fingerprint != live_tree_fingerprint:
        raise VaultBackupRestoreConflict("vault migration backup does not match source")
    restored = restore_vault_backup(snapshot_root=snapshot.snapshot_root, target_root=target_vault_root)
    return VaultMigrationResult(
        migration_id=migration.migration.migration_id,
        target_root=restored.target_root,
        source_fingerprint=restored.source_fingerprint,
        file_count=restored.file_count,
    )


def _require_existing_root(value: Path, *, label: str) -> Path:
    path = _require_non_symlink_path(value, label=label)
    if not path.exists() or not path.is_dir():
        raise VaultBackupRestoreError(f"{label} must be an existing directory")
    return path


def _require_empty_target(value: Path, *, label: str) -> Path:
    path = _require_non_symlink_path(value, label=label)
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise VaultBackupRestoreConflict(f"{label} must be empty")
    return path


def _require_non_symlink_path(value: Path, *, label: str) -> Path:
    raw = value.expanduser().absolute()
    for candidate in (raw, *raw.parents):
        if candidate.exists() and candidate.is_symlink():
            raise VaultBackupRestoreError(f"{label} cannot be a symlink")
    return raw.resolve(strict=False)


def _require_disjoint_roots(first: Path, second: Path, *, message: str) -> None:
    try:
        second.relative_to(first)
    except ValueError:
        pass
    else:
        raise VaultBackupRestoreConflict(message)
    try:
        first.relative_to(second)
    except ValueError:
        return
    raise VaultBackupRestoreConflict(message)


def _require_snapshot_id(value: str) -> None:
    if not _SAFE_SNAPSHOT_ID.fullmatch(value):
        raise VaultBackupRestoreError("vault backup snapshot id is invalid")


def _required_snapshot_id(value: object) -> str:
    if not isinstance(value, str):
        raise VaultBackupRestoreError("vault backup manifest snapshot id is invalid")
    _require_snapshot_id(value)
    return value


def _backup_inventory(root: Path, *, sqlite_online: bool, exclude_logs: bool):
    files = _inventory_files(root) if sqlite_online else _durable_inventory_files(root)
    if not exclude_logs:
        return files
    def log_path(relative):
        parts = relative.split('/')
        return (parts[0] == 'logs' or parts[:2] == ['server', 'logs']
                or len(parts) > 2 and parts[0] in {'users', 'shared'} and parts[2] == 'logs')
    return tuple(item for item in files if not log_path(item[0]))


def _inventory_files(root: Path) -> tuple[tuple[str, Path], ...]:
    files: list[tuple[str, Path]] = []
    native_root = _native_io_path(root)
    for current, directories, filenames in os.walk(native_root):
        for name in (*directories, *filenames):
            if os.path.islink(os.path.join(current, name)):
                raise VaultBackupRestoreError(
                    "vault backup rejects symlinked entries"
                )
        for name in filenames:
            # SQLite shared-memory indexes are disposable process-local caches.
            # They may disappear as the last connection closes and are rebuilt
            # from the database/WAL, so they are neither backup authority nor a
            # stable Vault fingerprint input.
            if name.endswith(("-shm", "-wal")):
                continue
            native_path = os.path.join(current, name)
            if not os.path.isfile(native_path):
                continue
            relative = os.path.relpath(native_path, native_root).replace(
                "\\",
                "/",
            )
            _safe_relative(relative)
            files.append((relative, root / Path(relative)))
    return tuple(sorted(files, key=lambda item: item[0]))


def _durable_inventory_files(root: Path) -> tuple[tuple[str, Path], ...]:
    _checkpoint_sqlite_databases(root)
    return _inventory_files(root)


def _checkpoint_sqlite_databases(root: Path) -> None:
    native_root = _native_io_path(root)
    databases: list[str] = []
    for current, directories, filenames in os.walk(native_root):
        for name in (*directories, *filenames):
            if os.path.islink(os.path.join(current, name)):
                raise VaultBackupRestoreError(
                    "vault backup rejects symlinked entries"
                )
        databases.extend(
            os.path.join(current, name)
            for name in filenames
            if name.endswith((".sqlite", ".sqlite3", ".db"))
            and os.path.isfile(os.path.join(current, name))
        )
    for database in sorted(databases):
        try:
            connection = sqlite3.connect(database, timeout=5)
            try:
                row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if row is not None and int(row[0]) != 0:
                    raise VaultBackupRestoreError(
                        "vault SQLite checkpoint is busy"
                    )
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as error:
            raise VaultBackupRestoreError(
                "vault SQLite checkpoint failed"
            ) from error


def _copy_inventory(
    files: tuple[tuple[str, Path], ...],
    *,
    destination_root: Path,
    copy_file: Callable[[Path, Path], None],
) -> list[dict[str, object]]:
    manifest_files: list[dict[str, object]] = []
    for relative, source in files:
        destination = _safe_join(destination_root, relative)
        _make_directory(destination.parent)
        copy_file(source, destination)
        if _sha256_file(source) != _sha256_file(destination):
            raise VaultBackupRestoreError("vault backup copy verification failed", reason_code="backup_source_changed")
        manifest_files.append({"path": relative, "size": _file_size(source), "sha256": _sha256_file(source)})
    return manifest_files


def _copy_file(source: Path, destination: Path) -> None:
    shutil.copyfile(_native_io_path(source), _native_io_path(destination))
    with open(_native_io_path(destination), "r+b") as copied:
        os.fsync(copied.fileno())


def _file_identity(path: Path) -> tuple[int, int]:
    stat = os.stat(_native_io_path(path), follow_symlinks=False)
    return stat.st_dev, stat.st_ino


def _require_file_identity(path: Path, expected: tuple[int, int]) -> None:
    if path.is_symlink() or not path.is_file() or _file_identity(path) != expected:
        raise VaultBackupRestoreConflict("vault backup source changed while copying", reason_code="backup_source_changed")


def _is_sqlite_file(path: Path) -> bool:
    with open(_native_io_path(path), "rb") as source:
        header = source.read(16)
    return header == b"SQLite format 3\x00" or path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}


def _backup_sqlite_file(source: Path, destination: Path, identity: tuple[int, int]) -> None:
    _require_file_identity(source, identity)
    deadline = time.monotonic() + 30
    def progress(status: int, remaining: int, total: int) -> None:
        _require_file_identity(source, identity)
        if time.monotonic() > deadline:
            raise VaultBackupRestoreError("vault SQLite backup is busy", reason_code="backup_sqlite_busy")
    try:
        # Pin a read transaction so continuous WAL commits cannot restart the
        # backup indefinitely. mode=ro prevents creating a missing authority.
        origin = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            origin.execute("BEGIN")
            origin.execute("SELECT count(*) FROM sqlite_master").fetchone()
            target = sqlite3.connect(_native_io_path(destination))
            try:
                origin.backup(target, pages=256, progress=progress, sleep=.01)
                target.execute("PRAGMA journal_mode=DELETE")
            finally:
                target.close()
        finally:
            origin.close()
        _require_file_identity(source, identity)
        with open(_native_io_path(destination), "r+b") as copied:
            os.fsync(copied.fileno())
    except sqlite3.Error as error:
        raise VaultBackupRestoreError("vault SQLite online backup failed", reason_code="backup_sqlite_failed") from error


def _write_manifest(snapshot: Path, payload: dict[str, object]) -> None:
    _write_json_atomically(snapshot / _MANIFEST_NAME, payload)


def _write_incomplete_marker(root: Path, *, reason: str) -> None:
    marker = root / "vault-backup-incomplete.json"
    if marker.exists():
        return
    _write_json_atomically(marker, {"schema_version": "1.0.0", "status": "incomplete", "reason": reason})


def _write_json_atomically(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".vault-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_manifest(snapshot: Path) -> dict[str, object]:
    path = snapshot / _MANIFEST_NAME
    if not path.is_file() or path.is_symlink():
        raise VaultBackupRestoreError("vault backup manifest is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VaultBackupRestoreError("vault backup manifest is unreadable") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != "1.0.0":
        raise VaultBackupRestoreError("vault backup manifest is invalid")
    return payload


def _manifest_files(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise VaultBackupRestoreError("vault backup manifest files are invalid")
    files: list[dict[str, object]] = []
    paths: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            raise VaultBackupRestoreError("vault backup manifest file is invalid")
        relative = item.get("path")
        size = item.get("size")
        digest = item.get("sha256")
        if not isinstance(relative, str) or not relative or not isinstance(size, int) or size < 0:
            raise VaultBackupRestoreError("vault backup manifest file metadata is invalid")
        _safe_relative(relative)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise VaultBackupRestoreError("vault backup manifest file hash is invalid")
        paths.append(relative)
        files.append({"path": relative, "size": size, "sha256": digest})
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise VaultBackupRestoreError("vault backup manifest file order is invalid")
    return files


def _safe_join(root: Path, relative: str) -> Path:
    _safe_relative(relative)
    path = root / relative
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as error:
        raise VaultBackupRestoreError("vault backup path escapes root") from error
    return path


def _safe_relative(value: str) -> None:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts or value.replace("\\", "/") != value:
        raise VaultBackupRestoreError("vault backup manifest path is invalid")


def _required_fingerprint(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise VaultBackupRestoreError("vault backup fingerprint is invalid")
    return value


def _fingerprint_manifest_files(files: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["size"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _fingerprint_inventory(files: tuple[tuple[str, Path], ...]) -> str:
    return _fingerprint_manifest_files([
        {"path": relative, "size": _file_size(path), "sha256": _sha256_file(path)}
        for relative, path in files
    ])


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(_native_io_path(path), "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_size(path: Path) -> int:
    return os.stat(_native_io_path(path)).st_size


def _make_directory(path: Path) -> None:
    os.makedirs(_native_io_path(path), exist_ok=True)


def _native_io_path(path: Path) -> str:
    resolved = str(path.resolve(strict=False))
    if os.name != "nt" or resolved.startswith("\\\\?\\"):
        return resolved
    if resolved.startswith("\\\\"):
        return "\\\\?\\UNC\\" + resolved[2:]
    return "\\\\?\\" + resolved


def _sqlite_tables(root):
    tables = {}
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open('rb') as stream:
            if stream.read(16) != b'SQLite format 3\x00':
                continue
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as database:
            database.execute('BEGIN')
            if database.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('backup_sqlite_failed')
            stats = {}
            names = database.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            for (name,) in names:
                quoted = '"' + name.replace('"', '""') + '"'
                columns = [row[1] for row in database.execute(f'PRAGMA table_info({quoted})')]
                maximum = database.execute(f'SELECT MAX(revision) FROM {quoted}').fetchone()[0] if 'revision' in columns else None
                stats[name] = {'count': database.execute(f'SELECT COUNT(*) FROM {quoted}').fetchone()[0], 'max_revision': maximum}
            tables[path.relative_to(root).as_posix()] = stats
    return tables


def verification_metadata(snapshot):
    path = Path(snapshot) / 'backup-verification.json'
    if path.is_symlink():
        raise ValueError('backup_verification_failed')
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding='utf8'))
    if not isinstance(value, dict):
        raise ValueError('backup_verification_failed')
    return value


def _write_verification(snapshot, value):
    path = Path(snapshot) / 'backup-verification.json'
    if Path(snapshot).is_symlink() or path.is_symlink():
        raise ValueError('backup_verification_failed')
    temporary = path.with_name(f'.verification-{uuid4().hex}.tmp')
    with temporary.open('x', encoding='utf8') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def verify_runtime_backup(snapshot):
    """Restore the owner's snapshot in a disposable root and compare every table."""
    snapshot = _require_existing_root(Path(snapshot), label='backup snapshot')
    meta = {}
    try:
        meta = verification_metadata(snapshot)
        # Old snapshots have no table baseline; the owner's manifest still binds
        # their entire payload. Never compare with a live, concurrently edited root.
        baseline = meta.get('tables')
        if baseline is not None and not isinstance(baseline, dict):
            raise ValueError('backup_verification_failed')
        with tempfile.TemporaryDirectory(prefix='chriptmas-backup-check-') as temporary:
            restored = restore_vault_backup(snapshot_root=snapshot, target_root=Path(temporary) / 'restored')
            expected = _sqlite_tables(snapshot / 'payload') if baseline is None else baseline
            if _sqlite_tables(restored.target_root) != expected:
                raise ValueError('backup_verification_failed')
        meta.update(verified=True, reason_code=None, tables=expected)
    except (OSError, ValueError, sqlite3.Error, VaultBackupRestoreError):
        meta.update(verified=False, reason_code='backup_verification_failed')
    _write_verification(snapshot, meta)
    return meta


def write_backup_catalog(snapshot, payload):
    path = Path(snapshot) / 'recovery-point.json'
    with path.open('x', encoding='utf8', newline='\n') as output:
        output.write(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n')
        output.flush()
        os.fsync(output.fileno())


def read_backup_catalog(snapshot):
    path = Path(snapshot) / 'recovery-point.json'
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get('schema_version') != '2.0.0':
        return None
    identity, fingerprint = value.get('id'), value.get('vault_fingerprint')
    if (not isinstance(identity, str) or re.fullmatch(r'snap-[a-z0-9][a-z0-9._-]{0,122}', identity) is None
            or Path(snapshot).name != identity or not isinstance(fingerprint, str)
            or re.fullmatch(r'[0-9a-f]{64}', fingerprint) is None or value.get('restorable') is not True):
        return None
    return value


def _automatic_backup_metadata(snapshot):
    """Conservative ownership validation; parse existing metadata, never rehash."""
    try:
        meta = verification_metadata(snapshot)
        if (meta.get('schema_version') != '1.0.0' or meta.get('snapshot_id') != snapshot.name
                or meta.get('automatic') is not True or type(meta.get('verified')) is not bool):
            return None
        created = datetime.fromisoformat(meta['created_at'])
        job_date = date.fromisoformat(meta['job_id'])
        if created.tzinfo is None or job_date.isoformat() != meta['job_id']:
            return None
        if meta['reason_code'] not in {None, 'backup_verification_pending', 'backup_verification_failed'}:
            return None
        if (meta['verified'] and meta['reason_code'] is not None or not meta['verified']
                and meta['reason_code'] not in {'backup_verification_pending', 'backup_verification_failed'}):
            return None
        tables = meta['tables']
        if not isinstance(tables, dict):
            return None
        for relative, stats in tables.items():
            _safe_relative(relative)
            if not isinstance(stats, dict):
                return None
            for table, values in stats.items():
                if (not isinstance(table, str) or not table or not isinstance(values, dict)
                        or set(values) != {'count', 'max_revision'} or type(values['count']) is not int
                        or values['count'] < 0 or values['max_revision'] is not None
                        and type(values['max_revision']) is not int):
                    return None
        manifest = _read_manifest(snapshot)
        if _required_snapshot_id(manifest.get('snapshot_id')) != snapshot.name:
            return None
        _required_fingerprint(manifest.get('source_fingerprint'))
        _manifest_files(manifest.get('files'))
        return meta
    except (OSError, ValueError, TypeError, KeyError):
        return None


def prune_automatic_backups(backups, *, keep=7):
    """Only remove snapshots explicitly created by the automatic owner."""
    backups = Path(backups)
    if not backups.is_dir() or backups.is_symlink():
        return
    parent = backups.resolve(strict=True)
    operations = parent.parent / 'operations'
    protected = set()
    if operations.exists():
        if operations.is_symlink() or not operations.is_dir():
            return
        from .vault_operational_recovery import load_vault_recovery_operation, VaultOperationalRecoveryError
        try:
            for path in operations.glob('*.json'):
                operation = load_vault_recovery_operation(operations_root=operations, operation_id=path.stem)
                candidate = Path(operation.snapshot_root).resolve(strict=False)
                if candidate.parent == parent:
                    protected.add(candidate.name)
        except (OSError, ValueError, VaultOperationalRecoveryError):
            return
    owned = []
    for candidate in parent.glob('snap-auto-*'):
        if candidate.is_symlink() or not candidate.is_dir() or candidate.resolve().parent != parent:
            continue
        try:
            meta = _automatic_backup_metadata(candidate)
        except (OSError, ValueError):
            continue
        if meta is not None:
            owned.append((meta.get('created_at', ''), candidate.name, candidate))
    for _, _, candidate in sorted(owned, reverse=True)[keep:]:
        if candidate.name in protected:
            continue
        allowed = {'payload', 'vault-backup-manifest.json', 'backup-verification.json', 'recovery-point.json'}
        entries = {entry.name for entry in candidate.iterdir()}
        if not {'payload', 'vault-backup-manifest.json', 'backup-verification.json'} <= entries or not entries <= allowed:
            continue
        # Recheck immediately before recursive deletion, including nested links.
        if candidate.resolve(strict=True).parent != parent or any(path.is_symlink() for path in candidate.rglob('*')):
            continue
        shutil.rmtree(candidate)
