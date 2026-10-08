"""Scoped status projection that excludes recognition task/document bodies.

Task execution keeps its prompt in the task record and compiled messages in
the context packet while it is migrated to the preserved Turn runtime. Callers
that only need progress must not receive that input or document Markdown.
This module therefore projects a fixed whitelist and verifies the linked
document in the same project before exposing its identifier.
"""

from __future__ import annotations

from collections.abc import Mapping

from backend.recognition import RecognitionConflict, WorkScope
from backend.recognition.restructuring import RestructureProposalService
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore


_TASKS = "recognition_tasks"
_UNAVAILABLE = "task is unavailable in this project"


def get_task_status(
    records: SQLiteStructuredRecordStore,
    scope: WorkScope,
    task_id: str,
    *, document_namespace: str = "recognition",
) -> Mapping[str, object]:
    """Return one project-scoped task status without task or document bodies.

    A missing task, a task from another project, and an invalid linked document
    intentionally share the same conflict.  This prevents the endpoint using
    this projection from becoming an object-existence or cross-project
    document oracle.
    """

    task = records.read(_TASKS, task_id)
    if task is None or task.payload.get("project_id") != scope.project_id:
        raise RecognitionConflict(_UNAVAILABLE)

    internal_state = task.payload.get("state")
    state = "running" if internal_state == "result_ready" else internal_state
    # A document produced during the fenced capability is deliberately hidden
    # until the old Turn reaches a completed terminal receipt.  This prevents
    # a cancelled or merely result-ready task leaking a draft through polling.
    document_id = task.payload.get("document_id") if state == "completed" and task.payload.get("kind") != "restructure" else None
    document_revision: int | None = None
    if document_id is not None:
        if not isinstance(document_id, str) or not document_id:
            raise RecognitionConflict(_UNAVAILABLE)
        document = SQLiteDocumentRepository(records, namespace_id=document_namespace).read(document_id)
        if document is None or document.get("project_id") != scope.project_id:
            raise RecognitionConflict(_UNAVAILABLE)
        revision = document.get("revision")
        if not isinstance(revision, int) or revision < 1:
            raise RecognitionConflict(_UNAVAILABLE)
        document_revision = revision

    proposal_id = None
    if state == "completed" and task.payload.get("kind") == "restructure":
        proposal = RestructureProposalService(records).get(scope=scope, proposal_id=task.payload.get("proposal_id"))
        if proposal.get("origin_task_id") != task.object_id:
            raise RecognitionConflict(_UNAVAILABLE)
        proposal_id = proposal["id"]

    return {
        "task_id": task.object_id,
        "status": state,
        "title": task.payload.get("title", task.object_id),
        "context_packet_id": task.payload.get("context_packet_id"),
        "document_id": document_id,
        "document_revision": document_revision,
        **({"kind": "restructure", "proposal_id": proposal_id} if task.payload.get("kind") == "restructure" else {}),
        "created_at": task.payload.get("created_at"),
        "finished_at": task.payload.get("finished_at"),
        "revision": task.revision,
        **({"turn_id": task.payload.get("turn_id")}
           if isinstance(task.payload.get("turn_id"), str) and task.payload.get("turn_id") else {}),
        **({"approval_event_id": task.payload.get("approval_event_id"),
            "approval_sequence": task.payload.get("approval_sequence")}
           if state == "waiting_approval" else {}),
    }
