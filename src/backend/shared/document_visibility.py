from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore

if TYPE_CHECKING:
    from backend.recognition import WorkScope


@dataclass(frozen=True, slots=True)
class LegacyDocumentVisibility:
    """Apply recognition publication state to the shared legacy Document view."""

    confirmed: frozenset[tuple[str, str]]
    completed: frozenset[tuple[str, str]]
    pending_sources: frozenset[str] = frozenset()
    filed: frozenset[tuple[str, str]] = frozenset()

    @classmethod
    def from_repository(cls, documents: object, *, project_id: str | None = None,
                        document_ids: set[str] | None = None,
                        metadata_only: bool = False) -> LegacyDocumentVisibility:
        from backend.recognition import RecognitionError, WorkScope
        from backend.recognition.document_filings import filing_document_candidate, filing_experience, DocumentFilingError

        if isinstance(documents, SQLiteDocumentRepository):
            records = documents.records
        else:
            object_root = getattr(getattr(documents, "object_store", None), "root", None)
            database = Path(object_root) / "structured-records.sqlite3" if object_root is not None else None
            if database is None or not database.is_file():
                return cls(frozenset(), frozenset())
            records = SQLiteStructuredRecordStore(database)
        def matching(collection, projected, **fields):
            if project_id is not None:
                fields["project_id"] = project_id
            reader = (lambda **values: records.list_projected(collection, fields=projected, **values)
                      if metadata_only else records.list_matching(collection, **values))
            if document_ids is None:
                return reader(**fields)
            return (row for identity in document_ids
                    for row in reader(document_id=identity, **fields))

        confirmed = frozenset(
            (str(row.payload.get("project_id")), str(row.payload.get("document_id")))
            for row in matching("workspace_items", ('project_id', 'document_id', 'status'), status="confirmed")
            if row.payload.get("status") == "confirmed" and row.payload.get("document_id")
        )
        completed = frozenset(
            (str(row.payload.get("project_id")), str(row.payload.get("document_id")))
            for row in matching("recognition_tasks", ('project_id', 'document_id', 'state', 'kind'), state="completed")
            if row.payload.get("state") == "completed"
            and row.payload.get("kind") != "restructure"
            and row.payload.get("document_id")
        )
        completed |= frozenset(_task_deliveries(records, project_id=project_id, document_ids=document_ids,
                                               metadata_only=metadata_only))
        pending_sources = frozenset(
            str(row.payload["source_id"])
            for row in (records.list_projected('workspace_review_intents', fields=('source_id', 'state'))
                        if metadata_only else records.list("workspace_review_intents"))
            if row.payload.get("state") != "confirmed" and isinstance(row.payload.get("source_id"), str)
        )
        filed = set()
        markers = ((row for identity in sorted(document_ids)
                    if (row := records.read('v2_document_filings', identity)) is not None)
                   if document_ids is not None else
                   records.list_matching('v2_document_filings', target_project_id=project_id)
                   if project_id is not None else records.list('v2_document_filings'))
        for marker in markers:
            project = marker.payload.get('target_project_id')
            if (not isinstance(project, str) or (project_id is not None and project != project_id)
                    or (document_ids is not None and marker.object_id not in document_ids)):
                continue
            scope = WorkScope('local-user', project)
            if metadata_only:
                document = records.read_projected('documents', marker.object_id,
                    fields=('id', 'project_id', 'revision', 'status'))
                visible = filing_document_candidate(marker, scope, document)
            else:
                try:
                    visible = filing_experience(records, scope, marker.object_id) is not None
                except (RecognitionError, DocumentFilingError):
                    visible = False
            if visible:
                filed.add((project, marker.object_id))
        return cls(confirmed, completed, pending_sources, frozenset(filed))

    def allows(self, document: Mapping[str, object]) -> bool:
        from backend.recognition.document_filings import FILED_ID

        document_id = str(document.get("id") or "")
        project_id = str(document.get("project_id"))
        document_type = document.get("type")
        refs = document.get("source_refs")
        if isinstance(refs, list) and any(
            isinstance(ref, Mapping) and ref.get("source_id") in self.pending_sources
            for ref in refs
        ):
            return False
        if FILED_ID.fullmatch(document_id):
            return (project_id, document_id) in self.filed
        workspace_ref = isinstance(refs, list) and any(
            isinstance(ref, Mapping) and str(ref.get("locator") or "").startswith("workspace://")
            for ref in refs
        )
        if document_type == "restructure-internal":
            return False
        if document_type == "agent-result" or (isinstance(document_type, str) and document_type.startswith('agent-result-turn-')):
            return (project_id, document_id) in self.completed
        if workspace_ref:
            return (project_id, document_id) in self.confirmed
        return True


def recognition_document_visible(records: SQLiteStructuredRecordStore, scope: WorkScope, document_id: str) -> bool:
    """Admit a current document backed by a confirmed review or completed task.

    Recognition only admits explicitly published results. Legacy library views
    also include historical documents and use LegacyDocumentVisibility instead.
    """
    from backend.recognition import RecognitionError
    from backend.recognition.document_filings import filing_experience, DocumentFilingError

    document = records.read("documents", document_id)
    if (document is None or document.payload.get("project_id") != scope.project_id
            or document.payload.get("status") == "archived"):
        return False
    try:
        copied = filing_experience(records, scope, document_id)
    except (RecognitionError, DocumentFilingError):
        return False
    if copied is not None:
        return True
    identity = {"document_id": document_id}
    if isinstance(scope.project_id, str):
        identity["project_id"] = scope.project_id

    def matching(collection, **fields):
        # WorkScope(None) is the supported personal scope; list_matching only
        # accepts string predicates, so check null ownership after narrowing.
        return (row for row in records.list_matching(collection, **identity, **fields)
                if row.payload.get("project_id") == scope.project_id)

    if any(matching("workspace_items", status="confirmed")):
        return True
    if any(matching("workspace_review_intents", state="confirmed")):
        return True
    if isinstance(scope.project_id, str) and (scope.project_id, document_id) in _task_deliveries(records, project_id=scope.project_id, document_ids={document_id}):
        return True
    return any(record.payload.get("kind") != "restructure"
               for record in matching("recognition_tasks", state="completed"))


def _task_deliveries(records, *, project_id=None, document_ids=None, metadata_only=False):
    rows = (records.list_projected('v2_turns', fields=('project_id', 'receipt.do.state',
            'receipt.do.document_id', 'receipt.do.kernel_turn_id'),
            **({'project_id':project_id} if isinstance(project_id, str) else {})) if metadata_only else
            records.list_matching('v2_turns', project_id=project_id)
            if isinstance(project_id, str) else records.list('v2_turns'))
    for turn in rows:
        project = turn.payload.get('project_id')
        task = turn.payload.get('receipt', {}).get('do', {})
        document_id, kernel_id = task.get('document_id'), task.get('kernel_turn_id')
        if (task.get('state') not in {'done', 'partial'} or not isinstance(project, str)
                or not isinstance(kernel_id, str) or not isinstance(document_id, str)
                or (document_ids is not None and document_id not in document_ids)):
            continue
        creation = (records.read_projected('v2_task_draft_operations', 'deliver-' + kernel_id,
                    fields=('inputs.project_id', 'inputs.turn_id', 'result.document_id')) if metadata_only else
                    records.read('v2_task_draft_operations', 'deliver-' + kernel_id))
        if (creation is not None and creation.payload.get('inputs', {}).get('project_id') == project
                and creation.payload.get('inputs', {}).get('turn_id') == kernel_id
                and creation.payload.get('result', {}).get('document_id') == document_id):
            yield project, document_id
