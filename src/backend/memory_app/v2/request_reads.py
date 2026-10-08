"""Detached content for one assembly pass, never an authorization reader."""
from dataclasses import dataclass

from core.document_engine import SQLiteDocumentRepository
from core.document_engine.runtime import DocumentRepositoryError, _revision_object_id


@dataclass(frozen=True)
class DocumentReadSet:
    documents: dict
    markdown: dict
    items: dict

    @classmethod
    def load(cls, records, repository, project, *, metadata_only=False, document_ids=None):
        # Bodies are immutable revisions. Load only this project's current
        # bodies after discovering their IDs; release the lease before use.
        if isinstance(repository, SQLiteDocumentRepository) and repository.records is records:
            fields = ('id', 'project_id', 'revision', 'title', 'type', 'status', 'source_refs', 'created_at', 'updated_at')
            rows = ((records.list_projected('documents', fields=fields, project_id=project)
                     if document_ids is None else (row for identity in document_ids
                     for row in records.list_projected('documents', fields=fields, id=identity, project_id=project)))
                    if metadata_only else records.list_matching('documents', project_id=project))
            documents = {row.object_id: dict(row.payload) for row in rows}
            if metadata_only:
                fields = ('id', 'project_id', 'status', 'document_id', 'source_id', 'title', 'created_at')
                rows = (records.list_projected('workspace_items', fields=fields, project_id=project, status='confirmed')
                        if document_ids is None else (row for identity in document_ids for row in
                        records.list_projected('workspace_items', fields=fields, project_id=project,
                                               status='confirmed', document_id=identity)))
                items = {row.object_id:row for row in rows}
                return cls(documents, {}, items)
            keys = {identity: _revision_object_id(identity, document['revision'])
                    for identity, document in documents.items()}
            batch = records.read_batch({'document_markdown': tuple(keys.values())})
            bodies = {row.object_id: row.payload.get('markdown') for row in batch['document_markdown']}
            if any(not isinstance(value, str) for value in bodies.values()):
                raise DocumentRepositoryError('stored markdown payload must contain markdown')
            markdown = {identity: bodies.get(key) for identity, key in keys.items()}
        else:
            documents = {document['id']: document for document in repository.list(include_archived=True)
                         if document.get('project_id') == project}
            markdown = {identity: repository.markdown(identity, revision=document['revision'])
                        for identity, document in documents.items()}
        items = {row.object_id: row for row in records.list_matching(
            'workspace_items', project_id=project, status='confirmed')}
        return cls(documents, markdown, items)
