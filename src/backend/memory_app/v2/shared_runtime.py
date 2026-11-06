from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import os
import re
from datetime import datetime
from threading import RLock, get_ident
from uuid import uuid4

from backend.shared.interprocess_lock import interprocess_file_lock
from backend.security.user_context import USER_ACCESS, user_action_allowed
from core.storage_provider.record_lineage import verify_lineage
from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import JsonObjectStore, SourceAssetRuntimeStore
from .server_deletion import _delete_posix, _delete_windows, _directory, _inspect_tree
from .shared_projects import ACTIVITY, ATTRIBUTION, SharedProjectError, segment
from .server_users import current_caller, enabled_user
from ..transaction_records import TransactionRecords

_ACTIVE_SHARED_READ = ContextVar('shared_transaction_read_capability', default=None)
_SHARED_READ_CAPABILITIES = {}
_CONVERSATION_PRIVACY = 'source_egress_original_item_policies'
_MERGE_PRIVACY = 'source_egress_recognition_policies'
_MERGE_COLLECTIONS = frozenset({'recognition_restructure_proposals', 'v2_candidate_merges',
    'v2_shared_insight_merge_operations', 'v2_shared_insight_merge_privacy'})

_CONTENT_COLLECTIONS = frozenset({
    'workspace_items', 'documents', 'document_revisions', 'document_markdown',
    'recognition_experiences', 'recognition_candidates', 'recognitions', 'recognition_versions',
    'recognition_relations', 'recognition_relation_proposals', 'v2_insight_validity', 'v2_insight_interference',
    'project_constraints', 'v2_constraint_scopes', 'v2_correction_events',
    'v2_experience_origins', 'v2_memory_recall', 'v2_document_recall', 'v2_private_scopes',
    'v2_scene_assignments_item', 'v2_scene_assignments_document',
    'v2_scene_assignments_candidate', 'v2_scene_assignments_recognition',
    'v2_usage_insight', 'v2_usage_document', 'v2_usage_inspiration',
    'recognition_recall_preferences', 'v2_candidate_fade', 'v2_activity',
    'original_text_retrieval_index', 'v2_original_sections', 'v2_shared_intake_commands',
    'v2_shared_document_imports', 'v2_image_groups', 'document_retrieval_index',
    'workspace_processing_heartbeats', 'v2_shared_organize_results',
    'v2_external_input_dependencies', 'v2_shared_mcp_usage', _CONVERSATION_PRIVACY, _MERGE_PRIVACY,
}) | _MERGE_COLLECTIONS


@dataclass(frozen=True)
class SharedData:
    root: Path
    records: object
    documents: object
    namespace: str
    root_identity: tuple
    sources: object


class SharedDataRoots:
    def __init__(self, server_root):
        self.server_root = Path(server_root).absolute()
        self._cache, self._cache_lock = {}, RLock()

    def path(self, project_id, *, create=False):
        project_id = segment(project_id)
        if not project_id.startswith('shared-'):
            raise SharedProjectError('shared_root_invalid')
        for node in (*reversed(self.server_root.parents), self.server_root):
            _directory(node)
        parent = self.server_root / 'shared'
        if create:
            parent.mkdir(exist_ok=True)
        _directory(parent)
        root = parent / project_id
        if root.parent != parent or root in (parent, self.server_root):
            raise SharedProjectError('shared_root_invalid')
        if create:
            root.mkdir(exist_ok=True)
        _directory(root)
        return root

    @contextmanager
    def lease(self, project_id):
        authority = self.server_root / 'server' / segment(project_id)
        with interprocess_file_lock(authority):
            yield

    def _data(self, project_id, *, require_binding=True):
        root = self.path(project_id)
        with self._cache_lock:
            cached = self._cache.get(project_id)
        if cached is not None:
            observed = _directory(root)
            if (observed.st_dev, observed.st_ino) != cached.root_identity:
                raise SharedProjectError('shared_root_changed', status=409)
            binding = cached.records.read('v2_shared_root', 'identity')
            if binding is None or binding.payload.get('project_id') != project_id:
                raise SharedProjectError('shared_root_binding_invalid', status=409)
            return cached
        # 复用唯一聚合工厂，只装配数据领域；不实例化个人模型或对话宿主。
        json = JsonObjectStore(root / '.rebuild-data', legacy_root=root / 'library', namespace_id='default')
        factory = AggregateRepositoryFactory(runtime_root=root, namespace_id='default', json_store=json)
        records, namespace = factory.recognition_record_store_resolution()
        binding = records.read('v2_shared_root', 'identity')
        if require_binding and (binding is None or binding.payload.get('project_id') != project_id):
            raise SharedProjectError('shared_root_binding_invalid', status=409)
        documents = SQLiteDocumentRepository(records, namespace_id=namespace)
        source_authority = factory.source_asset_authority_resolution()
        sources = SourceAssetRuntimeStore(json_store=json, sqlite_records=source_authority.records,
            library_root=root / 'library', authority_identity=source_authority.authority_identity,
            publication_records=records)
        observed = _directory(root)
        data = SharedData(root, records, documents, namespace, (observed.st_dev, observed.st_ino), sources)
        if require_binding:
            with self._cache_lock:
                self._cache[project_id] = data
        return data

    def provision(self, project_id, project_name):
        with self.lease(project_id):
            self.path(project_id, create=True)
            data = self._data(project_id, require_binding=False)
            with data.records.begin() as tx:
                binding = tx.read('v2_shared_root', 'identity')
                if binding and binding.payload.get('project_id') != project_id:
                    raise SharedProjectError('shared_root_binding_invalid', status=409)
                if binding is None:
                    if any(tx.list(collection) for collection in ('documents', 'workspace_items', 'recognitions', 'recognition_experiences')):
                        raise SharedProjectError('shared_root_not_empty', status=409)
                    tx.put('v2_shared_root', 'identity', {'project_id': project_id}, expected_revision=0)
                current = tx.read('v2_projects', project_id)
                if current is None:
                    tx.put('v2_projects', project_id, {'name': project_name, 'scenes': [], 'private': False,
                           'builtin': None}, expected_revision=0)
                tx.commit()
            with self._cache_lock:
                self._cache[project_id] = data

    def project_activity(self, project_id, payload):
        with self.lease(project_id):
            data = self._data(project_id)
            with data.records.begin() as tx:
                current = tx.read(ACTIVITY, payload['id'])
                if current is not None and dict(current.payload) != dict(payload):
                    raise SharedProjectError('shared_activity_conflict', status=409)
                if current is None:
                    tx.put(ACTIVITY, payload['id'], payload, expected_revision=0)
                tx.commit()

    def activity(self, project_id):
        with self.lease(project_id):
            return [{**row.payload, 'revision': row.revision}
                    for row in self._data(project_id).records.list(ACTIVITY)]

    def inspect(self, project_id):
        with self.lease(project_id):
            root = self.path(project_id)
            _inspect_tree(root)
            value = _directory(root)
            return [value.st_dev, value.st_ino]

    def delete(self, project_id, identity):
        with self.lease(project_id):
            with self._cache_lock:
                self._cache.pop(project_id, None)
            try:
                root = self.path(project_id)
            except FileNotFoundError:
                return
            value = _directory(root)
            if [value.st_dev, value.st_ino] != identity:
                raise SharedProjectError('shared_root_changed', status=409)
            _inspect_tree(root)
            if os.name == 'nt':
                _delete_windows(root, identity)
            else:
                _delete_posix(root, identity)
            if root.exists() or root.is_symlink():
                raise SharedProjectError('shared_delete_incomplete', status=409)


class SharedWriteRecords:
    def __init__(self, registry, actor, project_id, *, authority, action, object_kind, object_id, object_title):
        if not isinstance(authority, dict):
            raise SharedProjectError('shared_authority_required')
        self.registry, self.actor, self.project_id = registry, actor, project_id
        self.authority, self.action = dict(authority), action
        self.object_kind, self.object_id, self.object_title = object_kind, object_id, object_title

    @contextmanager
    def begin(self):
        active = _SHARED_READ_CAPABILITIES.get(_ACTIVE_SHARED_READ.get())
        if active is not None and active._thread == get_ident() and active._active:
            raise SharedProjectError('shared_nested_transaction_forbidden', status=409)
        # 文件定位及聚合装配先完成，中央资格锁只覆盖本次短数据事务。
        with self.registry.records.begin_read() as central:
            self.registry.authorize(central, self.actor, self.project_id, action='write', authority=self.authority)
        with self.registry.roots.lease(self.project_id):
            data = self.registry.roots._data(self.project_id)
        with self.registry.records.begin() as central:
            access = self.registry.authorize(central, self.actor, self.project_id,
                action='write', authority=self.authority)
            with self.registry.roots.lease(self.project_id):
                fresh = self.registry.roots._data(self.project_id)
                if (fresh.namespace != data.namespace or fresh.root_identity != data.root_identity
                        or fresh.root != data.root):
                    raise SharedProjectError('shared_root_changed', status=409)
                with fresh.records.begin() as transaction:
                    writer = _SharedWriteTransaction(transaction, self, access, central)
                    capability = _SharedReadCapability(writer, fresh)
                    writer._read_capability = capability
                    _SHARED_READ_CAPABILITIES[capability._token] = capability
                    token = _ACTIVE_SHARED_READ.set(capability._token)
                    try:
                        yield writer
                    finally:
                        capability._active = False
                        _SHARED_READ_CAPABILITIES.pop(capability._token, None)
                        _ACTIVE_SHARED_READ.reset(token)

    def read(self, collection, object_id):
        with self.registry.records.begin() as central:
            self.registry.authorize(central, self.actor, self.project_id, authority=self.authority)
            with self.registry.roots.lease(self.project_id):
                result = self.registry.roots._data(self.project_id).records.read(collection, object_id)
            return result

    def list(self, collection):
        with self.registry.records.begin() as central:
            self.registry.authorize(central, self.actor, self.project_id, authority=self.authority)
            with self.registry.roots.lease(self.project_id):
                result = self.registry.roots._data(self.project_id).records.list(collection)
            return result

    def list_matching(self, collection, **conditions):
        with self.registry.records.begin() as central:
            self.registry.authorize(central, self.actor, self.project_id, authority=self.authority)
            with self.registry.roots.lease(self.project_id):
                result = self.registry.roots._data(self.project_id).records.list_matching(collection, **conditions)
            return result


class _SharedWriteTransaction:
    def __init__(self, transaction, owner, access, central):
        self.transaction, self.owner, self.access = transaction, owner, access
        self._central = central
        self.writes = []

    def __getattr__(self, name):
        return getattr(self.transaction, name)

    def _attribute(self, record, action):
        if record.collection in {ACTIVITY, ATTRIBUTION}:
            raise SharedProjectError('shared_internal_collection_write')
        identity = 'write-' + uuid4().hex
        payload = {'id': identity, 'project_id': self.access.project_id, 'actor_user_id': self.access.actor_user_id,
                   'by': self.access.by, 'collection': record.collection, 'object_id': record.object_id,
                   'object_revision': record.revision, 'action': action, 'at': self.owner.registry.now()}
        self.transaction.put(ATTRIBUTION, identity, payload, expected_revision=0)
        self.writes.append(identity)

    def put(self, collection, object_id, payload, *, expected_revision):
        if collection not in _CONTENT_COLLECTIONS:
            raise SharedProjectError('shared_collection_forbidden', status=403)
        if collection in _MERGE_COLLECTIONS and self.owner.action != 'merge':
            raise SharedProjectError('shared_collection_forbidden', status=403)
        if collection in {'v2_shared_document_imports', 'v2_image_groups', 'document_retrieval_index'} and (
                self.owner.action != 'file' or expected_revision != 0):
            raise SharedProjectError('shared_collection_forbidden', status=403)
        if collection == _MERGE_PRIVACY:
            item = self.transaction.read('recognitions', object_id)
            births = (self.transaction.read(ATTRIBUTION, identity).payload for identity in self.writes)
            if (self.owner.action not in {'merge', 'confirm', 'supersede'} or expected_revision != 0
                    or item is None or item.revision != 1 or item.payload.get('published_by') != self.owner.actor.user_id
                    or item.payload.get('project_id') != self.access.project_id
                    or not any((birth['collection'], birth['object_id'], birth['object_revision'], birth['action'])
                        == ('recognitions', object_id, 1, 'write') for birth in births)
                    or payload.get('id') != object_id or payload.get('source_type') != 'recognition'
                    or payload.get('source_id') != object_id or payload.get('source_revision') != 1
                    or payload.get('allowed_purposes') != []):
                raise SharedProjectError('shared_content_scope_invalid', status=403)
        if collection == 'v2_external_input_dependencies' and (payload.get('schema_version') != 3
                or payload.get('actor_user_id') != self.access.actor_user_id
                or payload.get('namespace') != self._read_capability.namespace):
            raise SharedProjectError('shared_content_scope_invalid', status=403)
        if collection == 'v2_shared_mcp_usage' and (self.owner.action != 'use'
                or payload.get('actor_user_id') != self.access.actor_user_id
                or payload.get('namespace') != self._read_capability.namespace):
            raise SharedProjectError('shared_content_scope_invalid', status=403)
        if collection == 'v2_insight_interference':
            superseding = payload.get('superseding_id')
            if (self.owner.action != 'supersede' or not isinstance(superseding, str) or superseding == object_id
                    or set(payload) != {'superseding_id', 'confirmed_at', 'count_at_confirmation'}):
                raise SharedProjectError('shared_content_scope_invalid', status=403)
            scope = {'user_id': 'local-user', 'project_id': self.access.project_id}
            for identity in (object_id, superseding):
                endpoint = self.transaction.read('recognitions', identity)
                if endpoint is None or endpoint.payload.get('scope') != scope or endpoint.payload.get('state') != 'active':
                    raise SharedProjectError('shared_content_scope_invalid', status=403)
        if collection == _CONVERSATION_PRIVACY:
            item = self.transaction.read('workspace_items', object_id)
            births = (self.transaction.read(ATTRIBUTION, identity).payload for identity in self.writes)
            if (self.owner.action != 'remember' or self.owner.object_kind != 'source'
                    or self.owner.object_id != object_id or expected_revision != 0
                    or item is None or item.revision != 1 or item.payload.get('input_kind') != 'conversation'
                    or item.payload.get('project_id') != self.access.project_id
                    or not any((birth['collection'], birth['object_id'], birth['object_revision'], birth['action'])
                        == ('workspace_items', object_id, 1, 'write') for birth in births)
                    or payload.get('id') != object_id
                    or payload.get('source_type') != 'original_item' or payload.get('source_id') != object_id
                    or payload.get('source_revision') != 1 or payload.get('allowed_purposes') != []):
                raise SharedProjectError('shared_content_scope_invalid', status=403)
        if (payload.get('project_id', self.access.project_id) != self.access.project_id
                or isinstance(payload.get('scope'), Mapping) and payload['scope'].get('project_id') != self.access.project_id
                or collection == 'v2_private_scopes' and object_id != self.access.project_id):
            raise SharedProjectError('shared_content_scope_invalid', status=403)
        saved = self.transaction.put(collection, object_id, payload, expected_revision=expected_revision)
        self._attribute(saved, 'write')
        return saved

    def delete(self, collection, object_id, *, expected_revision):
        if collection not in _CONTENT_COLLECTIONS or collection in _MERGE_COLLECTIONS | {_CONVERSATION_PRIVACY, _MERGE_PRIVACY}:
            raise SharedProjectError('shared_collection_forbidden', status=403)
        if collection in {'v2_shared_document_imports', 'v2_image_groups', 'document_retrieval_index'}:
            raise SharedProjectError('shared_collection_forbidden', status=403)
        current = self.transaction.read(collection, object_id)
        result = self.transaction.delete(collection, object_id, expected_revision=expected_revision)
        if current:
            self._attribute(current, 'delete')
        return result

    def put_human_review_nonce(self, object_id, payload, *, expected_revision):
        collection = 'v2_native_mcp_human_review_nonces'
        fields = {'schema_version', 'actor_user_id', 'device_id', 'device_revision', 'target_user_id',
            'project_id', 'candidate_id', 'candidate_revision', 'candidate_fact_id', 'action',
            'expires_at', 'issuer_instance', 'consumed', 'result_id', 'result_revision'}
        if (not isinstance(object_id, str) or re.fullmatch(r'nonce-[a-f0-9]{64}', object_id) is None
                or not isinstance(payload, dict) or set(payload) != fields
                or type(payload['schema_version']) is not int or payload['schema_version'] != 1
                or payload['actor_user_id'] != self.owner.actor.user_id
                or payload['device_id'] != self.owner.actor.device_id
                or type(payload['device_revision']) is not int or payload['device_revision'] != self.owner.actor.revision
                or payload['project_id'] != self.access.project_id
                or payload['action'] not in {'confirm', 'discard'}
                or type(payload['candidate_revision']) is not int or payload['candidate_revision'] < 1
                or not isinstance(payload['candidate_fact_id'], str) or re.fullmatch(r'[a-f0-9]{32}', payload['candidate_fact_id']) is None
                or not isinstance(payload['issuer_instance'], str) or re.fullmatch(r'[a-f0-9]{32}', payload['issuer_instance']) is None
                or type(payload['expires_at']) is not int or payload['expires_at'] <= 0
                or type(payload['consumed']) is not bool):
            raise SharedProjectError('shared_human_review_binding_invalid', status=403)
        access = USER_ACCESS.get()
        caller = current_caller(self._central, self.owner.actor)
        if (access is None or access.caller != self.owner.actor or access.target_user_id != payload['target_user_id']
                or enabled_user(self._central, payload['target_user_id']) is None
                or not user_action_allowed(caller.payload['role'], self.owner.actor.user_id, payload['target_user_id'], 'space')):
            raise SharedProjectError('shared_human_review_target_changed', status=403)
        self.owner.registry.authorize(self._central, self.owner.actor, self.access.project_id,
                                      action='write', authority=self.owner.authority)
        candidate = self.transaction.read('recognition_candidates', payload['candidate_id'])
        if candidate is None or candidate.payload.get('scope', {}).get('project_id') != self.access.project_id:
            raise SharedProjectError('shared_human_review_candidate_changed', status=409)
        try:
            verify_lineage(self.transaction, {'collection': 'recognition_candidates', 'object_id': candidate.object_id,
                                              'fact_id': payload['candidate_fact_id']})
        except ValueError as error:
            raise SharedProjectError('shared_human_review_candidate_changed', status=409) from error
        previous = self.transaction.read(collection, object_id)
        if payload['expires_at'] <= int(datetime.fromisoformat(self.owner.registry.now()).timestamp()):
            raise SharedProjectError('shared_human_review_expired', status=409)
        if not payload['consumed']:
            if (expected_revision != 0 or previous is not None or payload['result_id'] is not None
                    or payload['result_revision'] is not None or candidate.revision != payload['candidate_revision']
                    or candidate.payload.get('state') != 'pending'):
                raise SharedProjectError('shared_human_review_candidate_changed', status=409)
        else:
            immutable = fields - {'consumed', 'result_id', 'result_revision'}
            if (previous is None or previous.payload.get('consumed') is not False
                    or any(previous.payload.get(field) != payload[field] for field in immutable)
                    or candidate.revision != payload['candidate_revision'] + 1
                    or type(payload['result_revision']) is not int or payload['result_revision'] < 1):
                raise SharedProjectError('shared_human_review_candidate_changed', status=409)
            if payload['action'] == 'confirm':
                result = self.transaction.read('recognitions', payload['result_id'])
                if (candidate.payload.get('state') != 'published' or candidate.payload.get('recognition_id') != payload['result_id']
                        or result is None or result.revision != payload['result_revision']
                        or result.payload.get('scope') != candidate.payload.get('scope')
                        or result.payload.get('published_by') != self.owner.actor.user_id):
                    raise SharedProjectError('shared_human_review_result_changed', status=409)
            elif (candidate.payload.get('state') != 'rejected' or payload['result_id'] != candidate.object_id
                    or payload['result_revision'] != candidate.revision
                    or candidate.payload.get('reviewed_by') != self.owner.actor.user_id):
                raise SharedProjectError('shared_human_review_result_changed', status=409)
        saved = self.transaction.put(collection, object_id, payload, expected_revision=expected_revision)
        self._attribute(saved, 'write')
        return saved

    def commit(self):
        if self.writes:
            identity = 'activity-' + uuid4().hex
            self.transaction.put(ACTIVITY, identity, {'id': identity, 'project_id': self.access.project_id,
                'actor_user_id': self.access.actor_user_id, 'by': self.access.by, 'at': self.owner.registry.now(),
                'action': self.owner.action, 'object_kind': self.owner.object_kind,
                'object_id': self.owner.object_id, 'object_title': self.owner.object_title,
                'writes': list(self.writes)}, expected_revision=0)
        self.transaction.commit()
        self._read_capability._active = False


def shared_read_capability(reader, *, registry):
    capability = _SHARED_READ_CAPABILITIES.get(_ACTIVE_SHARED_READ.get())
    if capability is None:
        raise SharedProjectError('shared_read_capability_unavailable', status=409)
    capability.validate(reader=reader, registry=registry)
    return capability


def current_shared_read_capability(*, registry):
    token = _ACTIVE_SHARED_READ.get()
    if token is None:
        return None
    capability = _SHARED_READ_CAPABILITIES.get(token)
    if capability is None:
        raise SharedProjectError('shared_read_capability_expired', status=409)
    capability.validate(registry=registry)
    return capability


class _SharedReadCapability:
    def __init__(self, writer, data):
        self._writer, self._data = writer, data
        self._thread, self._token, self._active = get_ident(), object(), True
        self._database_path = data.records.database_path.resolve(strict=True)
        if not self._database_path.is_relative_to(data.root):
            raise SharedProjectError('shared_read_capability_root_changed', status=409)
        observed = self._database_path.stat()
        self._database_identity = observed.st_dev, observed.st_ino
        self._marker = writer.transaction.read('v2_shared_root', 'identity')
        self.records = _BorrowedSharedRecords(self)

    def validate(self, *, reader=None, registry=None):
        writer, data = self._writer, self._data
        if (not self._active or self._thread != get_ident() or _ACTIVE_SHARED_READ.get() is not self._token
                or _SHARED_READ_CAPABILITIES.get(self._token) is not self
                or writer.transaction.closed or writer._central.closed
                or registry is not None and registry is not writer.owner.registry
                or reader is not None and (not isinstance(reader, TransactionRecords) or reader._transaction is not writer)):
            raise SharedProjectError('shared_read_capability_expired', status=409)
        writer.owner.registry.authorize(writer._central, writer.owner.actor, writer.access.project_id,
                                         action='write', authority=writer.owner.authority)
        observed = _directory(data.root)
        database = self._database_path.stat()
        marker = writer.transaction.read('v2_shared_root', 'identity')
        if ((observed.st_dev, observed.st_ino) != data.root_identity or marker is None or self._marker is None
                or (database.st_dev, database.st_ino) != self._database_identity
                or marker != self._marker or marker.payload.get('project_id') != writer.access.project_id):
            raise SharedProjectError('shared_read_capability_root_changed', status=409)

    @property
    def actor(self):
        self.validate()
        return self._writer.owner.actor

    @property
    def namespace(self):
        self.validate()
        return '/'.join(('shared', self._writer.access.project_id, self._data.namespace))

    def lease_payload(self):
        from .material_origins import SharedProjectLease
        self.validate()
        owner, access, data = self._writer.owner, self._writer.access, self._data
        return SharedProjectLease(access.project_id, owner.actor.user_id, owner.actor.device_id, owner.actor.revision,
            access.project_revision, access.member_revision, access.account_revision, data.namespace,
            tuple(data.root_identity)).payload()


class _BorrowedRows(Sequence):
    def __init__(self, rows, validate):
        self._rows, self._validate = rows, validate

    def __len__(self):
        self._validate()
        return len(self._rows)

    def __getitem__(self, index):
        self._validate()
        value = self._rows[index]
        self._validate()
        return _BorrowedRows(value, self._validate) if isinstance(index, slice) else value


class _BorrowedMetadataConnection:
    def __init__(self, capability):
        self._capability = capability

    def execute(self, statement, *args, **kwargs):
        self._capability.validate()
        if statement.strip().casefold() != 'pragma database_list' or args or kwargs:
            raise SharedProjectError('shared_borrowed_connection_read_only', status=403)
        rows = self._capability._writer.transaction.connection.execute('PRAGMA database_list').fetchall()
        self._capability.validate()
        return _BorrowedRows(rows, self._capability.validate)

    @property
    def in_transaction(self):
        self._capability.validate()
        return self._capability._writer.transaction.connection.in_transaction

    def __getattr__(self, name):
        self._capability.validate()
        raise SharedProjectError('shared_borrowed_connection_read_only', status=403)


class _BorrowedSharedRecords:
    def __init__(self, capability):
        self._capability = capability

    @property
    def database_path(self):
        self._capability.validate()
        return self._capability._data.records.database_path

    @property
    def connection(self):
        self._capability.validate()
        return _BorrowedMetadataConnection(self._capability)

    def read(self, collection, identity):
        self._capability.validate()
        row = self._capability._writer.transaction.read(collection, identity)
        self._capability.validate()
        return row

    def list(self, collection):
        self._capability.validate()
        rows = self._capability._writer.transaction.list(collection)
        self._capability.validate()
        return _BorrowedRows(rows, self._capability.validate)

    @contextmanager
    def begin_read(self):
        self._capability.validate()
        yield self
        self._capability.validate()

    def __getattr__(self, name):
        self._capability.validate()
        raise SharedProjectError('shared_borrowed_records_read_only', status=403)
