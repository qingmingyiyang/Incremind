"""Consistent SQLite backup and restore to an empty destination.

Keys and derived vector caches are intentionally outside the authority backup.
The SQLite online backup API includes committed WAL state.
"""
from pathlib import Path
from contextlib import closing
import argparse
import sqlite3
from datetime import datetime, timezone
import re
from core.storage_provider.vault_backup_restore import (
    verification_metadata, verify_runtime_backup, write_backup_catalog, read_backup_catalog, prune_automatic_backups,
)
from backend.shared.deployment import runtime_backup_roots
from uuid import uuid4


def backup_database(source: Path, destination: Path) -> dict:
    source = source.resolve(strict=True)
    destination = destination.resolve(strict=False)
    if destination.exists() or source == destination:
        raise ValueError("destination must not already exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Refuse clobbering even if another process created it after our check.
    with destination.open("xb"):
        pass
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as origin:
        with closing(sqlite3.connect(destination)) as target:
            origin.backup(target)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("backup verification failed")
            count = target.execute("SELECT count(*) FROM crp_structured_records").fetchone()[0]
    return {"destination": str(destination), "records": count, "credentials_included": False}


def backup_runtime(source: Path, backups: Path, *, user: str | None = None, snapshot_id=None, automatic=False, job_id=None):
    """Reuse the Settings data backup owner, including committed SQLite WAL."""
    from core.storage_provider.vault_backup_restore import create_vault_backup
    source = Path(source)
    if user is not None:
        if not isinstance(user, str) or re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', user) is None:
            raise ValueError('user_invalid')
        root = source.resolve(strict=True)
        source = (root / 'users' / user).resolve(strict=True)
        if not source.is_relative_to(root):
            raise ValueError('user_invalid')
    at = datetime.now(timezone.utc).strftime('%Y%m%dt%H%M%Sz')
    return create_vault_backup(source_root=source, backups_root=Path(backups),
        snapshot_id=snapshot_id or f'backup-{at}-{uuid4().hex[:8]}', sqlite_online=True,
        exclude_logs=True, restore_verify=True, automatic=automatic, job_id=job_id)



def restore_runtime(snapshot: Path, destination: Path):
    """A server restore is stricter than the legacy empty-directory contract."""
    from core.storage_provider.vault_backup_restore import restore_vault_backup
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError('restore destination already exists')
    result = restore_vault_backup(snapshot_root=Path(snapshot), target_root=destination)
    from backend.security.secrets import restrict_server_secret_file, ServerSecretStoreError
    from core.storage_provider.vault_backup_restore import VaultBackupRestoreError
    root = result.target_root
    files = [root / 'secrets.json']
    users = root / 'users'
    try:
        if users.is_symlink():
            raise ServerSecretStoreError('server_secret_file_invalid')
        if users.is_dir():
            for user in users.iterdir():
                if user.is_symlink():
                    raise ServerSecretStoreError('server_secret_file_invalid')
                if user.is_dir():
                    files.append(user / 'secrets.json')
        for path in files:
            if path.exists() or path.is_symlink():
                restrict_server_secret_file(path)
    except (ServerSecretStoreError, OSError):
        raise VaultBackupRestoreError('restored credential permissions unavailable') from None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("backup", "restore"))
    parser.add_argument("source", type=Path, help="existing SQLite authority or backup")
    parser.add_argument("destination", type=Path, help="new SQLite file; existing files are never overwritten")
    args = parser.parse_args()
    print(backup_database(args.source, args.destination))


if __name__ == "__main__":
    main()
