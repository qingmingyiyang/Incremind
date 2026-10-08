"""Online-safe SQLite backup immediately before a shared-authority cutover."""

import argparse
from pathlib import Path
import json
import sqlite3

parser = argparse.ArgumentParser()
parser.add_argument("--runtime-root", required=True, type=Path)
parser.add_argument("--backup-dir", required=True, type=Path)
args = parser.parse_args()
root = args.runtime_root.resolve(strict=True)
dest = args.backup_dir.resolve(strict=False)
if dest == root or root in dest.parents:
    raise RuntimeError("backup_must_be_outside_runtime_root")
if dest.exists() and any(dest.iterdir()):
    raise RuntimeError("backup_directory_must_be_empty")
dest.mkdir(parents=True, exist_ok=True)
sources = [
    root / "recognition.sqlite3",
    root / ".rebuild-data" / "structured-records.sqlite3",
    root / ".rebuild-data" / "aggregate-authority.sqlite3",
]
out = []
for source_path in sources:
    if not source_path.is_file():
        raise RuntimeError(f"missing_database:{source_path.name}")
    target_path = dest / source_path.name
    if target_path.exists():
        raise RuntimeError(f"backup_target_exists:{source_path.name}")
    with sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True) as source:
        with sqlite3.connect(target_path) as target:
            source.backup(target)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError(f"backup_integrity_failed:{source_path.name}")
    out.append({"name": source_path.name, "size": target_path.stat().st_size})
print(json.dumps({"backup": str(dest), "databases": out}))
