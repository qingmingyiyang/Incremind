"""Verify the required SQLite rollback set without calculating digests."""

from pathlib import Path
import sqlite3


BACKUP_NAMES = (
    "recognition.sqlite3", "structured-records.sqlite3", "aggregate-authority.sqlite3",
)


def require_backup(backup_dir: Path | None) -> None:
    if backup_dir is None:
        raise RuntimeError("backup_dir_required_for_apply")
    directory = backup_dir.resolve(strict=True)
    for name in BACKUP_NAMES:
        path = directory / name
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"backup_database_missing:{name}")
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError(f"backup_database_invalid:{name}")
