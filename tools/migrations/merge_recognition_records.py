"""Offline, exact-record rehearsal of recognition -> old structured SQLite.

This does not activate Document authority. Stop every application writer first.
It preserves record revisions and target markers.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from backup_guard import require_backup


QUERY = "SELECT collection, object_id, payload_json, revision FROM crp_structured_records"


def readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect("file:" + path.resolve(strict=True).as_posix() + "?mode=ro", uri=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    if args.apply:
        require_backup(args.backup_dir)
    root = args.runtime_root.resolve(strict=True)
    source_path = root / "recognition.sqlite3"
    target_path = root / ".rebuild-data" / "structured-records.sqlite3"
    for collection in ("documents", "document_revisions", "document_markdown"):
        if list((root / ".rebuild-data" / "objects" / "default" / collection).glob("*.json")):
            raise RuntimeError(f"old_json_document_records_present:{collection}")

    with readonly(source_path) as source, sqlite3.connect(target_path) as target:
        if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("source_database_check_failed")
        if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("target_database_check_failed")
        incoming = {(collection, object_id): (payload_json, revision)
                    for collection, object_id, payload_json, revision in source.execute(QUERY)}
        existing = {(collection, object_id): (payload_json, revision)
                    for collection, object_id, payload_json, revision in target.execute(QUERY)}
        collisions = [key for key, value in incoming.items() if key in existing and existing[key] != value]
        if collisions:
            raise RuntimeError(f"record_conflicts:{len(collisions)}")
        missing = [(collection, object_id, *value) for (collection, object_id), value in incoming.items()
                   if (collection, object_id) not in existing]
        if args.apply:
            target.execute("BEGIN IMMEDIATE")
            try:
                target.executemany(
                    "INSERT INTO crp_structured_records(collection,object_id,payload_json,revision) VALUES(?,?,?,?)",
                    missing,
                )
                target.commit()
            except Exception:
                target.rollback()
                raise
            copied = {(collection, object_id): (payload_json, revision)
                      for collection, object_id, payload_json, revision in target.execute(QUERY)}
            if any(copied.get(key) != value for key, value in incoming.items()):
                raise RuntimeError("readback_mismatch")
            if any(copied.get(key) != value for key, value in existing.items()):
                raise RuntimeError("old_target_record_changed")
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("target_database_check_after_merge_failed")
    print(json.dumps({"apply": args.apply, "source_records": len(incoming),
                      "target_records_before": len(existing), "already_identical": len(incoming) - len(missing),
                      "new_records": len(missing)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
