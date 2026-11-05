from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from stat import S_ISREG
from uuid import uuid4

from backend.recognition import WorkScope
from backend.recognition.external_turn_facts import source_store
from backend.security.user_context import USER_ACCESS, authorize_user, user_context
from backend.shared.interprocess_lock import interprocess_file_lock
from core.document_engine import SQLiteDocumentRepository
from core.product_core.workbench_original_asset import _sha256_file
from core.storage_provider.source_asset_authority import read_source_asset_authority
from ..document_recognition import ensure_document_experience
from ..original_sources import original
from ..source_egress import SourceEgressService, recognition_service
from ..server_audit import AdminAudit
from ..transaction_records import TransactionRecords
from ..uploaded_media import group_entries, original_path
from ..workspace_confirmation import WorkspaceConfirmation
from ..workspace_generation import _draft
from .insights import insight_view, source_documents, source_experiences
from .server_deletion import _directory
from .server_users import DELETIONS, USERS, current_caller
from .shared_projects import ACTIVITY, SharedProjectError, segment
from .shared_extraction_admission import AdmittedSources, current_admission, payload_bytes, run_admitted

PREVIEWS = 'v2_shared_extraction_previews'
MATERIALS = 'v2_shared_extraction_materials'
COPIES = 'v2_shared_extractions'
OPERATIONS = 'shared_extraction_operations'
COMMANDS = 'v2_shared_extraction_commands'


def now():
    return datetime.now(timezone.utc).isoformat()


def _personal_owner(operation):
    @wraps(operation)
    def run(self, actor, destination, *args, **kwargs):
        if operation.__name__ in {'preview', 'adopt', 'reconcile'}:
            if current_admission(self, destination).actor != actor:
                raise SharedProjectError('shared_extraction_identity_changed', status=409)
        expected = self.registry.users.server_root / 'users' / destination.owner_user_id
        if destination.root != expected:
            raise SharedProjectError('shared_personal_owner_mismatch', status=403)
        access = authorize_user(self.registry.users, actor, destination.owner_user_id)
        audit = AdminAudit(self.registry.users)
        intent = (audit.intent(access, method='GET' if operation.__name__ in {'progress', 'view_preview', 'retained_copy', 'preview_command'} else 'POST',
                               path='/api/v2/server/shared-projects/extractions')
                  if access.by == 'admin' and USER_ACCESS.get() != access else None)
        try:
            with user_context(access):
                result = operation(self, actor, destination, *args, **kwargs)
        except Exception as error:
            audit.outcome(intent, status=getattr(error, 'status', 500))
            raise
        audit.outcome(intent, status=200)
        return result
    return run


@dataclass(frozen=True)
class PersonalDestination:
    root: Path
    records: object
    documents: object
    workspace: object
    root_identity: tuple
    namespace: str
    owner_user_id: str

    @classmethod
    def from_application(cls, registry, actor, application, *, target_user_id=None):
        state = application.state
        target_user_id = actor.user_id if target_user_id is None else segment(target_user_id)
        registry.users.authorize_claim(actor, target_user_id)
        root = Path(state.recognition_runtime_root).absolute()
        expected = registry.users.server_root / 'users' / target_user_id
        if root != expected or not Path(state.recognition_records.database_path).is_relative_to(root):
            raise SharedProjectError('shared_personal_owner_mismatch', status=403)
        value = _directory(root)
        destination = cls(root, state.recognition_records, state.recognition_documents, state.workspace_domains,
            (value.st_dev, value.st_ino), state.recognition_documents.namespace_id, target_user_id)
        destination.validate_application(registry, application)
        return destination

    def validate_application(self, registry, application):
        self.validate()
        state = application.state
        context, models = getattr(state, 'server_context', None), state.recognition_models
        bound = getattr(models, 'server_model_access', None)
        if (self.root != registry.users.server_root / 'users' / self.owner_user_id
                or state.recognition_runtime_root != self.root or state.recognition_records is not self.records
                or state.recognition_documents is not self.documents or self.documents.namespace_id != self.namespace
                or state.workspace_domains is not self.workspace
                or context is None or context.users is not registry.users
                or getattr(state, 'server_user_id', None) != self.owner_user_id
                or models.root != self.root or models.records is not self.records
                or bound is None or bound._owner is not context.models
                or bound.user_id != self.owner_user_id or bound.user_root != self.root
                or models.server_model_balances is not context.model_balances):
            raise SharedProjectError('shared_personal_owner_mismatch', status=403)

    def validate(self):
        value = _directory(self.root)
        if (value.st_dev, value.st_ino) != self.root_identity:
            raise SharedProjectError('shared_personal_root_changed', status=409)


class _PersonalWriter:
    def __init__(self, extraction, actor, destination, preview):
        self.extraction, self.actor, self.destination, self.preview = extraction, actor, destination, preview
        self.database_path = destination.records.database_path

    def read(self, *args, **kwargs):
        return self.destination.records.read(*args, **kwargs)

    def list(self, *args, **kwargs):
        return self.destination.records.list(*args, **kwargs)

    @contextmanager
    def begin(self):
        admission = current_admission(self.extraction, self.destination)
        # 完整正文、JSON 来源与依赖验证在资格锁外；最终只复核修订及元数据。
        proof = self.extraction.validate_source(self.actor, self.preview)
        registry, project_id = self.extraction.registry, self.preview['shared_project_id']
        materials = [self.destination.records.read(MATERIALS, identity) for identity in self.preview['material_ids']]
        maximum = max(16384, 64 * (payload_bytes(self.preview) + self.preview.get('material_payload_bytes', 0)
            + sum(payload_bytes(row.payload) for row in materials if row is not None)))
        with admission.phase(maximum) as cap, registry.records.begin() as central, cap.using_reader(central):
            access = registry.authorize(central, self.actor, project_id, authority=self.preview['authority'])
            caller = current_caller(central, self.actor)
            target_user = self.destination.owner_user_id
            if (central.read(USERS, target_user) is None or central.read(DELETIONS, target_user) is not None
                    or target_user != self.actor.user_id and caller.payload['role'] != 'admin'):
                raise SharedProjectError('shared_personal_owner_unavailable', status=403)
            with registry.roots.lease(project_id):
                data = registry.roots._data(project_id)
                if data.root_identity != tuple(self.preview['root_identity']) or data.namespace != self.preview['source_namespace']:
                    raise SharedProjectError('shared_root_changed', status=409)
                if data.records.change_stamp() != proof['stamp']:
                    raise SharedProjectError('shared_extraction_source_changed', status=409)
                if self.extraction._json_stamp(data) != proof['json_stamp']:
                    raise SharedProjectError('shared_extraction_source_changed', status=409)
                store = source_store(data.records)
                for identity, revision, incarnation in proof['json']:
                    if store.revision('sources', identity) != revision or store.incarnation('sources', identity) != incarnation:
                        raise SharedProjectError('shared_extraction_source_changed', status=409)
                self.extraction._validate_files(self.preview)
                self.destination.validate()
                if (self.destination.root_identity != tuple(self.preview['target_root_identity'])
                        or self.destination.namespace != self.preview['target_namespace']):
                    raise SharedProjectError('shared_extraction_target_root_changed', status=409)
                with self.destination.records.begin() as tx:
                    target = tx.read('v2_projects', self.preview['target_project_id'])
                    if target is None or target.revision != self.preview['target_project_revision']:
                        raise SharedProjectError('shared_extraction_target_changed', status=409)
                    yield _CopyTransaction(tx, central, access, cap)


class _CopyTransaction:
    def __init__(self, transaction, central, access, cap):
        self.transaction, self.central, self.access = transaction, central, access
        self.cap = cap
        self.activity = None

    def __getattr__(self, name):
        return getattr(self.transaction, name)

    def commit(self):
        self.cap.validate()
        result = self.transaction.commit()
        if self.activity is not None:
            existing = self.central.read(ACTIVITY, self.activity['id'])
            if existing is not None and dict(existing.payload) != self.activity:
                raise SharedProjectError('shared_extraction_activity_conflict', status=409)
            if existing is None:
                self.central.put(ACTIVITY, self.activity['id'], self.activity, expected_revision=0)
            self.central.commit()
        return result


class SharedExtraction:
    def __init__(self, registry, *, server_context=None):
        self.registry = registry
        self.server_context, self._admissions = server_context, {}

    async def run_admitted(self, operation, actor, owner_user_id, *args, **kwargs):
        return await run_admitted(self, operation, actor, owner_user_id, *args, **kwargs)

    def _target(self, destination, project_id):
        destination.validate()
        row = destination.records.read('v2_projects', segment(project_id))
        if row is None or project_id in {'me', 'inbox'} or row.payload.get('shared'):
            raise SharedProjectError('shared_extraction_target_invalid', status=400)
        return row

    def _data(self, actor, project_id):
        with self.registry.records.begin_read() as tx:
            access = self.registry.authorize(tx, actor, project_id)
        with self.registry.roots.lease(project_id):
            data = self.registry.roots._data(project_id)
        return access, data

    def _original_material(self, data, project_id, node):
        scope = WorkScope('local-user', project_id)
        source = original(data.records, scope, node['type'], node['id'])
        payload = source.payload
        title = str(payload.get('title') or '原件')
        text = payload.get('source_text') if node['type'] == 'original_item' else (
            payload.get('metadata', {}).get('content_snapshot') or payload.get('metadata', {}).get('content') or '')
        if not isinstance(text, str):
            raise SharedProjectError('shared_extraction_original_text_invalid', status=409)
        files = []
        if node['type'] == 'original_item' and payload.get('original_path'):
            for index, entry in enumerate(group_entries(data.records, payload), 1):
                try:
                    path = original_path(data.root, payload, recorded_path=entry['path'], ordinal=index)
                except (ValueError, FileNotFoundError, NotADirectoryError) as error:
                    if isinstance(error, ValueError) and error.args != ('original_not_found',):
                        raise
                    raise SharedProjectError('shared_extraction_original_unavailable', status=409) from None
                files.append(self._file(path, data.root, entry['name']))
        elif node['type'] == 'original_source':
            authority = read_source_asset_authority(json_store=data.sources,
                sqlite_records=data.sources._records if data.sources.sqlite_active else None,
                authority_identity=data.sources.authority_identity)
            assets = {asset.asset_id: asset for asset in authority.assets}
            blobs = {blob.sha256: blob for blob in authority.blobs}
            for link in authority.links:
                if link.source_id != node['id'] or link.role != 'original':
                    continue
                asset = assets[link.asset_id]
                blob = blobs[asset.blob_sha256]
                path = data.root / 'library' / Path(blob.active_vault_ref)
                display = asset.metadata.get('original_filename') or title
                file = self._file(path, data.root, display)
                if file['size'] != asset.byte_count or file['sha256'] != asset.blob_sha256:
                    raise SharedProjectError('shared_extraction_asset_changed', status=409)
                files.append(file)
        return {'title': title, 'text': text, 'files': files}

    def _file(self, path, source_root, display_name):
        try:
            path = Path(path).resolve(strict=True)
            if not path.is_relative_to(source_root.resolve(strict=True)):
                raise SharedProjectError('shared_extraction_asset_path_invalid', status=409)
            value = path.stat()
            if not S_ISREG(value.st_mode):
                raise SharedProjectError('shared_extraction_asset_path_invalid', status=409)
            sha256 = _sha256_file(path)
        except (FileNotFoundError, NotADirectoryError):
            raise SharedProjectError('shared_extraction_original_unavailable', status=409) from None
        return {'path': str(path), 'name': Path(str(display_name)).name, 'size': value.st_size,
                'mtime_ns': value.st_mtime_ns, 'identity': [value.st_dev, value.st_ino], 'sha256': sha256}

    @_personal_owner
    def preview(self, actor, destination, shared_project_id, target_project_id, *, recognition_ids=None, request_key=None):
        if request_key is None:
            return self._prepare_preview(actor, destination, shared_project_id, target_project_id,
                recognition_ids=recognition_ids)
        key = segment(request_key)
        command = {'shared_project_id': shared_project_id, 'target_project_id': target_project_id,
            'recognition_ids': deepcopy(recognition_ids)}
        with self._command_lock(destination, key):
            existing = destination.records.read(COMMANDS, key)
            if existing is not None:
                preview = self._command_preview(actor, destination, key, existing)
                if existing.payload['request'] != command:
                    raise SharedProjectError('shared_extraction_request_conflict', status=409)
                return preview
            return self._prepare_preview(actor, destination, shared_project_id, target_project_id,
                recognition_ids=recognition_ids, command=(key, command))

    @contextmanager
    def _command_lock(self, destination, key):
        destination.validate()
        path = destination.root / '.rebuild-data' / 'shared-extraction-commands' / segment(key)
        try:
            with interprocess_file_lock(path, timeout_seconds=0):
                yield
        except TimeoutError:
            raise SharedProjectError('shared_extraction_preview_in_progress', status=409) from None

    def _command_preview(self, actor, destination, key, row):
        payload = row.payload
        if (payload.get('schema_version') != 1 or payload.get('command_id') != key
                or payload.get('actor_user_id') != actor.user_id
                or payload.get('owner_user_id') != destination.owner_user_id
                or payload.get('namespace') != destination.namespace):
            raise SharedProjectError('shared_extraction_command_unavailable', status=404)
        preview = self._preview(actor, destination, payload['preview_id']).payload
        request = payload['request']
        target = self._target(destination, request['target_project_id'])
        if (preview['shared_project_id'] != request['shared_project_id']
                or preview['target_project_id'] != request['target_project_id']
                or target.revision != preview['target_project_revision']
                or destination.namespace != preview['target_namespace']
                or destination.root_identity != tuple(preview['target_root_identity'])):
            raise SharedProjectError('shared_extraction_target_changed', status=409)
        return self.view_preview(actor, destination, payload['preview_id'])

    @_personal_owner
    def preview_command(self, actor, destination, request_key):
        key = segment(request_key)
        with self._command_lock(destination, key):
            row = destination.records.read(COMMANDS, key)
            if row is None:
                return {'command_id': key, 'state': 'unreceived'}
            preview = self._command_preview(actor, destination, key, row)
            return {'command_id': key, 'state': 'received', 'preview_id': preview['id']}

    def _prepare_preview(self, actor, destination, shared_project_id, target_project_id, *,
            recognition_ids=None, command=None):
        target = self._target(destination, target_project_id)
        access, data = self._data(actor, shared_project_id)
        scope = WorkScope('local-user', shared_project_id)
        rows = {row.object_id: row for row in data.records.list('recognitions')
                if row.payload.get('scope') == {'user_id': scope.user_id, 'project_id': scope.project_id}
                and row.payload.get('state') == 'active'}
        service = recognition_service(data.records)
        rows = {identity: row for identity, row in rows.items()
                if (qualified := service.get_recognition(scope=scope, recognition_id=identity)) is not None
                and qualified.authorized and qualified.effective_state == 'active'}
        views = {identity: insight_view(data.records, scope, identity, service=service) for identity in rows}
        if recognition_ids is not None and (not isinstance(recognition_ids, list)
                or any(not isinstance(identity, str) for identity in recognition_ids)):
            raise SharedProjectError('shared_extraction_selection_invalid')
        # 全部提取沿用当前召回范围；书架上的已遗忘认识仅接受明确逐条选择。
        identities = (sorted(identity for identity in rows if views[identity] and views[identity]['recall_state'] != 'forgotten')
                      if recognition_ids is None else sorted(set(recognition_ids)))
        if not identities or any(identity not in rows for identity in identities):
            raise SharedProjectError('shared_extraction_selection_invalid', status=409)
        if recognition_ids is not None and (not isinstance(recognition_ids, list) or len(identities) != len(recognition_ids)):
            raise SharedProjectError('shared_extraction_selection_invalid')
        preview_id, items, materials = 'extract-' + uuid4().hex, [], {}

        def material(kind, identity, revision, title, text, private, files=()):
            key = (kind, identity, revision)
            if key not in materials:
                materials[key] = {'id': 'material-' + uuid4().hex, 'kind': kind, 'source_id': identity,
                    'source_revision': revision, 'title': title[:200] or '原件', 'text': text,
                    'private': private, 'files': list(files), 'target_item_id': 'workspace-' + uuid4().hex}
            elif private:
                materials[key]['private'] = True
            return materials[key]['id']

        for identity in identities:
            row = rows[identity]
            authority = SourceEgressService(data.records)
            snapshot = authority.snapshot(scope, [{'type': 'recognition', 'id': identity, 'revision': row.revision}])
            private = any(not node['effective_purposes'] for node in snapshot['nodes'])
            basis = material('adoption', identity, row.revision, row.payload['content'].splitlines()[0][:80],
                             row.payload['content'], private)
            item_materials = [basis]
            experiences = source_experiences(data.records, scope, row.payload['source_experience_ids'],
                row.payload['source_recognition_ids'], experience_revisions=row.payload['source_experience_revisions'],
                recognition_revisions=row.payload['source_recognition_revisions'])
            for experience in experiences:
                item_materials.append(material('experience', experience.object_id, experience.revision,
                    '认识依据', experience.payload['content'], private))
            for document_id in source_documents(data.records, scope, [experience.object_id for experience in experiences]):
                document = data.documents.read(document_id)
                markdown = data.documents.markdown(document_id, revision=document['revision']) if document else None
                if document is None or not isinstance(markdown, str):
                    raise SharedProjectError('shared_extraction_document_unavailable', status=409)
                item_materials.append(material('document', document_id, document['revision'], document['title'], markdown, private))
            for node in snapshot['nodes']:
                if node['type'] not in {'original_item', 'original_source'}:
                    continue
                original_copy = self._original_material(data, shared_project_id, node)
                item_materials.append(material(node['type'], node['id'], node['source_revision'],
                    original_copy['title'], original_copy['text'], private))
                for index, file in enumerate(original_copy['files']):
                    item_materials.append(material('original_asset', node['id'] + '-' + str(index), node['source_revision'],
                        file['name'], original_copy['text'], private, [file]))
            items.append({'id': identity, 'revision': row.revision, 'text': row.payload['content'],
                'recall_state': views[identity]['recall_state'] if views[identity] else 'normal',
                'conditions': list(row.payload.get('conditions', ())), 'snapshot': snapshot, 'basis_material_id': basis,
                'material_ids': list(dict.fromkeys(item_materials)), 'state': 'pending',
                'copy_id': 'copy-' + uuid4().hex, 'candidate_id': 'candidate-' + uuid4().hex,
                'recognition_id': 'recognition-' + uuid4().hex})
        payload = {'id': preview_id, 'actor_user_id': actor.user_id, 'device_id': actor.device_id,
            'owner_user_id': destination.owner_user_id,
            'device_revision': actor.revision, 'shared_project_id': shared_project_id, 'source_namespace': data.namespace,
            'root_identity': list(data.root_identity), 'authority': access.authority,
            'target_project_id': target_project_id, 'target_project_revision': target.revision,
            'target_root_identity': list(destination.root_identity), 'target_namespace': destination.namespace,
            'created_at': now(), 'state': 'preview', 'confirmed_ids': None, 'items': items,
            'files': [file for entry in materials.values() for file in entry['files']],
            'material_payload_bytes': sum(payload_bytes(entry) for entry in materials.values()),
            'material_ids': [entry['id'] for entry in materials.values()]}
        self.validate_source(actor, payload)
        with _PersonalWriter(self, actor, destination, payload).begin() as tx:
            current = tx.read('v2_projects', target_project_id)
            if current is None or current.revision != target.revision:
                raise SharedProjectError('shared_extraction_target_changed', status=409)
            for entry in materials.values():
                tx.put(MATERIALS, entry['id'], {**entry, 'preview_id': preview_id, 'state': 'preview'}, expected_revision=0)
            saved = tx.put(PREVIEWS, preview_id, payload, expected_revision=0)
            if command is not None:
                key, request = command
                tx.put(COMMANDS, key, {'schema_version': 1, 'command_id': key,
                    'actor_user_id': actor.user_id, 'owner_user_id': destination.owner_user_id,
                    'namespace': destination.namespace, 'request': request, 'preview_id': preview_id,
                    'created_at': payload['created_at']}, expected_revision=0)
            tx.commit()
        return self.view_preview(actor, destination, preview_id, expected_revision=saved.revision)

    def validate_source(self, actor, preview):
        if actor.user_id != preview['actor_user_id'] or actor.device_id != preview['device_id'] or actor.revision != preview['device_revision']:
            raise SharedProjectError('shared_extraction_identity_changed', status=409)
        access, data = self._data(actor, preview['shared_project_id'])
        if access.authority != preview['authority'] or data.root_identity != tuple(preview['root_identity']):
            raise SharedProjectError('shared_extraction_authority_changed', status=409)
        before = data.records.change_stamp()
        json_before = self._json_stamp(data)
        authority, json = SourceEgressService(data.records), {}
        scope = WorkScope('local-user', preview['shared_project_id'])
        for item in preview['items']:
            authority.validate_snapshot(scope, item['snapshot'])
            for node in item['snapshot']['nodes']:
                if node['type'] == 'original_source':
                    json[node['id']] = (node['id'], node['source_revision'], node['incarnation'])
        self._validate_files(preview)
        if data.records.change_stamp() != before or self._json_stamp(data) != json_before:
            raise SharedProjectError('shared_extraction_source_changed', status=409)
        return {'stamp': before, 'json': list(json.values()), 'json_stamp': json_before}

    def _json_stamp(self, data):
        store, result = source_store(data.records), []
        for collection in ('sources', 'workbench_original_assets', 'source_asset_links'):
            path = store._collection_path(collection)
            if not path.exists():
                result.append((collection, None))
            else:
                value = path.stat()
                result.append((collection, (value.st_dev, value.st_ino, value.st_mtime_ns)))
        return result

    def _validate_files(self, preview):
        root = self.registry.roots.path(preview['shared_project_id']).resolve(strict=True)
        for file in preview.get('files', ()):
            path = Path(file['path']).resolve(strict=True)
            value = path.stat()
            if (not path.is_relative_to(root) or [value.st_dev, value.st_ino] != file['identity']
                    or value.st_size != file['size'] or value.st_mtime_ns != file['mtime_ns']):
                raise SharedProjectError('shared_extraction_asset_changed', status=409)

    def _preview(self, actor, destination, preview_id, *, require_actor=True):
        destination.validate()
        row = destination.records.read(PREVIEWS, segment(preview_id))
        if (row is None or require_actor and row.payload.get('actor_user_id') != actor.user_id
                or row.payload.get('owner_user_id') != destination.owner_user_id):
            raise SharedProjectError('shared_extraction_unavailable', status=404)
        return row

    @_personal_owner
    def view_preview(self, actor, destination, preview_id, *, expected_revision=None):
        row = self._preview(actor, destination, preview_id)
        if expected_revision is not None and row.revision != expected_revision:
            raise SharedProjectError('shared_extraction_preview_changed', status=409)
        self.validate_source(actor, row.payload)
        materials = [destination.records.read(MATERIALS, identity) for identity in row.payload['material_ids']]
        if any(material is None for material in materials):
            raise SharedProjectError('shared_extraction_material_unavailable', status=409)
        return {'id': row.object_id, 'revision': row.revision, 'state': row.payload['state'],
            'shared_project_id': row.payload['shared_project_id'], 'target_project_id': row.payload['target_project_id'],
            'recognition_ids': [item['id'] for item in row.payload['items']],
            'items': [{'id': item['id'], 'revision': item['revision'], 'text': item['text'], 'conditions': item['conditions'],
                       'state': item['state'], 'recall_state': item['recall_state']} for item in row.payload['items']],
            'materials': [{'id': material.object_id, 'title': material.payload['title'], 'text': material.payload['text'],
                           'private': material.payload['private'], 'file_names': [file['name'] for file in material.payload['files']]}
                          for material in materials]}

    def _prepared_files(self, destination, preview, material):
        admission = current_admission(self, destination)
        destination.validate()
        if destination.root_identity != tuple(preview['target_root_identity']) or destination.namespace != preview['target_namespace']:
            raise SharedProjectError('shared_extraction_target_root_changed', status=409)
        root = destination.root / 'workspace'
        _directory(root)
        prepared = []
        for index, file in enumerate(material['files'], 1):
            suffix = Path(file['name']).suffix.lower() or '.bin'
            target = root / (material['target_item_id'] + ('' if index == 1 else '-' + str(index)) + suffix)
            if target.parent != root or target.resolve(strict=False).parent != root.resolve(strict=True):
                raise SharedProjectError('shared_extraction_target_path_invalid', status=409)
            staging = destination.root / '.rebuild-data'
            _directory(staging)
            for directory in ('shared-extraction-staging', preview['id']):
                staging = staging / directory
                staging.mkdir(exist_ok=True)
                _directory(staging)
            with interprocess_file_lock(staging / material['id']):
                if target.exists():
                    value = target.lstat()
                    if (target.is_symlink() or getattr(value, 'st_file_attributes', 0) & 0x400 or value.st_nlink != 1
                            or value.st_size != file['size'] or _sha256_file(target) != file['sha256']):
                        raise SharedProjectError('shared_extraction_target_asset_conflict', status=409)
                else:
                    source = Path(file['path']).resolve(strict=True)
                    source_root = self.registry.roots.path(preview['shared_project_id']).resolve(strict=True)
                    if not source.is_relative_to(source_root):
                        raise SharedProjectError('shared_extraction_asset_path_invalid', status=409)
                    part = staging / (material['id'] + '-' + str(index) + '.part')
                    if part.resolve(strict=False) != part.absolute() or part.exists() and part.stat().st_nlink != 1:
                        raise SharedProjectError('shared_extraction_staging_path_invalid', status=409)
                    retained = part.stat().st_size if part.exists() else 0
                    if retained > file['size']:
                        raise SharedProjectError('shared_extraction_staging_asset_conflict', status=409)
                    admission.reserve(file['size'] - retained)
                    # 中断后的暂存只续写已逐字节核实的前缀，最终原件在完整校验后才可见。
                    with source.open('rb') as input_file:
                        if retained:
                            with part.open('rb') as existing:
                                while chunk := existing.read(1024 * 1024):
                                    if input_file.read(len(chunk)) != chunk:
                                        raise SharedProjectError('shared_extraction_staging_asset_conflict', status=409)
                        with part.open('ab') as output:
                            remaining = file['size'] - retained
                            while remaining:
                                chunk = input_file.read(min(1024 * 1024, remaining))
                                if not chunk:
                                    raise SharedProjectError('shared_extraction_asset_changed', status=409)
                                output.write(chunk)
                                remaining -= len(chunk)
                            if input_file.read(1):
                                raise SharedProjectError('shared_extraction_asset_changed', status=409)
                    if part.stat().st_size != file['size'] or _sha256_file(part) != file['sha256']:
                        raise SharedProjectError('shared_extraction_asset_changed', status=409)
                    part.replace(target)
                    admission.reserve()
            prepared.append({'path': str(target), 'name': file['name']})
        return prepared

    def _create_material(self, actor, destination, preview, material):
        if material.payload['state'] == 'retained':
            return
        payload = material.payload
        files = self._prepared_files(destination, preview, payload)
        text = payload['text']
        draft = _draft({'title': payload['title'], 'summary': text[:5000] or payload['title'],
            'topics': [], 'facts': [], 'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}, text)
        kind = 'text'
        extra = {}
        if files:
            suffix = Path(files[0]['name']).suffix.lower()
            kind = 'audio' if suffix in {'.mp3', '.wav', '.m4a', '.ogg', '.flac', '.webm'} else (
                'video' if suffix in {'.mp4', '.mkv', '.mov', '.avi'} else 'image' if suffix in {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'} else 'file')
            extra = {'original_path': files[0]['path'], 'original_name': files[0]['name']}
        writer = _PersonalWriter(self, actor, destination, preview)
        with writer.begin() as tx:
            current = tx.read(MATERIALS, material.object_id)
            if current is None or current.revision != material.revision:
                raise SharedProjectError('shared_extraction_material_changed', status=409)
            item = tx.read('workspace_items', payload['target_item_id'])
            if item is None:
                enlisted = TransactionRecords(tx)
                destination.workspace.items.with_records(enlisted).create_upload({
                    'id': payload['target_item_id'], 'project_id': preview['target_project_id'], 'input_kind': kind,
                    'title': payload['title'], 'source_text': text, 'status': 'ready', 'draft': draft,
                    'error': None, 'document_id': None, 'created_at': now(), **extra})
                if payload['private']:
                    SourceEgressService(destination.records, reader=tx).set_policy(WorkScope('local-user', preview['target_project_id']),
                        'original_item', payload['target_item_id'], 1, 0, [])
                tx.put(MATERIALS, material.object_id, {**current.payload, 'state': 'created'}, expected_revision=current.revision)
                tx.commit()
        repository = SQLiteDocumentRepository(writer, namespace_id=destination.namespace)
        confirmation = WorkspaceConfirmation(destination.root, writer, repository)
        confirmation.source_store = AdmittedSources(confirmation.source_store, current_admission(self, destination))
        item = destination.records.read('workspace_items', payload['target_item_id'])
        confirmation.confirm(payload['target_item_id'], preview['target_project_id'], item.revision,
                             lambda _draft: text or payload['title'])
        with writer.begin() as tx:
            current = tx.read(MATERIALS, material.object_id)
            item = tx.read('workspace_items', payload['target_item_id'])
            if item is None or item.payload['status'] != 'confirmed' or not item.payload.get('document_id'):
                raise SharedProjectError('shared_extraction_material_unconfirmed', status=409)
            tx.put(MATERIALS, material.object_id, {**current.payload, 'state': 'retained',
                'target_document_id': item.payload['document_id'], 'files': payload['files']}, expected_revision=current.revision)
            tx.commit()

    @_personal_owner
    def adopt(self, actor, destination, preview_id, *, expected_revision, confirmed_ids, batch_size=8):
        if type(batch_size) is not int or not 1 <= batch_size <= 32:
            raise SharedProjectError('shared_extraction_batch_invalid')
        row = self._preview(actor, destination, preview_id)
        preview = row.payload
        if self.progress(actor, destination, preview_id)['state'] == 'completed':
            return self.progress(actor, destination, preview_id)
        selection = [item['id'] for item in preview['items']]
        if not isinstance(confirmed_ids, list) or confirmed_ids != selection:
            raise SharedProjectError('shared_extraction_confirmation_mismatch', status=409)
        writer = _PersonalWriter(self, actor, destination, preview)
        if preview['state'] == 'preview':
            if row.revision != expected_revision:
                raise SharedProjectError('shared_extraction_preview_changed', status=409)
            with writer.begin() as tx:
                current = tx.read(PREVIEWS, preview_id)
                if current.revision != expected_revision:
                    raise SharedProjectError('shared_extraction_preview_changed', status=409)
                row = tx.put(PREVIEWS, preview_id, {**current.payload, 'state': 'pending',
                    'confirmed_ids': selection, 'confirmed_at': now()}, expected_revision=current.revision)
                tx.commit()
            preview = row.payload
            writer = _PersonalWriter(self, actor, destination, preview)
        elif preview['state'] == 'completed':
            return self.progress(actor, destination, preview_id)
        elif preview['state'] != 'pending' or preview['confirmed_ids'] != selection:
            raise SharedProjectError('shared_extraction_operation_invalid', status=409)
        with self.registry.records.begin() as central:
            self.registry.authorize(central, actor, preview['shared_project_id'], authority=preview['authority'])
            operation = central.read(OPERATIONS, preview_id)
            operation_payload = {'id': preview_id, 'actor_user_id': actor.user_id, 'owner_user_id': destination.owner_user_id,
                'project_id': preview['shared_project_id'], 'target_project_id': preview['target_project_id'],
                'recognition_ids': selection, 'copy_ids': [item['copy_id'] for item in preview['items']]}
            if operation is not None and any(operation.payload[key] != value for key, value in operation_payload.items()):
                raise SharedProjectError('shared_extraction_operation_conflict', status=409)
            if operation is None:
                central.put(OPERATIONS, preview_id, {**operation_payload, 'state': 'pending', 'created_at': now()}, expected_revision=0)
            central.commit()
        remaining = [item for item in preview['items'] if destination.records.read(COPIES, item['copy_id']) is None]
        for item in remaining[:batch_size]:
            self.validate_source(actor, preview)
            for identity in item['material_ids']:
                material = destination.records.read(MATERIALS, identity)
                if material is None or material.payload['preview_id'] != preview_id:
                    raise SharedProjectError('shared_extraction_material_unavailable', status=409)
                self._create_material(actor, destination, preview, material)
            basis = destination.records.read(MATERIALS, item['basis_material_id'])
            with writer.begin() as tx:
                if tx.read(COPIES, item['copy_id']) is not None:
                    continue
                enlisted = TransactionRecords(tx)
                repository = SQLiteDocumentRepository(enlisted, namespace_id=destination.namespace)
                service = recognition_service(enlisted, source_owner=destination.records)
                experience_id, _ = ensure_document_experience(repository, service, preview['target_project_id'],
                                                              basis.payload['target_document_id'])
                scope = WorkScope('local-user', preview['target_project_id'])
                candidate = service.propose(scope=scope, content=item['text'], conditions=item['conditions'],
                    source_experience_ids=[experience_id], candidate_id=item['candidate_id'])
                saved = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision,
                    reviewer=actor.user_id, recognition_id=item['recognition_id'])
                if not saved.authorized:
                    raise SharedProjectError('shared_extraction_personal_evidence_invalid', status=409)
                personal_snapshot = SourceEgressService(destination.records, reader=tx).snapshot(scope,
                    [{'type': 'recognition', 'id': saved.id, 'revision': saved.revision}])
                if basis.payload['private'] and any(node['effective_purposes'] for node in personal_snapshot['nodes']):
                    raise SharedProjectError('shared_extraction_personal_privacy_invalid', status=409)
                activity = {'id': preview_id + '--' + item['copy_id'], 'operation_id': preview_id,
                    'project_id': preview['shared_project_id'], 'actor_user_id': actor.user_id,
                    'by': 'admin' if destination.owner_user_id != actor.user_id else tx.access.by,
                    'at': now(), 'action': 'extract', 'object_kind': 'insight', 'object_id': item['id'],
                    'object_title': item['text'].splitlines()[0][:80]}
                tx.put(COPIES, item['copy_id'], {'id': item['copy_id'], 'actor_user_id': actor.user_id,
                    'owner_user_id': destination.owner_user_id, 'by': 'admin' if destination.owner_user_id != actor.user_id else None,
                    'shared_project_id': preview['shared_project_id'], 'source_namespace': preview['source_namespace'],
                    'source_insight_id': item['id'], 'source_insight_revision': item['revision'], 'source_snapshot': item['snapshot'],
                    'target_project_id': preview['target_project_id'], 'target_recognition_id': saved.id,
                    'target_revision': saved.revision, 'material_ids': item['material_ids'], 'preview_id': preview_id,
                    'activity': activity, 'state': 'retained', 'created_at': now()}, expected_revision=0)
                tx.activity = activity
                tx.commit()
        result = self.progress(actor, destination, preview_id)
        if result['remaining'] == 0:
            self.reconcile(actor, destination, preview_id)
            result = self.progress(actor, destination, preview_id)
        return result

    @_personal_owner
    def reconcile(self, actor, destination, preview_id):
        preview = self._preview(actor, destination, preview_id, require_actor=False).payload
        copies = [destination.records.read(COPIES, item['copy_id']) for item in preview['items']]
        if any(copy is None for copy in copies):
            raise SharedProjectError('shared_extraction_personal_copy_pending', status=409)
        # 个人内容回执已经提交；中央活动及共享投影按相同活动 ID 独立恢复。
        _access, data = self._data(actor, preview['shared_project_id'])
        if data.root_identity != tuple(preview['root_identity']) or data.namespace != preview['source_namespace']:
            raise SharedProjectError('shared_extraction_activity_root_changed', status=409)
        with self.registry.records.begin() as central:
            self.registry.authorize(central, actor, preview['shared_project_id'])
            operation = central.read(OPERATIONS, preview_id)
            if operation is None:
                raise SharedProjectError('shared_extraction_operation_unavailable', status=409)
            for copy in copies:
                activity = copy.payload['activity']
                current = central.read(ACTIVITY, activity['id'])
                if current is not None and dict(current.payload) != activity:
                    raise SharedProjectError('shared_extraction_activity_conflict', status=409)
                if current is None:
                    central.put(ACTIVITY, activity['id'], activity, expected_revision=0)
                self.registry.roots.project_activity(preview['shared_project_id'], activity)
            if operation.payload['state'] != 'completed':
                central.put(OPERATIONS, preview_id, {**operation.payload, 'state': 'completed', 'completed_at': now(),
                    'completed_by': actor.user_id}, expected_revision=operation.revision)
            central.commit()
        return self.progress(actor, destination, preview_id)

    @_personal_owner
    def progress(self, actor, destination, preview_id):
        row = self._preview(actor, destination, preview_id, require_actor=False)
        copies = [destination.records.read(COPIES, item['copy_id']) for item in row.payload['items']]
        completed = sum(copy is not None for copy in copies)
        operation = self.registry.records.read(OPERATIONS, preview_id)
        state = 'completed' if completed == len(copies) and operation and operation.payload['state'] == 'completed' else row.payload['state']
        return {'id': preview_id, 'state': state, 'completed': completed,
                'personal_state': 'completed' if completed == len(copies) else row.payload['state'],
                'activity_state': 'completed' if operation and operation.payload['state'] == 'completed' else 'pending',
                'remaining': len(copies) - completed, 'total': len(copies),
                'copies': [{'id': copy.object_id, 'target_project_id': copy.payload['target_project_id'],
                            'target_recognition_id': copy.payload['target_recognition_id']} for copy in copies if copy]}

    @_personal_owner
    def retained_copy(self, actor, destination, copy_id):
        row = destination.records.read(COPIES, segment(copy_id))
        if row is None or row.payload.get('owner_user_id') != destination.owner_user_id:
            raise SharedProjectError('shared_extraction_copy_unavailable', status=404)
        materials = []
        for identity in row.payload['material_ids']:
            material = destination.records.read(MATERIALS, identity)
            item = destination.records.read('workspace_items', material.payload['target_item_id']) if material else None
            if item is None or item.payload.get('project_id') != row.payload['target_project_id']:
                raise SharedProjectError('shared_extraction_personal_material_unavailable', status=409)
            files = []
            if item.payload.get('original_path'):
                path = original_path(destination.root, item.payload)
                files.append({'name': item.payload['original_name'], 'size': path.stat().st_size,
                    'url': '/api/workspace/v1/items/' + item.object_id + '/original?project_id=' + row.payload['target_project_id']})
            materials.append({'item_id': item.object_id, 'document_id': item.payload.get('document_id'),
                'title': item.payload['title'], 'text': item.payload['source_text'], 'files': files,
                'private': material.payload['private']})
        return {**row.payload, 'materials': materials, 'revision': row.revision}
