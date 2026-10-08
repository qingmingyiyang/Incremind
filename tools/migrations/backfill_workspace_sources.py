"""Attach Source identities to confirmed pre-cutover workspace items."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backup_guard import require_backup
from backend.memory_app.workspace_confirmation import WorkspaceConfirmation
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.runtime_root.resolve(strict=True)
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "structured-records.sqlite3")
    documents = SQLiteDocumentRepository(records, namespace_id="default")
    items = [row for row in records.list("workspace_items")
             if row.payload.get("status") == "confirmed" and not row.payload.get("source_id")]
    service = WorkspaceConfirmation(root, records, documents)
    # Validate every link before the first write. The service validates again at commit.
    for row in items:
        item = row.payload
        document = documents.read(str(item.get("document_id") or ""))
        if (document is None or document.get("project_id") != item.get("project_id")
                or not any(ref.get("locator") == "workspace://" + row.object_id
                           for ref in document.get("source_refs", []) if isinstance(ref, dict))):
            raise RuntimeError(f"historical_document_mismatch:{row.object_id}")
    if not args.apply:
        print(json.dumps({"mode": "preview", "pending": len(items),
                          "items": [row.object_id for row in items]}))
        return
    require_backup(args.backup_dir)
    updated = [service.backfill_confirmed_source(row.object_id, str(row.payload["project_id"]))
               for row in items]
    print(json.dumps({"mode": "apply", "updated": len(updated),
                      "remaining": sum(not row.payload.get("source_id") for row in records.list("workspace_items")
                                       if row.payload.get("status") == "confirmed")}))


if __name__ == "__main__":
    main()
