"""Publish one reviewed workspace item as a Source and Document.

The JSON Source is invisible until the SQLite confirmation operation commits.
An interrupted confirmation can resume from its frozen operation record.
"""

from __future__ import annotations

from core.storage_provider.source_retrieval_index import project_original

import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from core.document_engine import SQLiteDocumentRepository
from core.document_engine.ports import DocumentDraft
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore
from backend.security.user_context import json_attribution


COLLECTION = "workspace_confirmation_operations"
ITEMS = "workspace_items"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_payload(item: Mapping[str, object], operation_id: str, source_id: str) -> dict[str, object]:
    kind = str(item["input_kind"])
    source_text = str(item["source_text"])
    item_id = str(item["id"])
    timestamp = _now()
    original_name = str(item.get("original_name") or "")
    media_type = (
        mimetypes.guess_type(original_name)[0] or "application/octet-stream"
        if kind in {"file", "audio", "image", "video"}
        else "text/uri-list" if kind == "link" else "text/plain"
    )
    return {
        "schema_version": "1.2.0", "identity_method": "workspace_confirmation",
        "id": source_id, "type": kind if kind in {"text", "link", "file", "audio", "image", "video"} else "other",
        "title": str(item["title"]), "capture_mode": "snapshot",
        "storage_uri": f"crp://default/sources/{source_id}",
        "original_url": str(item.get("url")) if kind == "link" and item.get("url") else None,
        "content_hash": None, "source_revision": 1,
        "confirmation_operation_id": operation_id, "workspace_item_id": item_id,
        "media_type": media_type, "size_bytes": len(source_text.encode("utf-8")),
        "parser_version": None, "processing_state": "captured", "created_at": timestamp,
        "occurred_at": None, "recorded_at": timestamp, "observed_at": timestamp,
        "imported_from_legacy": False, "trust_status": "user_confirmed",
        "project_id": str(item["project_id"]),
        "metadata": {
            "encoding": "utf-8", "content": source_text, "content_snapshot": source_text,
            "content_read": {"status": "completed", "content_read": True,
                             "char_count": len(source_text),
                             "byte_count": len(source_text.encode("utf-8")),
                             "preview": source_text[:240], "read_ref": None,
                             "error": None, "activity_refs": []},
            "workspace_item_id": item_id, "input_kind": kind,
            "content_kind": item.get("content_kind"),
            "platform": item.get("platform"),
            "original_name": original_name or None,
            "original_download_url": f"/api/workspace/v1/items/{item_id}/original?project_id={item['project_id']}"
            if kind in {"file", "audio", "image", "video"} else None,
        },
    }


def _refs(item: Mapping[str, object], source_id: str) -> list[dict[str, object]]:
    item_id = str(item["id"])
    refs: list[dict[str, object]] = [
        {"source_id": item_id, "locator": "workspace://" + item_id},
        {"source_id": source_id, "locator": f"crp://default/sources/{source_id}"},
    ]
    draft = item["draft"]
    for field in ("facts", "todos"):
        for entry in draft[field]:
            evidence = entry["evidence"]
            refs.append({"source_id": source_id,
                         "locator": f"text:{evidence['start']}:{evidence['end']}",
                         "quote": evidence["quote"]})
    return refs


class WorkspaceConfirmation:
    def __init__(self, runtime_root: Path, records: SQLiteStructuredRecordStore,
                 documents: SQLiteDocumentRepository) -> None:
        self.records = records
        self.documents = documents
        self.source_store = JsonObjectStore(runtime_root / ".rebuild-data",
                                            legacy_root=runtime_root / "library",
                                            namespace_id="default",
                                            mutation_attribution=json_attribution(runtime_root, 'default'),
                                            vector_cache_path=records.database_path.parent / 'recognition-vectors.sqlite3')

    def confirm(
        self, item_id: str, project_id: str, expected_revision: int,
        markdown_for_draft: Callable[[Mapping[str, object]], str],
    ) -> Mapping[str, object]:
        operation_id = "confirm-" + item_id
        with self.records.begin() as tx:
            row = tx.read(ITEMS, item_id)
            if row is None or row.payload.get("project_id") != project_id:
                raise ValueError("item_not_found")
            if row.payload.get("status") == "confirmed":
                reviewed = row.payload.get("reviewed_revision")
                if expected_revision != row.revision and expected_revision != reviewed:
                    raise ValueError("draft_revision_conflict")
                return dict(row.payload)
            if row.payload.get("status") == "ready":
                if row.revision != expected_revision:
                    raise ValueError("draft_revision_conflict")
                if not isinstance(row.payload.get("draft"), Mapping):
                    raise ValueError("draft_not_ready")
                draft = row.payload["draft"]
                source_id = "source-" + item_id
                source = _source_payload(row.payload, operation_id, source_id)
                operation = {
                    "id": operation_id, "state": "pending", "workspace_item_id": item_id,
                    "project_id": project_id, "source_id": source_id, "source_payload": source,
                    "markdown": markdown_for_draft(draft), "draft": draft,
                    "reviewed_revision": row.revision,
                    "source_refs": _refs(row.payload, source_id),
                }
                tx.put(COLLECTION, operation_id, operation, expected_revision=0)
                updated = tx.put(ITEMS, item_id, {**row.payload, "status": "confirming",
                                        "reviewed_revision": row.revision},
                       expected_revision=row.revision)
                project_original(tx, item_id, project_id, updated.revision, str(updated.payload.get('source_text') or ''),
                                 document_id=updated.payload.get('document_id'), previous_document_id=row.payload.get('document_id'))
                tx.commit()
            elif row.payload.get("status") == "confirming":
                operation = tx.read(COLLECTION, operation_id)
                frozen = operation.payload if operation is not None else None
                if (not isinstance(frozen, Mapping) or frozen.get("project_id") != project_id
                        or frozen.get("draft") != row.payload.get("draft")):
                    raise ValueError("confirmation_operation_conflict")
                reviewed = frozen.get("reviewed_revision")
                if (expected_revision != reviewed
                        and not (reviewed is None and expected_revision == row.revision)):
                    raise ValueError("draft_revision_conflict")
            else:
                raise ValueError("draft_not_ready")
        return self.finish(operation_id)

    def finish(self, operation_id: str) -> Mapping[str, object]:
        row = self.records.read(COLLECTION, operation_id)
        if row is None:
            raise ValueError("confirmation_operation_not_found")
        operation = dict(row.payload)
        if operation.get("kind") == "historical_source_backfill":
            return self.finish_backfill(operation_id)
        item_id = str(operation["workspace_item_id"])
        if operation["state"] == "committed":
            item = self.records.read(ITEMS, item_id)
            if item is None or item.payload.get("status") != "confirmed":
                raise ValueError("confirmation_commit_inconsistent")
            return dict(item.payload)
        if operation["state"] != "pending":
            raise ValueError("confirmation_operation_invalid")
        source_id = str(operation["source_id"])
        expected_source = operation["source_payload"]
        existing = self.source_store.read_including_deleted("sources", source_id)
        if existing is not None and dict(existing) != expected_source:
            raise ValueError("confirmation_source_conflict")
        revision = self.source_store.revision("sources", source_id)
        if revision > 1:
            raise ValueError("confirmation_source_revision_conflict")
        if revision == 0:
            self.source_store.write("sources", source_id, expected_source, expected_revision=0)
        if (self.source_store.read_including_deleted("sources", source_id) != expected_source
                or self.source_store.revision("sources", source_id) != 1):
            raise ValueError("confirmation_source_readback_failed")
        with self.records.begin() as tx:
            op_row = tx.read(COLLECTION, operation_id)
            item_row = tx.read(ITEMS, item_id)
            if op_row is None or item_row is None:
                raise ValueError("confirmation_operation_incomplete")
            if op_row.payload.get("state") == "committed":
                return dict(item_row.payload)
            if (op_row.payload != operation or item_row.payload.get("status") != "confirming"
                    or item_row.payload.get("project_id") != operation["project_id"]):
                raise ValueError("confirmation_operation_conflict")
            draft = operation["draft"]
            repository = SQLiteDocumentRepository(self.records, namespace_id=self.documents.namespace_id, now=_now())
            document = repository.create_or_replay_generated_in_uow(
                DocumentDraft(title=draft["title"], document_type=item_id,
                              markdown=operation["markdown"],
                              source_refs=tuple(operation["source_refs"]),
                              project_id=operation["project_id"]), tx)
            item = {**item_row.payload, "status": "confirmed", "source_id": source_id,
                    "document_id": document["id"]}
            updated = tx.put(ITEMS, item_id, item, expected_revision=item_row.revision)
            project_original(tx, item_id, str(item['project_id']), updated.revision, str(item.get('source_text') or ''),
                             document_id=item.get('document_id'), previous_document_id=item_row.payload.get('document_id'))
            tx.put(COLLECTION, operation_id,
                   {**operation, "state": "committed", "document_id": document["id"]},
                   expected_revision=op_row.revision)
            tx.commit()
            return item

    def backfill_confirmed_source(self, item_id: str, project_id: str) -> Mapping[str, object]:
        """Attach a Source to a previously confirmed item without editing its Document."""
        operation_id = "backfill-" + item_id
        with self.records.begin() as tx:
            row = tx.read(ITEMS, item_id)
            if row is None or row.payload.get("project_id") != project_id:
                raise ValueError("item_not_found")
            if row.payload.get("status") != "confirmed" or not row.payload.get("document_id"):
                raise ValueError("historical_item_not_confirmed")
            document_row = tx.read("documents", str(row.payload["document_id"]))
            document = document_row.payload if document_row is not None else None
            if (document is None or document.get("project_id") != project_id
                    or not any(ref.get("locator") == "workspace://" + item_id
                               for ref in document.get("source_refs", []) if isinstance(ref, Mapping))):
                raise ValueError("historical_document_mismatch")
            source_id = "source-" + item_id
            if row.payload.get("source_id"):
                if row.payload["source_id"] != source_id:
                    raise ValueError("historical_source_conflict")
                source = self.source_store.read_including_deleted("sources", source_id)
                if (source is None or source.get("workspace_item_id") != item_id
                        or source.get("project_id") != project_id):
                    raise ValueError("historical_source_missing")
                return dict(row.payload)
            existing = tx.read(COLLECTION, operation_id)
            if existing is None:
                operation = {
                    "id": operation_id, "kind": "historical_source_backfill",
                    "state": "pending", "workspace_item_id": item_id,
                    "project_id": project_id, "document_id": row.payload["document_id"],
                    "source_id": source_id,
                    "source_payload": _source_payload(row.payload, operation_id, source_id),
                }
                tx.put(COLLECTION, operation_id, operation, expected_revision=0)
                tx.commit()
            elif existing.payload.get("kind") != "historical_source_backfill":
                raise ValueError("historical_operation_conflict")
        return self.finish_backfill(operation_id)

    def finish_backfill(self, operation_id: str) -> Mapping[str, object]:
        operation_row = self.records.read(COLLECTION, operation_id)
        if operation_row is None or operation_row.payload.get("kind") != "historical_source_backfill":
            raise ValueError("historical_operation_not_found")
        operation = dict(operation_row.payload)
        item_id = str(operation["workspace_item_id"])
        source_id = str(operation["source_id"])
        if operation.get("state") == "committed":
            item = self.records.read(ITEMS, item_id)
            if (item is None or item.payload.get("source_id") != source_id
                    or self.source_store.read_including_deleted("sources", source_id) != operation["source_payload"]):
                raise ValueError("historical_commit_inconsistent")
            return dict(item.payload)
        if operation.get("state") != "pending":
            raise ValueError("historical_operation_invalid")
        expected_source = operation["source_payload"]
        existing = self.source_store.read_including_deleted("sources", source_id)
        if existing is not None and dict(existing) != expected_source:
            raise ValueError("historical_source_conflict")
        revision = self.source_store.revision("sources", source_id)
        if revision > 1:
            raise ValueError("historical_source_revision_conflict")
        if revision == 0:
            self.source_store.write("sources", source_id, expected_source, expected_revision=0)
        if (self.source_store.read_including_deleted("sources", source_id) != expected_source
                or self.source_store.revision("sources", source_id) != 1):
            raise ValueError("historical_source_readback_failed")
        with self.records.begin() as tx:
            op_row = tx.read(COLLECTION, operation_id)
            item_row = tx.read(ITEMS, item_id)
            if op_row is None or item_row is None:
                raise ValueError("historical_operation_incomplete")
            if op_row.payload.get("state") == "committed":
                return dict(item_row.payload)
            if (op_row.payload != operation or item_row.payload.get("status") != "confirmed"
                    or item_row.payload.get("project_id") != operation["project_id"]
                    or item_row.payload.get("document_id") != operation["document_id"]
                    or item_row.payload.get("source_id")):
                raise ValueError("historical_operation_conflict")
            item = {**item_row.payload, "source_id": source_id}
            updated = tx.put(ITEMS, item_id, item, expected_revision=item_row.revision)
            project_original(tx, item_id, str(item['project_id']), updated.revision, str(item.get('source_text') or ''),
                             document_id=item.get('document_id'), previous_document_id=item_row.payload.get('document_id'))
            tx.put(COLLECTION, operation_id, {**operation, "state": "committed"},
                   expected_revision=op_row.revision)
            tx.commit()
            return item

    def recover_pending(self) -> tuple[str, ...]:
        failures = []
        for row in self.records.list(COLLECTION):
            if row.payload.get("state") != "pending":
                continue
            try:
                self.finish(row.object_id)
            except Exception:
                failures.append(row.object_id)
        return tuple(failures)
