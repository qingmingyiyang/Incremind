"""Fail-closed derived Source text in the existing structured-record database."""
from .sqlite_uow import SQLiteStructuredRecordStore
from core.search_and_recall.evidence_windows import split_evidence_chunks
from core.search_and_recall.vector_cache_invalidation import VectorCacheInvalidator, vector_cache_path
from contextlib import contextmanager
from uuid import uuid4

COLLECTION = 'source_retrieval_index'
PROJECTION_VERSION = 'source-search@1'
ORIGINALS = 'original_text_retrieval_index'
ORIGINAL_VERSION = 'original-search@1'


def index_store(store):
    return SQLiteStructuredRecordStore(store.root / 'structured-records.sqlite3')


def namespace_projection(row, namespace):
    return row.payload.get('projections', {}).get(namespace) if row else None


def _invalidate_vectors(tx, store, identity, projects):
    path = getattr(store, 'vector_cache_path', None)
    invalidator = VectorCacheInvalidator(path if path is not None else vector_cache_path(tx))
    for project in sorted({value for value in projects if isinstance(value, str) and value}):
        invalidator.material(project, 'source', identity)


@contextmanager
def source_mutation(store, identity):
    # Source authority already owns SQLite before taking the JSON object lock.
    with index_store(store).begin() as tx, store.locked('sources', identity):
        yield tx


def invalidate_source(tx, store, identity, project_id=None):
    # Commit before touching either JSON file. A payload/meta crash can never
    # make the old projection eligible merely because its old revision matches.
    current = tx.read(COLLECTION, identity)
    values = dict(current.payload.get('projections', {})) if current else {}
    prior = values.get(store.namespace_id, {})
    _invalidate_vectors(tx, store, identity, (prior.get('project_id'), project_id))
    token = uuid4().hex
    values[store.namespace_id] = {**prior, 'state':'invalid', 'source_id':identity,
               'namespace_id':store.namespace_id, 'invalidation_id':token,
               'project_id':project_id if project_id is not None else prior.get('project_id'),
               'invalidated_revision':store.revision('sources', identity),
               'invalidated_incarnation':store.incarnation('sources', identity)}
    tx.put(COLLECTION, identity, {'projections':values},
           expected_revision=current.revision if current else 0)
    tx.commit()
    return token


def project_source(tx, store, identity, payload, revision, incarnation):
    metadata = payload.get('metadata')
    content = (metadata.get('content_snapshot') or metadata.get('content') or '') if isinstance(metadata, dict) else ''
    content = content if isinstance(content, str) else ''
    projected = {'state':'ready', 'source_id':identity, 'source_revision':revision,
                 'namespace_id':store.namespace_id,
                 'incarnation':incarnation, 'projection_version':PROJECTION_VERSION,
                 'project_id':payload.get('project_id', 'default'),
                 'entry': {'kind':'source', 'id':identity, 'source_id':identity,
                           'title':str(payload.get('title') or identity), 'revision':revision},
                 'length':len(content), 'nonempty':bool(content.strip()),
                 'chunks':[{'start':w.start, 'end':w.end, 'text':w.text}
                           for w in split_evidence_chunks(content)],
                 'created_at':payload.get('created_at'),
                 'library_lifecycle':payload.get('library_lifecycle'),
                 'identity_method':payload.get('identity_method'),
                 'confirmation_operation_id':payload.get('confirmation_operation_id'),
                 'workspace_item_id':payload.get('workspace_item_id')}
    current = tx.read(COLLECTION, identity)
    values = dict(current.payload.get('projections', {})) if current else {}
    prior = values.get(store.namespace_id, {})
    if prior != projected:
        _invalidate_vectors(tx, store, identity, (prior.get('project_id'), projected['project_id']))
    values[store.namespace_id] = projected
    tx.put(COLLECTION, identity, {'projections':values},
           expected_revision=current.revision if current else 0)


def refresh_source(store, identity, revision, incarnation, token):
    # Release the physical write lock before opening SQLite again. A newer
    # invalidation, including an interrupted write, must win over this refresh.
    with source_mutation(store, identity) as tx:
        prior = namespace_projection(tx.read(COLLECTION, identity), store.namespace_id)
        if (not prior or prior.get('invalidation_id') != token
                or store.revision('sources', identity) != revision
                or store.incarnation('sources', identity) != incarnation):
            return
        payload = store.read_including_deleted('sources', identity)
        if payload is not None:
            project_source(tx, store, identity, payload, revision, incarnation)
            tx.commit()


def project_original(tx, identity, project, revision, text, *, document_id=None, previous_document_id=None):
    """Project explicit original text parameters; the domain owner supplies them."""
    current = tx.read(ORIGINALS, identity)
    value = {'id':identity, 'project_id':project, 'original_revision':revision,
             'projection_version':ORIGINAL_VERSION, 'length':len(text),
             'chunks':[{'start':w.start, 'end':w.end, 'text':w.text}
                       for w in split_evidence_chunks(text)]}
    if current is None or current.payload != value:
        invalidator = VectorCacheInvalidator(vector_cache_path(tx))
        bindings = {(project, document_id)}
        if current is not None:
            bindings.add((current.payload['project_id'], previous_document_id))
        for own_project, document in sorted((own_project, document) for own_project, document in bindings
                                            if isinstance(document, str) and document):
            invalidator.material(own_project, 'document', document)
        tx.put(ORIGINALS, identity, value, expected_revision=current.revision if current else 0)
