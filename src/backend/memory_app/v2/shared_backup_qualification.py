from backend.memory_app.original_sources import document_roots
from backend.memory_app.source_egress import SourceEgressService, recognition_service
from backend.memory_app.server_inspirations import _existing_records
from backend.recognition import RecognitionError, WorkScope
from backend.recognition.product_draft_dependencies import DRAFT_DEPENDENCY_FIELDS
from backend.recognition.external_input_dependencies import FIELDS as EXTERNAL_INPUT_FIELDS
from backend.memory_app.route_original_metadata import ROUTE_ORIGINAL_OWNER_FIELDS
from core.storage_provider.record_lineage import HEAD_COLLECTIONS, verify_lineage
from core.storage_provider.vault_backup_restore import verification_metadata
from .shared_projects import SharedProjectError


# 现有 verification metadata 的可选 version=2 字段；version=1 与旧归档继续可读。
FIELD = 'shared_archive_sources'
COLLECTIONS = ('workspace_items', 'documents', 'recognition_experiences', 'recognitions', 'recognition_candidates')
KINDS = {'workspace_items': 'original_item', 'recognition_experiences': 'experience', 'recognitions': 'recognition'}
_ARTIFACT = frozenset({'task_revision', 'document_revision', 'document_revision_record_revision',
    'document_markdown_revision', 'context_packet_revision'})
_NATIVE = (*DRAFT_DEPENDENCY_FIELDS, EXTERNAL_INPUT_FIELDS, ROUTE_ORIGINAL_OWNER_FIELDS, _ARTIFACT)


def _error():
    raise SharedProjectError('shared_backup_source_qualification_invalid', status=409)


def _lineage(records, collection, row):
    head = records.read(HEAD_COLLECTIONS[collection], row.object_id)
    if head is None:
        _error()
    value = {'collection': collection, 'object_id': row.object_id, 'fact_id': head.payload['fact_id']}
    verify_lineage(records, value)
    return value


def _graph(data, project, collection, row):
    scope = WorkScope('local-user', project)
    if collection in KINDS:
        refs = [{'type': KINDS[collection], 'id': row.object_id, 'revision': row.revision}]
    elif collection == 'documents':
        refs = [{'type': kind, 'id': identity, 'revision': revision}
            for kind, identity, revision in document_roots(data.records, scope, row.payload.get('source_refs', []), optional=False)]
    else:
        refs = []
        for name, kind, source_collection in (('source_experience_ids', 'experience', 'recognition_experiences'),
                ('source_recognition_ids', 'recognition', 'recognitions')):
            for identity in row.payload.get(name, []):
                source = data.records.read(source_collection, identity)
                if source is None:
                    _error()
                refs.append({'type': kind, 'id': identity, 'revision': source.revision})
    authority = SourceEgressService(data.records)
    graph = authority.snapshot(scope, refs)
    authority.validate_snapshot(scope, graph)
    return graph


def capture_sources(registry, data, project, namespace, snapshot):
    # 只有持有原共享根租约的真实来源 owner 可以形成此回执。
    if registry.roots._cache.get(project) is not data or data.namespace != namespace.rsplit('/', 1)[1]:
        _error()
    frozen = _existing_records(snapshot.snapshot_root / 'payload', data.namespace)
    if frozen is None:
        _error()
    entries = []
    for collection in COLLECTIONS:
        actual = {row.object_id: row for row in data.records.list(collection)}
        for row in frozen.list(collection):
            if collection == 'recognition_candidates' and row.payload.get('state') == 'published':
                continue
            current = actual.get(row.object_id)
            lineage = _lineage(frozen, collection, row)
            if (current is None or current.revision != row.revision or current.payload != row.payload
                    or _lineage(data.records, collection, current) != lineage):
                _error()
            value = {'collection': collection, 'id': row.object_id, 'revision': row.revision,
                'lineage': lineage, 'status': 'unverifiable', 'private': True, 'authority': None}
            try:
                stopped = current.payload.get('state') in {'revoked', 'superseded', 'stale', 'rejected', 'invalidated'}
                if stopped and collection in (*KINDS, 'recognition_candidates'):
                    kind = KINDS.get(collection, 'candidate')
                    history = recognition_service(data.records).archive_source_history(
                        scope=WorkScope('local-user', project), source_type=kind, source_id=current.object_id)
                    if history.get('archive_only') is not True or history.get('revision') != current.revision:
                        _error()
                    graph = None
                else:
                    graph = _graph(data, project, collection, current)
            except (RecognitionError, ValueError, SharedProjectError):
                # 归档保留无法证明的历史事实，完整复制必须在发布前明确拒绝。
                pass
            else:
                if graph is None:
                    value.update(status='historical', private=True, authority='portable')
                else:
                    native = any(set(node.get('dependency_revisions', {})) in _NATIVE for node in graph['nodes'])
                    private = any(not node['effective_purposes'] for node in graph['nodes'])
                    if collection == 'recognition_candidates':
                        from .shared_merge_privacy import pending_merge_private
                        scope = WorkScope('local-user', project)
                        try:
                            private = private or pending_merge_private(data.records, SourceEgressService(data.records), scope, current)
                        except (RecognitionError, ValueError, SharedProjectError):
                            private = True
                    value.update(status='verified', private=private,
                        authority='nonportable_native' if native else 'portable')
            entries.append(value)
    return {'version': 2, 'namespace': namespace, 'snapshot_fingerprint': snapshot.source_fingerprint, 'objects': entries}


def read_sources(row, snapshot, records):
    saved = row.payload.get(FIELD)
    observed = verification_metadata(snapshot.snapshot_root).get(FIELD)
    if saved is None and observed is None:
        return {}
    if (not isinstance(saved, dict) or saved != observed
            or set(saved) != {'version', 'namespace', 'snapshot_fingerprint', 'objects'}
            or type(saved['version']) is not int or saved['version'] not in {1, 2} or saved['namespace'] != row.payload['source_namespace']
            or saved['snapshot_fingerprint'] != snapshot.source_fingerprint or not isinstance(saved['objects'], list)):
        _error()
    result = {}
    for value in saved['objects']:
        if (not isinstance(value, dict) or set(value) != {'collection', 'id', 'revision', 'lineage', 'status', 'private', 'authority'}
                or value['collection'] not in COLLECTIONS or not isinstance(value['id'], str)
                or type(value['revision']) is not int
                or type(value['private']) is not bool or value['status'] not in (
                    {'verified', 'unverifiable', 'historical'} if saved['version'] == 2 else {'verified', 'unverifiable'})
                or value['authority'] not in {'portable', 'nonportable_native', None}):
            _error()
        key = value['collection'], value['id']
        source = records.read(*key)
        if (key in result or source is None or source.revision != value['revision']
                or _lineage(records, key[0], source) != value['lineage']
                or value['status'] == 'verified' and value['authority'] is None
                or value['status'] == 'unverifiable' and value['authority'] is not None):
            _error()
        if value['status'] == 'historical' and (not value['private'] or value['authority'] != 'portable'
                or source.payload.get('state') not in {'revoked', 'superseded', 'stale', 'rejected', 'invalidated'}):
            _error()
        result[key] = value
    expected = {(collection, source.object_id) for collection in COLLECTIONS for source in records.list(collection)
        if collection != 'recognition_candidates' or source.payload.get('state') != 'published'}
    if set(result) != expected:
        _error()
    return result
