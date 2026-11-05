import json
from contextlib import nullcontext
from types import SimpleNamespace

from backend.recognition import RecognitionConflict, WorkScope
from backend.recognition.sql_source_identities import CollectingReader
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider.record_lineage import HEAD_COLLECTIONS, WITNESS_COLLECTIONS, FACTS, verify_lineage, RecordLineageError
from core.storage_provider.sqlite_uow import begin_read
from ..original_sources import resolve_turn_material
from ..source_egress import SourceEgressService
from ..transaction_records import TransactionRecords


FIELDS = {'namespace', 'collection', 'object_id', 'revision', 'fact_id', 'origin', 'observed_revision'}


class MaterialSQLCollector(CollectingReader):
    def read(self, collection, object_id):
        row = super().read(collection, object_id)
        if row is not None and collection in HEAD_COLLECTIONS:
            self._selected.add((collection, object_id))
        return row


def validate_sql_shape(identities):
    if not isinstance(identities, list):
        raise RecognitionConflict('material_origin_sql_identity_changed')
    for frozen in identities:
        if (not isinstance(frozen, dict) or set(frozen) != FIELDS
                or any(not isinstance(frozen[field], str) or not frozen[field]
                       for field in ('namespace', 'collection', 'object_id', 'fact_id', 'origin'))
                or frozen['collection'] not in HEAD_COLLECTIONS
                or type(frozen['revision']) is not int or frozen['revision'] < 1
                or frozen['origin'] not in {'created', 'current_anchor'}
                or type(frozen['observed_revision']) is not int or frozen['observed_revision'] < 1):
            raise RecognitionConflict('material_origin_sql_identity_changed')


class _CommitMetadataReader:
    def __init__(self, connection, validate):
        self._connection, self._validate = connection, validate

    def read(self, collection, identity):
        self._validate()
        if collection in HEAD_COLLECTIONS:
            row = self._connection.execute(
                'SELECT revision FROM crp_structured_records WHERE collection = ? AND object_id = ?',
                (collection, identity)).fetchone()
            result = None if row is None else SimpleNamespace(revision=row[0], payload={})
        elif collection in {*HEAD_COLLECTIONS.values(), *WITNESS_COLLECTIONS.values(), FACTS}:
            row = self._connection.execute(
                'SELECT revision, payload_json FROM crp_structured_records WHERE collection = ? AND object_id = ?',
                (collection, identity)).fetchone()
            if row is not None and len(row[1]) > 4096:
                raise RecognitionConflict('material_origin_sql_identity_changed')
            result = None if row is None else SimpleNamespace(revision=row[0], payload=json.loads(row[1]))
        else:
            raise RecognitionConflict('material_origin_sql_identity_unavailable')
        self._validate()
        return result


def _validate_commit_sql(identities, readers, validate):
    validate_sql_shape(identities)
    seen = set()
    for frozen in identities:
        validate()
        key = frozen['namespace'], frozen['collection'], frozen['object_id']
        if frozen['namespace'] not in readers or key in seen:
            raise RecognitionConflict('material_origin_sql_identity_owner_changed')
        seen.add(key)
        if _identity(_CommitMetadataReader(readers[frozen['namespace']], validate), *key) != frozen:
            raise RecognitionConflict('material_origin_sql_identity_changed')
    validate()


def _verify(reader, identity):
    try:
        verify_lineage(reader, identity)
    except RecordLineageError:
        raise RecognitionConflict('material_origin_sql_identity_changed') from None


def _identity(reader, namespace, collection, identity):
    if collection not in HEAD_COLLECTIONS:
        raise RecognitionConflict('material_origin_sql_identity_unavailable')
    row = reader.read(collection, identity)
    head = reader.read(HEAD_COLLECTIONS[collection], identity)
    if row is None or head is None:
        raise RecognitionConflict('material_origin_sql_identity_unavailable')
    fact_id = head.payload.get('fact_id')
    _verify(reader, {'collection': collection, 'object_id': identity, 'fact_id': fact_id})
    fact = reader.read(FACTS, fact_id)
    if fact is None:
        raise RecognitionConflict('material_origin_sql_identity_unavailable')
    return {'namespace': namespace, 'collection': collection, 'object_id': identity,
        'revision': row.revision, 'fact_id': fact_id, 'origin': fact.payload['origin'],
        'observed_revision': fact.payload['observed_revision']}


def _append(result, row):
    key = row['namespace'], row['collection'], row['object_id']
    if key in result and result[key] != row:
        raise RecognitionConflict('material_origin_sql_identity_changed')
    result[key] = row


def freeze_sql_identities(origins, materials, *, query=None, prepared_input=None, actor_reader=None):
    from .shared_rendered_preparation import PreparedRenderedWireInput, restored_rendered_body, validate_rendered_material
    from .material_read_context import is_prepared_primary_input
    from .shared_auxiliary import _qualified_commit
    from .material_origin_readers import qualified_record_read, qualified_actor_reader
    actor = qualified_actor_reader(origins, actor_reader)
    frozen, rendered_items, primary_input = None, None, False
    if prepared_input is not None:
        primary_input = is_prepared_primary_input(prepared_input)
        if (query is None or not isinstance(prepared_input, PreparedRenderedWireInput)
                and not primary_input):
            raise RecognitionConflict('material_origin_prepared_input_changed')
        frozen = prepared_input.body(query, origins, materials=materials)
    elif origins.auxiliary is not None and origins.auxiliary['selection'].get('schema_version') == 3:
        query, frozen, rendered_items = restored_rendered_body(origins)
    result = {}
    with _qualified_commit(origins.admission) as qualification:
        context = (nullcontext(actor) if actor is not None else
            qualified_record_read(origins.admission.destination.records, validate=qualification._validate))
        with context as transaction:
            actor_reader = TransactionRecords(transaction)
            for material in materials:
                owner = origins.owner(material['project_id'], material['namespace'])
                scope = WorkScope('local-user', owner.project_id)
                reader = qualification.reader_for(origins, owner, actor_reader=actor_reader)
                collector = MaterialSQLCollector(reader)
                descriptor = {key: material[key] for key in ('type', 'id', 'revision', 'project_id')}
                _, roots = resolve_turn_material(collector, scope, descriptor)
                authority_owner = owner if owner is origins.shared else origins.admission.destination.records
                SourceEgressService(authority_owner, reader=collector, _memo={}).snapshot(scope, roots)
                if frozen is not None:
                    _validate_primary_body(query, owner, collector, material, frozen, authority_owner=authority_owner,
                        strict_excerpt=primary_input)
                    if prepared_input is not None:
                        prepared_input.validate_material(owner, collector, material)
                    else:
                        validate_rendered_material(rendered_items, owner, collector, material)
                if descriptor['type'] == 'document':
                    repository = SQLiteDocumentRepository(TransactionRecords(collector), namespace_id=owner.documents.namespace_id)
                    if repository.markdown(descriptor['id'], revision=descriptor['revision']) is None:
                        raise RecognitionConflict('material_origin_sql_identity_unavailable')
                for collection, identity in collector.selected_keys():
                    _append(result, _identity(reader, owner.namespace, collection, identity))
                owner.validate_current()
            origins.validate_current()
            qualification._actor_reader(actor_reader)
    return [result[key] for key in sorted(result)]


def _validate_primary_body(query, owner, reader, material, frozen, *, authority_owner, strict_excerpt=False):
    from ..context_adapter import format_recognition_content
    from ..source_egress import recognition_service
    from .request_reads import DocumentReadSet

    candidate_kind = 'source' if material['type'] == 'original_source' else material['type']
    qualified = (recognition_service(TransactionRecords(reader), source_owner=authority_owner)
        if candidate_kind == 'recognition' else None)
    candidates = [row for row in frozen['chosen'] if row['kind'] == candidate_kind
        and row['entry']['id'] == material['id'] and row['scope'].project_id == material['project_id']
        and row.get('namespace', query.origin_admission.lease.namespace) == owner.namespace]
    for candidate in candidates:
        expected = candidate['entry']
        if candidate['kind'] == 'recognition':
            current = qualified._qualified_recognition(reader,
                qualified._require_scope(reader, 'recognitions', material['id'], candidate['scope']))
            if current is None or not current.authorized:
                raise RecognitionConflict('material_origin_model_body_changed')
            projection = current.retrieval_projection()
            if (any(expected.get(key) != projection.get(key) for key in
                    ('id', 'revision', 'content', 'conditions', 'source_refs'))
                    or candidate['excerpt'] != format_recognition_content(projection)):
                raise RecognitionConflict('material_origin_model_body_changed')
            continue
        if candidate['kind'] == 'source':
            repository = SQLiteDocumentRepository(TransactionRecords(reader), namespace_id=owner.documents.namespace_id)
            documents = {row['id']: row for row in repository.list(include_archived=True)
                if row.get('project_id') == owner.project_id}
            item_id = expected.get('item_id')
            item = reader.read('workspace_items', item_id) if isinstance(item_id, str) else None
            items = {item_id: item} if item is not None and item.payload.get('project_id') == owner.project_id else {}
            prepared = DocumentReadSet(documents, {}, items)
            current = next((row for row in query.query_entries(owner.project_id, selected=[expected], prepared=prepared)
                if row['kind'] == 'source' and row['id'] == material['id']), None)
            if (current is None or any(expected.get(key) != current.get(key)
                    for key in ('id', 'revision', 'title', 'content'))):
                raise RecognitionConflict('material_origin_model_body_changed')
            if candidate.get('coordinate_space') == 'source_content_v1':
                content = current['content']
            elif candidate.get('coordinate_space') == 'workspace_source_text_v1':
                item = items.get(expected.get('item_id'))
                if item is None:
                    raise RecognitionConflict('material_origin_model_body_changed')
                content = str(item.payload.get('source_text') or '')
            else:
                raise RecognitionConflict('material_origin_model_body_changed')
            if (not candidate['windows'] or any(type(window.start) is not int or type(window.end) is not int
                    or not 0 <= window.start < window.end <= len(content)
                    or content[window.start:window.end] != window.text for window in candidate['windows'])):
                raise RecognitionConflict('material_origin_model_body_changed')
            if strict_excerpt and candidate.get('excerpt') != '\n…\n'.join(window.text for window in candidate['windows']):
                raise RecognitionConflict('material_origin_model_body_changed')
            continue
        if candidate['kind'] != 'document':
            continue
        repository = SQLiteDocumentRepository(TransactionRecords(reader), namespace_id=owner.documents.namespace_id)
        document = repository.read(material['id'])
        markdown = repository.markdown(material['id'], revision=material['revision'])
        if document is None or markdown is None:
            raise RecognitionConflict('material_origin_model_body_changed')
        items = {row.object_id: reader.read('workspace_items', row.object_id)
            for row in reader.list('workspace_items')
            if row.payload.get('project_id') == owner.project_id and row.payload.get('status') == 'confirmed'
            and row.payload.get('document_id') == material['id']}
        prepared = DocumentReadSet({material['id']: document}, {material['id']: markdown}, items)
        entries = query.query_entries(owner.project_id, selected=[expected], prepared=prepared)
        current = next((row for row in entries if row['id'] == material['id']), None)
        if (current is None or any(expected.get(key) != current.get(key) for key in
                ('id', 'revision', 'title', 'item_id', 'item_revision', 'citation_id'))
                or expected.get('content') and expected['content'] != current['content']):
            raise RecognitionConflict('material_origin_model_body_changed')
        coordinates = candidate.get('coordinate_space')
        if coordinates == 'document_markdown_v1':
            content = markdown
        elif coordinates == 'workspace_source_text_v1':
            item = items.get(expected.get('item_id'))
            if item is None:
                raise RecognitionConflict('material_origin_model_body_changed')
            content = str(item.payload.get('source_text') or '')
        else:
            raise RecognitionConflict('material_origin_model_body_changed')
        if (not candidate['windows'] or any(type(window.start) is not int or type(window.end) is not int
                or not 0 <= window.start < window.end <= len(content)
                or content[window.start:window.end] != window.text for window in candidate['windows'])):
            raise RecognitionConflict('material_origin_model_body_changed')
        if strict_excerpt and candidate.get('excerpt') != '\n…\n'.join(window.text for window in candidate['windows']):
            raise RecognitionConflict('material_origin_model_body_changed')


def validate_sql_identities(origins, identities):
    validate_sql_shape(identities)
    seen = set()
    for frozen in identities:
        owners = [owner for owner in (origins.shared, origins.profile) if owner.namespace == frozen['namespace']]
        if len(owners) != 1:
            raise RecognitionConflict('material_origin_sql_identity_owner_changed')
        owner = owners[0]
        key = frozen['namespace'], frozen['collection'], frozen['object_id']
        if key in seen:
            raise RecognitionConflict('material_origin_sql_identity_changed')
        seen.add(key)
        with begin_read(owner.records) as reader:
            _verify(reader, {field: frozen[field] for field in ('collection', 'object_id', 'fact_id')})
            if _identity(reader, owner.namespace, frozen['collection'], frozen['object_id']) != frozen:
                raise RecognitionConflict('material_origin_sql_identity_changed')
        owner.validate_current()
    origins.validate_current()
