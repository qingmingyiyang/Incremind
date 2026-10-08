"""Document serialization ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import sqlite3

from backend.api.job_runtime import build_rebuild_job_repository as _job_repository
from backend.api.task_reference_projection import workbench_transform_task_ref_for_document

from core.document_engine import DocumentRepositoryPort
from core.product_core.workbench_content_transform_execution import read_workbench_transform_receipt


def _serialize_document_detail(
    document: Mapping[str, object],
    markdown: str,
    *,
    status: str = "document_ready",
    source_task_ref: str | None = None,
) -> Mapping[str, object]:
    document_id = str(document.get("id") or "")
    source_refs = document.get("source_refs")
    blocks = document.get("blocks")
    payload = {
        "status": status,
        "document_id": document_id,
        "title": str(document.get("title") or document_id),
        "document_type": str(document.get("type") or "document"),
        "project_id": document.get("project_id"),
        "document_status": str(document.get("status") or "draft"),
        "revision": document.get("revision"),
        "updated_at": document.get("updated_at"),
        "markdown": markdown,
        "blocks": blocks if isinstance(blocks, list) else [],
        "source_refs": source_refs if isinstance(source_refs, list) else [],
        "trace_refs": [f"crp://default/documents/{document_id}.json"],
        "memory_publication_state": "not_published",
        "blocked_operations": ["auto_memory_publication", "document_overwrite_without_revision"],
    }
    if source_task_ref:
        payload["source_task_ref"] = source_task_ref
    return payload


def _document_source_task_ref(
    runtime_root: Path,
    store: object,
    document: Mapping[str, object],
    documents: DocumentRepositoryPort,
) -> str | None:
    """Resolve a Document's source TaskReference only from verified evidence."""
    try:
        jobs = _job_repository(runtime_root, store)
        return workbench_transform_task_ref_for_document(
            document=document,
            object_store=store,
            jobs=jobs.list_jobs(job_type="workbench_content_transform"),
            receipt_reader=lambda job_id: read_workbench_transform_receipt(jobs.sqlite.database_path, job_id),
            document_revision_reader=documents.revision,
            document_markdown_reader=lambda document_id, revision: documents.markdown(
                document_id, revision=revision,
            ),
        )
    except (OSError, ValueError, sqlite3.DatabaseError):
        return None
