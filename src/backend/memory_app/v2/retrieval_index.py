"""Consume revision-bound text projections; eligibility always stays live."""
import logging
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock, Thread

from core.document_engine.retrieval_index import COLLECTION, PROJECTION_VERSION, project_document
from core.document_engine.runtime import _revision_object_id
from core.storage_provider.source_retrieval_index import (
    COLLECTION as SOURCES, PROJECTION_VERSION as SOURCE_VERSION, index_store, project_source,
    namespace_projection, source_mutation,
    ORIGINALS, ORIGINAL_VERSION, project_original,
)
from core.storage_provider.runtime import _logical_object_id_from_payload_path
from core.storage_provider.sqlite_uow import _schema_ready
from core.storage_provider.connection_scope import borrow_read_connection
from core.storage_provider.observability import observe_connection
from core.search_and_recall.evidence_windows import EvidenceWindow
from ..document_visibility import LegacyDocumentVisibility
from .request_reads import DocumentReadSet

_LOG = logging.getLogger(__name__)


class RetrievalIndex:
    def __init__(self, query, *, read_only=False, source_records=None):
        if read_only and source_records is None:
            raise ValueError('readonly_source_index_required')
        self.query = query
        self.source_records = source_records if source_records is not None else index_store(query.source_store)
        self.read_only = read_only
        self.unavailable = set()
        self._local_unavailable = ContextVar('retrieval_index_local_unavailable', default=None)
        self.lock, self.pending, self.worker = Lock(), set(), None

    @contextmanager
    def readonly_inventory(self):
        """让每次本机检查独立记录不可用的索引条目。"""
        if not self.read_only:
            raise ValueError('readonly_inventory_requires_readonly_index')
        token = self._local_unavailable.set(set())
        try:
            self.bootstrap()
            yield
        finally:
            self._local_unavailable.reset(token)

    def _source_projection(self, identity):
        if not self._source_index_available():
            self.schedule('source', identity)
            return None
        store = self.query.source_store
        row = self.source_records.read(SOURCES, identity)
        payload = namespace_projection(row, store.namespace_id)
        if not payload or payload.get('namespace_id') != store.namespace_id:
            return None
        lifecycle = payload.get('library_lifecycle')
        if isinstance(lifecycle, dict) and lifecycle.get('status') == 'deleted':
            return None
        if (payload.get('state') != 'ready' or payload.get('projection_version') != SOURCE_VERSION
                or payload.get('source_revision') != store.revision('sources', identity)
                or payload.get('incarnation') != store.incarnation('sources', identity)):
            self.schedule('source', identity)
            return None
        return dict(payload)

    def _source_index_available(self):
        if not self.read_only:
            return True
        path = self.source_records.database_path
        if not path.is_file():
            return False
        def open_readonly():
            return observe_connection(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5))
        connection = None
        try:
            connection = borrow_read_connection(path, open_readonly)
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            return _schema_ready(connection)
        except sqlite3.Error:
            return False
        finally:
            if connection is not None:
                connection.close()

    def bootstrap(self):
        if self.read_only:
            self._inventory_readonly()
            return
        # One composition-time rebuild for old stores, never per question.
        for row in self.query.records.list_projected('documents', fields=(
                'id', 'project_id', 'revision')):
            index = self.query.records.read(COLLECTION, row.object_id)
            if index is None:
                self._repair_document(row.object_id)
        for row in self.query.records.list('workspace_items'):
            if self.query.records.read(ORIGINALS, row.object_id) is None:
                self._repair_original(row.object_id)
        directory = self.query.source_store._collection_path('sources')
        if directory.exists():
            for path in directory.glob('*.json'):
                if not path.name.endswith('.meta.json'):
                    identity = _logical_object_id_from_payload_path(path)
                    row = self.source_records.read(SOURCES, identity)
                    projection = namespace_projection(row, self.query.source_store.namespace_id)
                    if projection is None or projection.get('state') != 'ready':
                        self._repair_source(identity)

    @staticmethod
    def _document_projection_valid(payload):
        entry = payload.get('entry')
        return (isinstance(entry, dict) and entry.get('kind') == 'document'
                and entry.get('id') == payload.get('document_id')
                and entry.get('revision') == payload.get('document_revision')
                and isinstance(payload.get('summary'), str) and isinstance(payload.get('spans'), list))

    def _inventory_readonly(self):
        for row in self.query.records.list_projected('documents', fields=('id', 'project_id', 'revision')):
            value = self.query.records.read(COLLECTION, row.object_id)
            if (value is None or value.payload.get('projection_version') != PROJECTION_VERSION
                    or value.payload.get('document_revision') != row.payload.get('revision')
                    or not self._document_projection_valid(value.payload)):
                self.schedule('document', row.object_id)
        for row in self.query.records.list('workspace_items'):
            value = self.query.records.read(ORIGINALS, row.object_id)
            if (value is None or value.payload.get('projection_version') != ORIGINAL_VERSION
                    or value.payload.get('original_revision') != row.revision):
                self.schedule('original', row.object_id)
        directory = self.query.source_store._collection_path('sources')
        if directory.exists():
            for path in directory.glob('*.json'):
                if not path.name.endswith('.meta.json'):
                    identity = _logical_object_id_from_payload_path(path)
                    if self._source_projection(identity) is None:
                        self.schedule('source', identity)

    def _repair_original(self, identity):
        with self.query.records.begin() as tx:
            item = tx.read('workspace_items', identity)
            if item is not None:
                project_original(tx, identity, str(item.payload['project_id']), item.revision,
                                 str(item.payload.get('source_text') or ''), document_id=item.payload.get('document_id'),
                                 previous_document_id=item.payload.get('document_id'))
                tx.commit()

    def _repair_document(self, identity):
        records = self.query.records
        with records.begin() as tx:
            document = tx.read('documents', identity)
            if document is None:
                return
            body = tx.read('document_markdown', _revision_object_id(identity, document.payload['revision']))
            if body and isinstance(body.payload.get('markdown'), str):
                project_document(tx, document.payload, body.payload['markdown'])
                tx.commit()

    def _repair_source(self, identity):
        store = self.query.source_store
        with source_mutation(store, identity) as tx:
            row = tx.read(SOURCES, identity)
            prior = namespace_projection(row, store.namespace_id)
            # A body-before-meta interruption has no committed new version.
            # Never bless that ambiguous body with the still-old metadata.
            if prior and prior.get('state') == 'invalid' and (
                    prior.get('invalidated_revision') == store.revision('sources', identity)
                    and prior.get('invalidated_incarnation') == store.incarnation('sources', identity)):
                return
            value = store.read('sources', identity)
            if value is not None:
                project_source(tx, store, identity, dict(value), store.revision('sources', identity),
                               store.incarnation('sources', identity))
                tx.commit()

    def schedule(self, kind, identity):
        if self.read_only:
            current = self._local_unavailable.get()
            (self.unavailable if current is None else current).add((kind, identity))
            return
        with self.lock:
            self.pending.add((kind, identity))
            if self.worker is None:
                self.worker = Thread(target=self._drain, daemon=True, name='retrieval-index-repair')
                self.worker.start()

    def unavailable_for(self, project):
        """读取原对象的归属，避免从过期索引推断所属项目。"""
        if not self._source_index_available():
            return (('source_index', self.source_records.database_path.name),)
        result = []
        current = self._local_unavailable.get()
        for kind, identity in sorted(self.unavailable if current is None else current):
            if kind == 'source':
                payload = self.query.source_store.read('sources', identity)
                lifecycle = payload.get('library_lifecycle') if payload else None
                if isinstance(lifecycle, dict) and lifecycle.get('status') == 'deleted':
                    continue
                own_project = payload.get('project_id', 'default') if payload else None
            else:
                row = self.query.records.read('documents' if kind == 'document' else 'workspace_items', identity)
                if row and (row.payload.get('status') == 'archived'
                            or kind == 'original' and row.payload.get('status') != 'confirmed'):
                    continue
                own_project = row.payload.get('project_id') if row else None
            if not isinstance(own_project, str) or own_project == project:
                result.append((kind, identity))
        return tuple(result)

    def _drain(self):
        while True:
            with self.lock:
                if not self.pending:
                    self.worker = None
                    return
                kind, identity = self.pending.pop()
            try:
                {'document':self._repair_document, 'source':self._repair_source,
                 'original':self._repair_original}[kind](identity)
            except Exception:
                _LOG.warning('retrieval_index_rebuild_failed')

    def wait_for_repairs(self):
        with self.lock:
            worker = self.worker
        if worker is not None:
            worker.join(timeout=5)

    def prepared(self, project, *, selected=None):
        query = self.query
        document_ids = {selected['id']} if selected and selected['kind'] == 'document' else None
        meta = DocumentReadSet.load(query.records, query.documents, project,
                                    metadata_only=True, document_ids=document_ids)
        projections = {}
        rows = (query.records.list_matching(COLLECTION, project_id=project) if selected is None else
                tuple(row for identity in document_ids or ()
                      if (row := query.records.read(COLLECTION, identity)) is not None))
        for row in rows:
            document = meta.documents.get(row.object_id)
            if (document is None or row.payload.get('projection_version') != PROJECTION_VERSION
                    or row.payload.get('document_revision') != document['revision']
                    or self.read_only and not self._document_projection_valid(row.payload)):
                self.schedule('document', row.object_id)
                continue
            projections[row.object_id] = dict(row.payload)
        for identity in (meta.documents.keys() if selected is None else document_ids or set()) - projections.keys():
            self.schedule('document', identity)
        sources = []
        rows = (() if not self._source_index_available() else self.source_records.list(SOURCES) if selected is None else
                (self.source_records.read(SOURCES, selected['id']),) if selected['kind'] == 'source' else ())
        for row in rows:
            if row is None:
                continue
            value = namespace_projection(row, query.source_store.namespace_id)
            if not value or value.get('project_id') != project:
                continue
            projected = self._source_projection(row.object_id)
            if projected is not None:
                sources.append(projected)
        rows = (query.records.list_matching(ORIGINALS, project_id=project) if selected is None else
                tuple(row for identity in meta.items
                      if (row := query.records.read(ORIGINALS, identity)) is not None)
                if selected['kind'] == 'document' else ())
        originals = {row.object_id:row.payload for row in rows}
        current_originals = {}
        for identity, item in (meta.items.items() if selected is None or selected['kind'] == 'document' else ()):
            original = originals.get(identity)
            if (original and original.get('projection_version') == ORIGINAL_VERSION
                    and original.get('original_revision') == item.revision):
                current_originals[identity] = original
            else:
                self.schedule('original', identity)
        return meta, projections, sources, current_originals

    def indexed_entries(self, project, *, selected=None):
        prepared, projections, sources, originals = self.prepared(project, selected=selected)
        document_ids = {selected['id']} if selected and selected['kind'] == 'document' else None
        visibility = LegacyDocumentVisibility.from_repository(self.query.documents, project_id=project,
                                                               document_ids=document_ids, metadata_only=True)
        confirmed = {str(row.payload['document_id']):row for row in prepared.items.values()
                     if row.payload.get('document_id')}
        entries, linked, archived = [], set(), set()
        for identity, document in prepared.documents.items():
            references = {str(ref['source_id']) for ref in document.get('source_refs', [])
                          if isinstance(ref, dict) and isinstance(ref.get('source_id'), str)}
            if document.get('status') == 'archived':
                archived.update(references)
                continue
            if not visibility.allows(document):
                continue
            linked.update(references)
            projection = projections.get(identity)
            if projection is None:
                continue
            item = confirmed.get(identity)
            entries.append({**projection['entry'], 'item_id':item.object_id if item else None,
                'citation_id':item.object_id if item else identity,
                'item_revision':item.revision if item else None,
                'href':f'#view=rebuild-library-overview&project_id={project}&item_id={identity}&action=inspect'})
        guard = getattr(self.query.source_store, '_guard_source_authority', None)
        if callable(guard):
            guard('sources')
        publisher = getattr(self.query.source_store, '_source_published', None)
        sources.sort(key=lambda value:self.query.source_store._object_paths('sources', value['source_id']).payload_path.name)
        for value in sources:
            identity = value['source_id']
            if (not value['nonempty'] or identity in visibility.pending_sources
                    or (identity in archived and identity not in linked)
                    or (identity in linked and value.get('identity_method') == 'workspace_confirmation')
                    or (callable(publisher) and not publisher('sources', {**value, 'id':identity}))):
                continue
            entries.append({**value['entry'],
                'href':f'#view=rebuild-library-overview&project_id={project}&item_id={identity}&action=inspect'})
        return entries, prepared, projections, sources, originals

    def hydrate(self, project, entry):
        if entry['kind'] == 'document':
            document = self.query.documents.read(entry['id'])
            if document is None:
                return None, None
            markdown = self.query.documents.markdown(entry['id'], revision=document['revision'])
            items = {row.object_id:row for row in self.query.records.list_matching(
                'workspace_items', project_id=project, status='confirmed', document_id=entry['id'])}
            prepared = DocumentReadSet({entry['id']:document}, {entry['id']:markdown}, items)
        else:
            prepared = None
        values = self.query.query_entries(project, selected=[entry], prepared=prepared)
        return next((value for value in values if value['kind'] == entry['kind'] and value['id'] == entry['id']), None), prepared

    def current_entry(self, project, entry):
        entries, _, _, _, _ = self.indexed_entries(project, selected=entry)
        return next((value for value in entries if value['kind'] == entry['kind'] and value['id'] == entry['id']), None)

    def vector_material(self, project, entry, layer):
        """Fresh bindings and exact indexed slices, without a body hydration."""
        entries, _, projections, sources, originals = self.indexed_entries(project, selected=entry)
        current = next((value for value in entries if value['kind'] == entry['kind'] and value['id'] == entry['id']), None)
        if current is None:
            return None
        if entry['kind'] == 'document':
            value = projections.get(entry['id'])
            if value is None or value.get('document_revision') != current['revision']:
                return None
            summary = value['summary'][:120]
            if layer == 'L1':
                chunks = [EvidenceWindow(span['start'] + chunk['start'], span['start'] + chunk['end'], chunk['text'])
                          for span in value['spans'] for chunk in span['chunks']]
                chunks.extend(EvidenceWindow(span['start'], span['end'], value['summary'])
                              for span in value['spans'] if span['layer'] == 'L2')
                length = max((span['end'] for span in value['spans']), default=0)
            elif layer == 'L0' and current.get('item_id'):
                original = originals.get(current['item_id'])
                if original is None or original.get('original_revision') != current['item_revision']:
                    return None
                chunks = [EvidenceWindow(**chunk) for chunk in original['chunks']]
                length = original['length']
            else:
                return None
        elif layer == 'L0':
            source = next((value for value in sources if value['source_id'] == entry['id']), None)
            if source is None or source['source_revision'] != current['revision']:
                return None
            summary, length = '', source['length']
            chunks = [EvidenceWindow(**chunk) for chunk in source['chunks']]
        else:
            return None
        return current, summary, length, tuple(sorted(chunks, key=lambda chunk:(chunk.start, chunk.end)))

    @staticmethod
    def indexed_slice(chunks, start, end):
        pieces, cursor = [], start
        for chunk in chunks:
            if chunk.start <= cursor < chunk.end:
                right = min(end, chunk.end)
                pieces.append(chunk.text[cursor-chunk.start:right-chunk.start])
                cursor = right
                if cursor == end:
                    return ''.join(pieces)
        return None
