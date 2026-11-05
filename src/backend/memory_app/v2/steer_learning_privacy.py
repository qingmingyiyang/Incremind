from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
import sqlite3

from backend.recognition import RecognitionConflict, WorkScope
from backend.recognition.external_evidence_json import encoded
from backend.recognition.sql_source_identities import CollectingReader
from core.storage_provider.record_lineage import RecordLineageError

from ..source_egress import SourceEgressService, _parents, _source, validate_inspiration_source
from ..source_graph import LIMIT, SourceGraph
from ..transaction_records import TransactionRecords
from .material_origins import SharedMaterialOwner


_PUBLICATION_READS = ContextVar('steer_learning_publication_privacy_reads', default=())


@contextmanager
def publication_privacy_scope(freshness, reader, context, *, publication_proof):
    from .steer_learning_publication import require_published_learning_context
    require_published_learning_context(publication_proof, freshness=freshness, reader=reader, context=context)
    _, transaction = _publication_transaction(freshness, reader)
    key = (freshness, transaction, encoded(context))
    previous = _PUBLICATION_READS.get()
    if key in previous or len(previous) >= LIMIT:
        raise RecognitionConflict('steer_learning_publication_privacy_cycle_or_limit')
    token = _PUBLICATION_READS.set((*previous, key))
    try:
        yield
    finally:
        _PUBLICATION_READS.reset(token)


def _publication_transaction(freshness, reader):
    from .outcome_source_reads import SourceReadRecords
    transaction = reader
    while isinstance(transaction, (TransactionRecords, CollectingReader)):
        if isinstance(transaction, SourceReadRecords):
            transaction.validate(freshness)
        transaction = (transaction._reader if isinstance(transaction, CollectingReader)
            else transaction._transaction)
    view = freshness._reader(transaction)
    if transaction.connection is not view.connection or not view.connection.in_transaction:
        raise RecognitionConflict('steer_learning_publication_transaction_changed')
    return view, transaction


def publication_privacy_active(freshness, reader):
    _, transaction = _publication_transaction(freshness, reader)
    return any(owner is freshness and actual is transaction for owner, actual, _ in _PUBLICATION_READS.get())


def _permit(permits, scope, identity, revision, birth=None):
    key = (scope.user_id, scope.project_id, identity)
    current = permits.setdefault(key, {'birth': None, 'revisions': set()})
    if birth is not None:
        if current['birth'] is not None and current['birth'] != birth:
            raise RecognitionConflict('steer_learning_retained_birth_changed')
        current['birth'] = deepcopy(birth)
    current['revisions'].add(revision)


def _permit_snapshot(permits, snapshot, *, namespace, source_namespace):
    graph = SourceGraph()
    graph.snapshot(snapshot)
    for node in graph.result()['nodes']:
        if (node['kind'] == 'material' and node['type'] == 'recognition'
                and node.get('namespace', source_namespace) == namespace):
            _permit(permits, WorkScope(**node['scope']), node['id'], node['source_revision'])


class _PublishedPrivacySources(SourceEgressService):
    def __init__(self, records, reader, permits):
        # 私有实例只读取已经验证的旧依据，不与普通外发遍历共享缓存。
        super().__init__(records, reader=reader, _memo={})
        self._permits = permits

    def _walk_key(self, kind, scope, value):
        return None, None

    def _source_for_graph(self, uow, scope, source_type, source_id):
        permit = self._permits.get((scope.user_id, scope.project_id, source_id)) if source_type == 'recognition' else None
        if permit is None:
            return _source(uow, scope, source_type, source_id)
        from .steer_learning_lineage import retained_recognition_record
        current = None
        for revision in sorted(permit['revisions']):
            current, _ = retained_recognition_record(self._owner_records, uow, scope, source_id,
                expected_birth=permit['birth'], expected_revision=revision)
        if current is None:
            raise RecognitionConflict('steer_learning_retained_source_unavailable')
        if current.payload.get('state') not in {'active', 'superseded'}:
            raise RecognitionConflict('steer_learning_retained_source_unavailable')
        return current

    def _check_graph_parent(self, uow, scope, source, parent_type, parent, revision):
        permit = self._permits.get((scope.user_id, scope.project_id, parent.object_id)) if parent_type == 'recognition' else None
        if permit is None:
            return super()._check_graph_parent(uow, scope, source, parent_type, parent, revision)
        from .steer_learning_lineage import retained_recognition_record
        current, _ = retained_recognition_record(self._owner_records, uow, scope, parent.object_id,
            expected_birth=permit['birth'], expected_revision=revision)
        if current != parent:
            raise RecognitionConflict('steer_learning_retained_source_changed')


def recognition_privacy_snapshots(authority, uow, scope, source, fact, *, trail, budget):
    owner = authority._owner_records
    if isinstance(owner, SharedMaterialOwner):
        owner.validate_current()
        namespace = owner.namespace
    else:
        learning = getattr(owner, 'product_steer_learning', None)
        if learning is None or learning.records is not owner:
            raise RecognitionConflict('steer_learning_recognition_actual_owner_unavailable')
        namespace = learning.freshness._source_owner(scope.project_id).namespace
    permits = {}
    for qualified in fact['qualified']:
        for frozen in qualified['privacy_ceiling']['sources']:
            _permit_snapshot(permits, frozen['current'], namespace=namespace,
                source_namespace=frozen['origin']['namespace'])
            origins = frozen.get('history_origins', [frozen['origin']] * len(frozen['history_current']))
            for snapshot, origin in zip(frozen['history_current'], origins, strict=True):
                _permit_snapshot(permits, snapshot, namespace=namespace, source_namespace=origin['namespace'])
    for ancestor in fact.get('lineage_recognitions', []):
        _permit(permits, scope, ancestor['id'], ancestor['revision'], ancestor['birth'])
    private = _PublishedPrivacySources(authority._owner_records, uow, permits)
    additional = validate_inspiration_source(uow, scope, source)
    references = list(dict.fromkeys((*_parents('recognition', source.payload),
        *(('experience', item['id'], item['revision']) for item in additional),
        *(('recognition', item['id'], item['revision']) for item in fact.get('lineage_recognitions', [])))))
    snapshots = []
    for kind, identity, revision in references:
        current = private._source_for_graph(TransactionRecords(uow), scope, kind, identity)
        private._check_graph_parent(uow, scope, source, kind, current, revision)
        snapshot = private._snapshot(scope, [{'type': kind, 'id': identity, 'revision': current.revision}], trail, budget)
        if snapshot not in snapshots:
            snapshots.append(snapshot)
    return snapshots


def _current_source(freshness, owner, reader, source, private):
    frozen = source['frozen']
    if frozen['source_type'] != 'recognition':
        current = freshness._source(owner, reader, frozen['source_type'], frozen['id'])
        if current['incarnation'] != frozen['incarnation']:
            raise RecognitionConflict('outcome_source_birth_changed')
        return current['revision']
    from .outcome_freshness import _identity
    from .steer_learning_lineage import retained_recognition_record
    scope = WorkScope('local-user', frozen['project_id'])
    authority_owner = owner if isinstance(owner, SharedMaterialOwner) else owner.records
    row, _ = retained_recognition_record(authority_owner, reader, scope, frozen['id'],
        expected_birth=frozen['incarnation'], expected_revision=frozen['revision'])
    versions = [version for version in reader.list('recognition_versions')
        if version.payload.get('recognition_id') == frozen['id']
        and version.payload.get('recognition_revision') == frozen['revision']]
    if len(versions) != 1:
        raise RecognitionConflict('steer_learning_retained_version_unavailable')
    original = versions[0].payload['snapshot']
    if _identity({'content': original.get('content'), 'conditions': original.get('conditions', [])}) != frozen['content']:
        raise RecognitionConflict('steer_learning_retained_body_changed')
    if private._source_for_graph(reader, scope, 'recognition', frozen['id']) != row:
        raise RecognitionConflict('steer_learning_retained_source_changed')
    return row.revision


def _publication_sources(freshness, proofs, owner, reader):
    permits = {}
    for source in proofs:
        if not source.get('verified'):
            continue
        origin, frozen = source['origin'], source['frozen']
        _permit_snapshot(permits, source['snapshot'], namespace=owner.namespace,
            source_namespace=origin['namespace'])
        if origin['namespace'] == owner.namespace and frozen['source_type'] == 'recognition':
            _permit(permits, WorkScope('local-user', frozen['project_id']), frozen['id'],
                frozen['revision'], frozen['incarnation'])
    authority = freshness._source_authority(owner, reader)
    return _PublishedPrivacySources(authority._owner_records, reader, permits)


def _publication_history(freshness, source, proofs, *, reader):
    graph = SourceGraph()
    graph.snapshot(source['snapshot'])
    history = {}
    for node in graph.result()['nodes']:
        if node['kind'] != 'material':
            continue
        scope = WorkScope(**node['scope'])
        namespace = node.get('namespace', source['origin']['namespace'])
        owner = freshness._source_owner(scope.project_id, origin={'namespace': namespace}, reader=reader)
        origin = freshness._origin(owner)
        if origin['namespace'] != namespace:
            raise RecognitionConflict('outcome_source_owner_changed')
        source_reader = freshness._source_reader(owner, reader)
        private = _publication_sources(freshness, proofs, owner, source_reader)
        current = private._source_for_graph(source_reader, scope, node['type'], node['id'])
        snapshot = private.snapshot(scope, [{'type': node['type'], 'id': node['id'], 'revision': current.revision}])
        history[encoded({'origin': origin, 'snapshot': snapshot})] = (snapshot, origin)
    return list(history.values())


def qualify_publication_sources(freshness, proofs, *, reader, local_only):
    from ..original_sources import resolve_turn_material
    from .outcome_freshness import OutcomeFreshnessError
    reader, _ = _publication_transaction(freshness, reader)
    if not publication_privacy_active(freshness, reader):
        raise RecognitionConflict('steer_learning_publication_privacy_scope_required')
    purposes, ceilings, unknown = {'generation', 'embedding', 'rerank'}, [], []
    for source in proofs:
        if not source.get('verified'):
            unknown.append({'reason': source.get('reason', 'outcome_source_proof_unavailable')})
            purposes.clear()
            continue
        frozen, origin = source['frozen'], source['origin']
        try:
            owner = freshness._source_owner(origin['project_id'], origin=origin, binding=source, reader=reader)
            if freshness._origin(owner) != origin:
                raise OutcomeFreshnessError('outcome_source_owner_changed')
            source_reader = freshness._source_reader(owner, reader)
            private = _publication_sources(freshness, proofs, owner, source_reader)
            revision = _current_source(freshness, owner, source_reader, source, private)
            descriptor = {'type': frozen['source_type'], 'id': frozen['id'],
                'revision': revision, 'project_id': frozen['project_id']}
            scope = WorkScope('local-user', frozen['project_id'])
            if frozen['source_type'] == 'recognition':
                roots = [{key: descriptor[key] for key in ('type', 'id', 'revision')}]
            else:
                _, roots = resolve_turn_material(source_reader, scope, descriptor)
            snapshot = private.snapshot(scope, roots)
            history_pairs = _publication_history(freshness, source, proofs, reader=reader)
            history = [item[0] for item in history_pairs]
            for item in [source['snapshot'], snapshot, *history]:
                for node in item['nodes']:
                    purposes.intersection_update(node['effective_purposes'])
            ceilings.append({'origin': origin, 'source': descriptor, 'previous': deepcopy(source['snapshot']),
                'current': snapshot, 'history_current': history,
                'history_origins': [item[1] for item in history_pairs], 'aliases': deepcopy(source['aliases'])})
        except (RecognitionConflict, RecordLineageError, FileNotFoundError, PermissionError, sqlite3.Error) as error:
            unknown.append({'id': frozen['id'], 'project_id': frozen['project_id'], 'reason': str(error)})
            purposes.clear()
    if not local_only and 'generation' not in purposes:
        raise OutcomeFreshnessError('private_source_remote_blocked')
    return {'complete': not unknown, 'unknown': unknown, 'current_sources': [], 'recognitions': [],
        'privacy_ceiling': {'effective_purposes': sorted(purposes), 'sources': ceilings}}
