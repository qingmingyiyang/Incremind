from collections.abc import Mapping
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
import re

from backend.recognition import WorkScope
from ..constraints import ProjectConstraintError, ProjectConstraintService
from ..transaction_records import TransactionRecords
from .policies import get


SCOPES = 'v2_constraint_scopes'
VERSION = '@1'
_NAMESPACE = re.compile(r'^[a-z0-9][a-z0-9_-]*$')


class ProjectConstraintScopes:
    def __init__(self, records, *, namespace, owner_user_id, origin_kind, now=None, validate_owner=None):
        if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace):
            raise ProjectConstraintError('constraint_namespace_invalid')
        if not isinstance(owner_user_id, str) or not owner_user_id.strip():
            raise ProjectConstraintError('constraint_owner_invalid')
        if origin_kind not in {'personal', 'shared'}:
            raise ProjectConstraintError('constraint_origin_invalid')
        if origin_kind == 'shared' and not callable(validate_owner):
            raise ProjectConstraintError('shared_constraint_owner_unavailable')
        self.records, self.namespace, self.owner_user_id = records, namespace, owner_user_id
        self.origin_kind, self.now, self.validate_owner = origin_kind, now, validate_owner
        self._identity = self._physical_identity()

    def _physical_identity(self):
        path = Path(self.records.database_path).resolve(strict=True)
        database, directory = path.stat(), path.parent.stat()
        return {'database_path': str(path), 'database': [database.st_dev, database.st_ino],
                'directory': [directory.st_dev, directory.st_ino]}

    def _origin(self):
        if self.validate_owner is not None:
            self.validate_owner()
        if self._physical_identity() != self._identity:
            raise ProjectConstraintError('constraint_root_changed')
        return {'kind': self.origin_kind, 'namespace': self.namespace,
                'owner_user_id': self.owner_user_id, 'physical_identity': deepcopy(self._identity)}

    @staticmethod
    def _project(tx, scope):
        if not isinstance(scope, WorkScope) or not isinstance(scope.project_id, str):
            raise ProjectConstraintError('constraint_project_invalid')
        project = tx.read('v2_projects', scope.project_id)
        if project is None:
            raise ProjectConstraintError('constraint_project_unavailable')
        scenes = project.payload.get('scenes')
        if not isinstance(scenes, list) or any(not isinstance(scene, str) or not scene for scene in scenes):
            raise ProjectConstraintError('constraint_project_scenes_invalid')
        return project, scenes

    @staticmethod
    def _scope_setting(tx, scope, constraint_id):
        row = tx.read(SCOPES, constraint_id)
        if row is None:
            return {'mode': 'always', 'scene': None, 'keywords': []}, 0
        expected_scope = {'user_id': scope.user_id, 'project_id': scope.project_id}
        if (set(row.payload) != {'scope', 'policy_version', 'setting'}
                or row.payload['scope'] != expected_scope):
            raise ProjectConstraintError('constraint_scope_binding_changed')
        if row.payload['policy_version'] != VERSION:
            raise ProjectConstraintError('constraint_scope_policy_unavailable')
        policy = get('constraint_scope', version=row.payload['policy_version'])()
        setting = policy.normalize(**row.payload['setting'])
        if setting != row.payload['setting']:
            raise ProjectConstraintError('constraint_scope_setting_changed')
        return setting, row.revision

    def _list(self, tx, scope):
        project, scenes = self._project(tx, scope)
        original = ProjectConstraintService(TransactionRecords(tx), now=self.now).list(scope)
        items = []
        for row in original:
            setting, revision = self._scope_setting(tx, scope, row['id'])
            items.append({**row, 'activation': setting, 'scope_revision': revision,
                          'scope_available': setting['mode'] != 'scene' or setting['scene'] in scenes})
        return {'items': items, 'scenes': list(scenes), 'project_revision': project.revision}

    def list(self, scope):
        self._origin()
        with self.records.begin_read() as tx:
            result = self._list(tx, scope)
            self._origin()
        return result

    def upsert(self, scope, constraint_id, expected_revision, content, *, expected_scope_revision,
               expected_project_revision, mode, scene=None, keywords=(), enabled=True,
               valid_from=None, valid_until=None):
        self._origin()
        if type(expected_scope_revision) is not int or expected_scope_revision < 0:
            raise ProjectConstraintError('constraint_scope_revision_invalid')
        if type(expected_project_revision) is not int or expected_project_revision < 1:
            raise ProjectConstraintError('constraint_project_revision_invalid')
        setting = get('constraint_scope', version=VERSION)().normalize(mode=mode, scene=scene, keywords=keywords)
        with self.records.begin() as tx:
            project, scenes = self._project(tx, scope)
            if project.revision != expected_project_revision:
                raise ProjectConstraintError('constraint_project_changed')
            previous, revision = self._scope_setting(tx, scope, constraint_id)
            if revision != expected_scope_revision:
                raise ProjectConstraintError('constraint_scope_revision_conflicted')
            if setting['mode'] == 'scene' and setting['scene'] not in scenes:
                raise ProjectConstraintError('constraint_scene_unavailable')
            result = ProjectConstraintService(self.records, now=self.now).upsert_in_uow(
                tx, scope, constraint_id, expected_revision, content,
                enabled=enabled, valid_from=valid_from, valid_until=valid_until)
            if setting != previous:
                stored = tx.put(SCOPES, constraint_id, {
                    'scope': {'user_id': scope.user_id, 'project_id': scope.project_id},
                    'policy_version': VERSION, 'setting': setting}, expected_revision=revision)
                revision = stored.revision
            self._origin()
            tx.commit()
        return {**result, 'activation': setting, 'scope_revision': revision, 'scope_available': True}

    def activation_limits(self):
        policy = get('constraint_scope', version=VERSION)()
        return {'max_keywords': policy.max_keywords, 'max_keyword_chars': policy.max_keyword_chars}

    def preview_activation(self, scope, *, mode, scene, keywords):
        self._origin()
        setting = get('constraint_scope', version=VERSION)().normalize(mode=mode, scene=scene, keywords=keywords)
        with self.records.begin_read() as tx:
            project, scenes = self._project(tx, scope)
            if setting['mode'] == 'scene' and setting['scene'] not in scenes:
                raise ProjectConstraintError('constraint_scene_unavailable')
            self._origin()
        return {'activation': setting, 'project_revision': project.revision, 'limits': self.activation_limits()}

    def prepare_bundle(self, scope, *, scene, text):
        if not isinstance(text, str) or not text.strip():
            raise ProjectConstraintError('constraint_input_invalid')
        return self._prepare_bundle(scope, scene=scene, text=text)

    def prepare_automatic_bundle(self, scope, *, scene, reader=None):
        # 自动生成没有新的用户正文，材料和系统指令不能触发显式点名。
        if not isinstance(scope, WorkScope) or scope.user_id != 'local-user':
            raise ProjectConstraintError('constraint_scope_invalid')
        if reader is not None:
            from backend.security.audited_records import _AuditedTransaction
            from core.storage_provider.sqlite_uow import SQLiteStructuredRecordUnitOfWork
            view = TransactionRecords(reader)
            actual = view._transaction
            if isinstance(actual, _AuditedTransaction):
                actual = actual.transaction
            if (not isinstance(actual, SQLiteStructuredRecordUnitOfWork) or actual.closed
                    or not actual.connection.in_transaction
                    or Path(view.database_path).resolve(strict=True)
                    != Path(self.records.database_path).resolve(strict=True)):
                raise ProjectConstraintError('constraint_reader_unavailable')
            reader = view
        return self._prepare_bundle(scope, scene=scene, text='', reader=reader)

    def _prepare_bundle(self, scope, *, scene, text, reader=None):
        origin = self._origin()
        if scene is not None and (not isinstance(scene, str) or not scene):
            raise ProjectConstraintError('constraint_input_scene_invalid')
        policy = get('constraint_scope', version=VERSION)()
        with self.records.begin_read() if reader is None else nullcontext(reader) as tx:
            view = self._list(tx, scope)
            if scene is not None and scene not in view['scenes']:
                raise ProjectConstraintError('constraint_input_scene_unavailable')
            selected = [row for row in view['items'] if row['effective'] and policy.matches(
                row['activation'], scene=scene, text=text, available_scenes=view['scenes'])]
            fields = ('id', 'revision', 'scope_revision', 'content', 'valid_from', 'valid_until', 'activation')
            items = [{key: deepcopy(row[key]) for key in fields} for row in selected]
            if self._origin() != origin:
                raise ProjectConstraintError('constraint_owner_changed')
        return {'version': 'project-constraints@1', 'policy_version': VERSION, 'origin': origin,
                'scope': {'user_id': scope.user_id, 'project_id': scope.project_id},
                'scene': scene, 'text': text, 'items': items, 'prompt': policy.prompt(items)}

    def validate_bundle(self, bundle, scope, *, scene, text):
        if not isinstance(bundle, Mapping):
            raise ProjectConstraintError('constraint_bundle_invalid')
        if self.prepare_bundle(scope, scene=scene, text=text) != bundle:
            raise ProjectConstraintError('constraint_bundle_changed')
        return deepcopy(bundle)

    def binding_ref(self, native_turn_id):
        if (not isinstance(native_turn_id, str) or not native_turn_id
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._~-]{0,127}', native_turn_id) is None):
            raise ProjectConstraintError('constraint_turn_invalid')
        return {'kind': 'session_event', 'object_id': native_turn_id,
                'uri': f'crp://{self.namespace}/project-constraints/{native_turn_id}'}

    def prepare_admission(self, native_turn_id, scope, *, scene, text):
        # 这里只准备内容；唯一原子保存由原内核的 admission bundle 完成。
        return {'kind': 'project-constraints-v1', 'ref': self.binding_ref(native_turn_id),
                'payload': {'native_turn_id': native_turn_id,
                            'bundle': self.prepare_bundle(scope, scene=scene, text=text)}}

    def validate_admission_payload(self, payload, native_turn_id, scope, *, scene, text):
        if (not isinstance(payload, Mapping) or set(payload) != {'native_turn_id', 'bundle'}
                or payload['native_turn_id'] != native_turn_id):
            raise ProjectConstraintError('constraint_admission_changed')
        return self.validate_bundle(payload['bundle'], scope, scene=scene, text=text)
