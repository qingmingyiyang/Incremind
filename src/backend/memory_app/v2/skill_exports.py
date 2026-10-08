"""认识领域资格、显式用户审阅和导出事实的事务编排。"""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from backend.recognition import RecognitionError, WorkScope
from ..source_egress import SourceEgressService
from ..transaction_records import TransactionRecords
from .insights import insight_view
from .insight_validity import read_validity
from .policies import get, version
from .skill_package import validate_document, package_files, zip_package

COLLECTION = 'v2_skill_exports'
PREFERENCES = 'v2_skill_export_preferences'
_UNSET = object()


class SkillExports:
    def __init__(self, records, service, *, owner_id):
        self.records, self.service, self.owner_id = records, service, owner_id

    def _sources(self, reader, project, descriptors, scene, *, scope_version=None):
        if not isinstance(descriptors, list) or not 1 <= len(descriptors) <= 100:
            raise ValueError('skill_source_unavailable')
        scope, identities, result = WorkScope(self.owner_id, project), set(), []
        scope_version = scope_version or version('scope')
        reference = datetime.now(timezone.utc).isoformat()
        for descriptor in descriptors:
            if (not isinstance(descriptor, dict) or set(descriptor) != {'id', 'revision'}
                    or not isinstance(descriptor['id'], str) or descriptor['id'] in identities
                    or type(descriptor['revision']) is not int or descriptor['revision'] < 1):
                raise ValueError('skill_source_unavailable')
            identity = descriptor['id']
            row = reader.read('recognitions', identity)
            if (row is None or row.payload.get('scope') != {'user_id': self.owner_id, 'project_id': project}
                    or row.revision != descriptor['revision']):
                raise ValueError('skill_source_unavailable')
            try:
                qualified = self.service._qualified_recognition(reader, row)
            except RecognitionError as error:
                raise ValueError('skill_source_unavailable') from error
            # 用原读模型继承候选和整理稿的场景，同一事务内读取领域资格。
            view = insight_view(reader, scope, identity, service=SimpleNamespace(
                get_recognition=lambda *, scope, recognition_id: qualified
                if scope == qualified.scope and recognition_id == qualified.id else None))
            validity = read_validity(reader, identity)
            if view is None or not qualified.authorized or not get('skill_export', version='@1')(
                    view, scene=scene, validity=validity, reference=reference, scope_version=scope_version):
                raise ValueError('skill_source_unavailable')
            identities.add(identity)
            result.append({'number': len(result) + 1, **descriptor, 'text': row.payload['content'],
                'conditions': view['conditions'], 'scene': view['scene'], 'validity': validity,
                'source_snapshot': SourceEgressService(TransactionRecords(reader)).snapshot(scope,
                    [{'type': 'recognition', 'id': identity, 'revision': row.revision}])})
        return result

    def _row(self, reader, project, identity, expected_revision=_UNSET):
        row = reader.read(COLLECTION, identity)
        if row is None or row.payload.get('project_id') != project or row.payload.get('owner_id') != self.owner_id:
            raise ValueError('skill_export_unavailable')
        if expected_revision is not _UNSET and (type(expected_revision) is not int
                or expected_revision < 1 or row.revision != expected_revision):
            raise ValueError('skill_revision_conflict')
        return row

    def _changed(self, reader, row):
        saved = row.payload['sources']
        descriptors = [{key: source[key] for key in ('id', 'revision')} for source in saved]
        try:
            selected = row.payload['policy_versions']['scope']
            current = self._sources(reader, row.payload['project_id'], descriptors,
                row.payload['scene'], scope_version=selected)
        except ValueError:
            return True
        return current != saved

    def _view(self, reader, row):
        return {'id': row.object_id, **deepcopy(dict(row.payload)), 'revision': row.revision,
            'needs_update': self._changed(reader, row)}

    def _generation_origin(self, reader, project, sources, document, turn_id):
        if turn_id is None:
            return None
        binding = reader.read('v2_memory_turn_keys', turn_id)
        if (binding is None or binding.payload.get('identity', {}).get('kind') != 'memory.skill_export'
                or binding.payload['request']['scope']['project_id'] != project):
            raise ValueError('skill_generation_origin_invalid')
        request = binding.payload['request']
        materials = [{'type': 'recognition', 'id': source['id'], 'revision': source['revision'],
            'project_id': project} for source in sources]
        if request['privacy']['material_refs'] != materials:
            raise ValueError('skill_generation_origin_invalid')
        from .memory_turn import MemoryTurn
        store = MemoryTurn.store_for(self.records)
        saved = store.get_immutable_payload(turn_id, 'memory-generation-output-v1')
        events = tuple(store.events_after(turn_id))
        # 不可变结果可能先于终态落盘；原内核完成证明与原请求同时成立才接收。
        if (store.get_request(turn_id) != request or not events
                or events[-1]['type'] != 'turn.completed'
                or not any(event['type'] == 'model.completed' for event in events)
                or saved is None or saved[1]['output'] != document):
            raise ValueError('skill_generation_origin_invalid')
        return turn_id

    def create(self, project, *, sources, document, scene=None, frozen_sources=None, generation_turn_id=None):
        if scene is not None and (not isinstance(scene, str) or not scene.strip()):
            raise ValueError('invalid_skill_scene')
        with self.records.begin() as tx:
            frozen = self._sources(tx, project, sources, scene)
            if frozen_sources is not None and frozen != frozen_sources:
                raise ValueError('skill_sources_changed')
            document = validate_document(document, source_count=len(frozen))
            origin = self._generation_origin(tx, project, frozen, document, generation_turn_id)
            if origin is not None:
                # 相同生成请求重放只返回已有草稿，保留用户后续的编辑和审阅事实。
                existing = next((item for item in tx.list(COLLECTION)
                    if item.payload.get('owner_id') == self.owner_id
                    and item.payload.get('project_id') == project
                    and origin in item.payload.get('generation_turn_ids',
                        [item.payload.get('generation_turn_id')])), None)
                if existing is not None:
                    return self._view(tx, existing)
            row = tx.put(COLLECTION, 'skill-export-' + uuid4().hex, {
                'owner_id': self.owner_id, 'project_id': project, 'scene': scene, 'sources': frozen,
                'document': document, 'reviewed': False, 'reviewed_by': None, 'exported_revision': None,
                'document_version': 1, 'generation_turn_id': origin,
                'generation_turn_ids': [origin] if origin is not None else [],
                'policy_versions': {'skill_export': '@1', 'scope': version('scope')}},
                expected_revision=0)
            result = self._view(tx, row)
            tx.commit()
        return result

    def get(self, project, identity):
        with self.records.begin() as tx:
            return self._view(tx, self._row(tx, project, identity))

    def require_revision(self, project, identity, expected_revision):
        with self.records.begin() as tx:
            return self._view(tx, self._row(tx, project, identity, expected_revision))

    def sources(self, project, descriptors, scene=None, *, reader=None):
        if reader is not None:
            return self._sources(reader, project, descriptors, scene)
        with self.records.begin() as tx:
            return self._sources(tx, project, descriptors, scene)

    def methods(self, project, scene=None):
        with self.records.begin() as tx:
            result = []
            for row in tx.list('recognitions'):
                if row.payload.get('scope') != {'user_id': self.owner_id, 'project_id': project}:
                    continue
                try:
                    result.extend(self._sources(tx, project, [{'id': row.object_id, 'revision': row.revision}], scene))
                except ValueError:
                    continue
            return result

    def list(self, project):
        with self.records.begin() as tx:
            return [self._view(tx, row) for row in tx.list(COLLECTION)
                if row.payload.get('project_id') == project and row.payload.get('owner_id') == self.owner_id]

    def preferences(self):
        with self.records.begin() as tx:
            row = tx.read(PREFERENCES, self.owner_id)
            return {'confirmed': bool(row and row.payload.get('confirmed') is True),
                'revision': row.revision if row else 0}

    def edit(self, project, identity, *, expected_revision, document):
        with self.records.begin() as tx:
            current = self._row(tx, project, identity, expected_revision)
            document = validate_document(document, source_count=len(current.payload['sources']))
            row = tx.put(COLLECTION, identity, {**current.payload, 'document': document,
                'document_version': current.payload['document_version'] + 1,
                'reviewed': False, 'reviewed_by': None}, expected_revision=current.revision)
            result = self._view(tx, row)
            tx.commit()
        return result

    def regenerate(self, project, identity, *, expected_revision, sources, document,
            scene=None, frozen_sources=None, generation_turn_id=None):
        if scene is not None and (not isinstance(scene, str) or not scene.strip()):
            raise ValueError('invalid_skill_scene')
        with self.records.begin() as tx:
            current = self._row(tx, project, identity, expected_revision)
            frozen = self._sources(tx, project, sources, scene)
            if frozen_sources is not None and frozen != frozen_sources:
                raise ValueError('skill_sources_changed')
            document = validate_document(document, source_count=len(frozen))
            origin = self._generation_origin(tx, project, frozen, document, generation_turn_id)
            origins = list(current.payload.get('generation_turn_ids',
                [current.payload['generation_turn_id']] if current.payload.get('generation_turn_id') else []))
            if origin is not None and origin not in origins:
                origins.append(origin)
            row = tx.put(COLLECTION, identity, {**current.payload, 'sources': frozen,
                'scene': scene, 'document': document, 'document_version': current.payload['document_version'] + 1,
                'reviewed': False, 'reviewed_by': None,
                'generation_turn_id': origin,
                'generation_turn_ids': origins,
                'policy_versions': {'skill_export': '@1', 'scope': version('scope')}},
                expected_revision=current.revision)
            result = self._view(tx, row)
            tx.commit()
        return result

    def review(self, project, identity, *, expected_revision):
        with self.records.begin() as tx:
            current = self._row(tx, project, identity, expected_revision)
            if self._changed(tx, current):
                raise ValueError('skill_sources_changed')
            validate_document(dict(current.payload['document']), source_count=len(current.payload['sources']))
            row = tx.put(COLLECTION, identity, {**current.payload,
                'reviewed': True, 'reviewed_by': self.owner_id}, expected_revision=current.revision)
            result = self._view(tx, row)
            tx.commit()
        return result

    def _approved_files(self, reader, current):
        if self._changed(reader, current):
            raise ValueError('skill_sources_changed')
        if not current.payload['reviewed'] or current.payload['reviewed_by'] != self.owner_id:
            raise ValueError('skill_review_required')
        # 本地预览允许手写；发出的包仍排除私密及已撤回的来源。
        authority = SourceEgressService(TransactionRecords(reader))
        for source in current.payload['sources']:
            try:
                authority.require(source['source_snapshot'], 'generation')
            except RecognitionError as error:
                raise ValueError('skill_source_private') from error
        return package_files(dict(current.payload['document']), sources=current.payload['sources'],
            version=current.payload['document_version'])

    def download(self, project, identity, *, expected_revision):
        with self.records.begin() as tx:
            current = self._row(tx, project, identity, expected_revision)
            package = zip_package(self._approved_files(tx, current))
            tx.put(COLLECTION, identity, {**current.payload, 'exported_revision': current.revision},
                expected_revision=current.revision)
            tx.commit()
        return package

    def export_folder(self, project, identity, *, expected_revision, directory,
            confirm_first_export, expected_confirmation_revision):
        from .skill_folder import write_reviewed_folder
        if type(confirm_first_export) is not bool:
            raise ValueError('skill_folder_confirmation_required')
        with self.records.begin() as tx:
            current = self._row(tx, project, identity, expected_revision)
            preference = tx.read(PREFERENCES, self.owner_id)
            revision = preference.revision if preference else 0
            if type(expected_confirmation_revision) is not int or expected_confirmation_revision != revision:
                raise ValueError('skill_folder_confirmation_conflict')
            confirmed = bool(preference and preference.payload.get('confirmed') is True)
            if not confirmed and not confirm_first_export:
                raise ValueError('skill_folder_confirmation_required')
            files = self._approved_files(tx, current)
            name = current.payload['document']['name']
            prefix = name + '/'
            relative = {path.removeprefix(prefix): body for path, body in files.items()}
            folder = write_reviewed_folder(directory, name, relative)
            if not confirmed:
                tx.put(PREFERENCES, self.owner_id, {'confirmed': True}, expected_revision=revision)
            row = tx.put(COLLECTION, identity, {**current.payload, 'exported_revision': current.revision,
                'folder_path': folder}, expected_revision=current.revision)
            result = self._view(tx, row)
            tx.commit()
        return result
