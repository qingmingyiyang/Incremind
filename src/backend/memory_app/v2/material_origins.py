from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from backend.recognition import RecognitionConflict, WorkScope
from core.document_engine import SQLiteDocumentRepository
from ..source_egress import SourceEgressService, recognition_service
from .shared_projects import segment
from .material_origin_readers import ReadOnlyMaterialRecords, ReadOnlyMaterialSources, ReadOnlyMaterialDocuments
from .material_read_context import current_material_data


ORIGIN_KIND = 'material-origin-v1'


@dataclass(frozen=True)
class SharedProjectLease:
    project_id: str
    actor_user_id: str
    device_id: str
    device_revision: int
    project_revision: int
    member_revision: int
    account_revision: int
    data_namespace: str
    root_identity: tuple

    @classmethod
    def acquire(cls, registry, actor, project_id):
        with registry.records.begin_read() as tx:
            access = registry.authorize(tx, actor, project_id)
        with registry.roots.lease(project_id):
            data = registry.roots._data(project_id)
            lease = cls(project_id, actor.user_id, actor.device_id, actor.revision,
                access.project_revision, access.member_revision, access.account_revision,
                data.namespace, tuple(data.root_identity))
        lease.validate(registry, actor)
        return lease

    @property
    def namespace(self):
        return '/'.join(('shared', self.project_id, self.data_namespace))

    def payload(self):
        return {'project_id': self.project_id, 'actor_user_id': self.actor_user_id,
            'device_id': self.device_id, 'device_revision': self.device_revision,
            'project_revision': self.project_revision, 'member_revision': self.member_revision,
            'account_revision': self.account_revision, 'data_namespace': self.data_namespace,
            'root_identity': list(self.root_identity)}

    @classmethod
    def from_payload(cls, payload):
        expected = {'project_id', 'actor_user_id', 'device_id', 'device_revision', 'project_revision',
                    'member_revision', 'account_revision', 'data_namespace', 'root_identity'}
        if not isinstance(payload, dict) or set(payload) != expected:
            raise RecognitionConflict('material_origin_binding_invalid')
        for field in ('project_id', 'actor_user_id', 'device_id', 'data_namespace'):
            segment(payload[field])
        if any(type(payload[field]) is not int or payload[field] < (0 if field == 'member_revision' else 1)
               for field in ('device_revision', 'project_revision', 'member_revision', 'account_revision')):
            raise RecognitionConflict('material_origin_binding_invalid')
        identity = payload['root_identity']
        if not isinstance(identity, list) or len(identity) != 2 or any(type(value) is not int or value < 0 for value in identity):
            raise RecognitionConflict('material_origin_binding_invalid')
        return cls(**{**payload, 'root_identity': tuple(identity)})

    @contextmanager
    def _current_data(self, registry, actor):
        if (actor.user_id, actor.device_id, actor.revision) != (self.actor_user_id, self.device_id, self.device_revision):
            raise RecognitionConflict('material_origin_identity_changed')
        borrowed = current_material_data(registry, actor, self)
        if borrowed is not None:
            try:
                yield borrowed
            finally:
                current_material_data(registry, actor, self)
            return
        authority = {'project_revision': self.project_revision, 'member_revision': self.member_revision,
                     'account_revision': self.account_revision}
        # 只读材料也按中央资格、物理资料根的固定顺序复验，结束后才释放资格快照。
        with registry.records.begin_read() as tx:
            registry.authorize(tx, actor, self.project_id, authority=authority)
            with registry.roots.lease(self.project_id):
                data = registry.roots._data(self.project_id)
                if data.namespace != self.data_namespace or tuple(data.root_identity) != self.root_identity:
                    raise RecognitionConflict('material_origin_root_changed')
                yield data

    def validate(self, registry, actor):
        with self._current_data(registry, actor):
            return None

    @contextmanager
    def current(self, registry, actor):
        reader = SharedMaterialOwner(registry, actor, self)
        try:
            yield reader
        finally:
            reader._active = False


class SharedMaterialOwner:
    def __init__(self, registry, actor, lease):
        self.registry, self.actor, self.lease = registry, actor, lease
        self._active = True
        with lease._current_data(registry, actor) as data:
            self.records = ReadOnlyMaterialRecords(data.records.database_path, validate=self.validate_current)
            self.documents = ReadOnlyMaterialDocuments(SQLiteDocumentRepository(self.records, namespace_id=data.namespace),
                                                        validate=self.validate_current)
            self.sources = ReadOnlyMaterialSources(data.sources, validate=self.validate_current)
            self.root = Path(data.root)
        self.namespace, self.project_id = lease.namespace, lease.project_id

    @property
    def database_path(self):
        self.validate_current()
        return self.records.database_path

    def validate_current(self):
        if not self._active:
            raise RecognitionConflict('material_origin_borrow_expired')
        self.lease.validate(self.registry, self.actor)

    def snapshot(self, material):
        if (not isinstance(material, dict) or set(material) != {'type', 'id', 'revision', 'project_id'}
                or material['project_id'] != self.project_id):
            raise RecognitionConflict('material_origin_scope_changed')
        self.validate_current()
        descriptor = {field: material[field] for field in ('type', 'id', 'revision')}
        result = SourceEgressService(self.records).snapshot(WorkScope('local-user', self.project_id), [descriptor])
        self.validate_current()
        return result

    def validate_snapshot(self, snapshot, *, purpose=None):
        if snapshot.get('scope') != {'user_id': 'local-user', 'project_id': self.project_id}:
            raise RecognitionConflict('material_origin_scope_changed')
        self.validate_current()
        authority = SourceEgressService(self.records)
        authority.validate_snapshot(WorkScope('local-user', self.project_id), snapshot)
        if purpose is not None:
            authority.require(snapshot, purpose)
        self.validate_current()

    def recognition(self, recognition_id):
        self.validate_current()
        result = recognition_service(self.records).get_recognition(
            scope=WorkScope('local-user', self.project_id), recognition_id=segment(recognition_id))
        self.validate_current()
        return result
