import asyncio
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from backend.memory_app.document_recognition import ensure_document_experience
from backend.memory_app.source_egress import POLICY_COLLECTIONS, SourceEgressService, recognition_service
from backend.memory_app.transaction_records import TransactionRecords
from backend.memory_app.uploaded_media import group_entries
from backend.memory_app.workspace_confirmation import WorkspaceConfirmation, _refs
from backend.memory_app.workspace_generation import _draft
from backend.memory_app.server_jobs import retain_until_finished
from backend.recognition import WorkScope
from backend.security.user_context import authorize_user, user_context
from backend.shared.interprocess_lock import interprocess_file_lock
from core.document_engine import SQLiteDocumentRepository
from core.product_core.workbench_original_asset import _sha256_file
from core.storage_provider.persistence_budget import persistence_budget
from core.storage_provider.record_lineage import HEAD_COLLECTIONS, capture_lineage, verify_first_created, verify_lineage
from core.storage_provider.source_asset_authority import read_source_asset_authority
from backend.memory_app.backup_archive import safe_backup_path, _parts
from .backup_transfers import command_id
from .policies import get
from .privacy import is_private_project, set_private_project_in_transaction
from .projects import _name, assign_scene
from .recall_preferences import set_preference
from .shared_backup_archive import open_archive_project
from .shared_extraction import PersonalDestination
from .shared_extraction_admission import AdmittedSources
from .shared_projects import SharedProjectError


COMMANDS = 'v2_shared_backup_project_imports'
OBJECTS = 'v2_shared_backup_project_objects'


def _error(code):
    raise SharedProjectError('shared_backup_import_' + code, status=409)


def _merged_candidate(archive, candidate):
    # 已失效的候选沿归档历史资格保留，合并时的冻结修订仅约束仍待确认的候选。
    if candidate.payload.get('state') != 'pending':
        return False
    records, project_id = archive.records, archive.project_id
    collection = 'recognition_candidates'
    marker = records.read('v2_candidate_merges', candidate.object_id)
    if marker is None:
        return False
    value = marker.payload
    proposal_id, result_id = value.get('proposal_id'), value.get('candidate_id')
    scope = {'user_id': 'local-user', 'project_id': project_id}
    if (marker.revision != 1 or value.get('project_id') != project_id
            or not isinstance(proposal_id, str) or not proposal_id
            or not isinstance(result_id, str) or not result_id or result_id == candidate.object_id
            or candidate.payload.get('scope') != scope or candidate.payload.get('project_id') != project_id
            or candidate.payload.get('id') != candidate.object_id):
        _error('candidate_merge_binding_changed')
    proposal = records.read('recognition_restructure_proposals', proposal_id)
    result = records.read(collection, result_id)
    if (proposal is None or proposal.revision != 2 or result is None
            or result.payload.get('id') != result_id or result.payload.get('scope') != scope
            or result.payload.get('project_id') != project_id):
        _error('candidate_merge_binding_changed')
    approved = proposal.payload
    snapshot, outputs = approved.get('snapshot'), approved.get('outputs')
    source = {'id': candidate.object_id, 'revision': candidate.revision, 'payload': dict(candidate.payload)}
    if (approved.get('id') != proposal_id or approved.get('scope') != scope or approved.get('project_id') != project_id
            or approved.get('state') != 'approved' or approved.get('operation') != 'merge'
            or not approved.get('reviewed_by') or approved.get('result_candidate_ids') != [result_id]
            or not isinstance(snapshot, dict) or snapshot.get('scope') != scope
            or snapshot.get('schema_version') != 2 or snapshot.get('pending_output') is not True
            or not isinstance(snapshot.get('candidates'), list) or snapshot['candidates'].count(source) != 1
            or not isinstance(outputs, list) or len(outputs) != 1 or not isinstance(outputs[0], dict)
            or outputs[0].get('recognition_id') != result_id
            or any(outputs[0].get(key) != result.payload.get(key)
                for key in ('source_experience_ids', 'source_recognition_ids'))):
        _error('candidate_merge_binding_changed')
    head = records.read(HEAD_COLLECTIONS[collection], result_id)
    if head is None:
        _error('candidate_merge_binding_changed')
    verify_first_created(records, {'collection': collection, 'object_id': result_id, 'fact_id': head.payload['fact_id']},
        collection=collection, object_id=result_id)
    return True


class _Admission:
    def __init__(self, owner, actor, destination, check):
        self.owner, self.actor, self.destination, self.check = owner, actor, destination, check

    def guard(self):
        self.owner.backups._current(self.actor, self.destination)
        self.check(0)

    @contextmanager
    def phase(self, maximum):
        self.guard()
        cap = self.check.reserve_up_to(maximum)
        try:
            with persistence_budget(root=self.destination.root,
                    databases=(self.destination.records.database_path,
                               self.destination.root / '.rebuild-data/structured-records.sqlite3'), consume=cap.consume):
                yield cap
        finally:
            cap.close()


class _Writer:
    def __init__(self, admission):
        self.admission, self.records = admission, admission.destination.records
        self.command = None

    def __getattr__(self, name):
        return getattr(self.records, name)

    @contextmanager
    def begin(self):
        maximum = self.admission.owner.backups.limits['manifest_bytes']
        with self.admission.phase(maximum), self.records.begin() as tx:
            self.admission.guard()
            if self.command is not None:
                self.admission.owner._target(self.command, tx)
            yield tx


class SharedBackupImports:
    def __init__(self, backups):
        self.backups, self.context = backups, backups.context

    def _command(self, actor, destination, identity):
        self.backups._current(actor, destination)
        row = destination.records.read(COMMANDS, command_id(identity))
        if row is None:
            raise SharedProjectError('shared_backup_import_unavailable', status=404)
        if self.backups._owner_id(row.payload) != destination.owner_user_id:
            _error('owner_changed')
        return row

    def _view(self, row, records):
        objects = [value for value in records.list(OBJECTS) if value.payload['command_id'] == row.object_id]
        done = sum(value.payload['state'] == 'retained' for value in objects)
        return {'command_id': row.object_id, 'backup_id': row.payload['backup_id'],
            'project_id': row.payload['project_id'], 'name': row.payload['name'], 'state': row.payload['state'],
            'completed': done, 'total': len(objects), 'requires_resume': row.payload['state'] != 'completed',
            'requires_privacy_review': any(value.payload.get('privacy_reason') == 'archive_native_authority_not_portable'
                for value in objects),
            'revision': row.revision}

    def read(self, actor, destination, identity):
        return self._view(self._command(actor, destination, identity), destination.records)

    def _target(self, command, records):
        verify_lineage(records, command.payload['target_project_lineage'])
        project = records.read('v2_projects', command.payload['project_id'])
        privacy = records.read('v2_private_scopes', command.payload['project_id'])
        if (project is None or project.revision != command.payload['target_project_revision']
                or privacy is None or privacy.revision != command.payload['target_privacy_revision']):
            _error('target_project_changed')

    def _private(self, archive, collection, row):
        qualified = archive.qualifications.get((collection, row.object_id))
        if qualified is not None:
            if qualified['status'] == 'historical':
                return get('archive_copy', version='@1')({'project_private': True, 'explicit_private': True,
                    'ancestor_private': True, 'authority': 'portable'})
            if qualified['status'] != 'verified':
                _error('source_unverifiable')
            if qualified['authority'] == 'nonportable_native':
                return get('archive_copy', version='@1')({
                    'project_private': is_private_project(archive.records, archive.project_id),
                    'explicit_private': qualified['private'], 'ancestor_private': False,
                    'authority': 'nonportable_native'})
        scope = WorkScope('local-user', archive.project_id)
        kinds = {'workspace_items': 'original_item', 'recognition_experiences': 'experience', 'recognitions': 'recognition'}
        private = is_private_project(archive.records, archive.project_id)
        if collection in kinds:
            explicit = archive.records.read(POLICY_COLLECTIONS[kinds[collection]], row.object_id)
            private = private or explicit is not None and explicit.payload.get('allowed_purposes') == []
        if collection in kinds and row.payload.get('state', 'active') == 'active':
            # 原领域负责完整祖先与原生出生资格，缺自身源及无效 lineage 在此直接终止。
            policy = SourceEgressService(archive.records).policy(scope, kinds[collection], row.object_id)
            private = private or not policy['allowed_purposes']
        elif collection == 'documents':
            from backend.memory_app.original_sources import document_roots
            roots = document_roots(archive.records, scope, row.payload.get('source_refs', []), optional=False)
            if not roots:
                _error('document_source_missing')
            for kind, identity, _revision in roots:
                policy = SourceEgressService(archive.records).policy(scope, kind, identity)
                private = private or not policy['allowed_purposes']
        elif collection == 'recognition_candidates':
            from .shared_merge_privacy import PRIVACY
            private = private or (qualified['private'] if qualified is not None
                else archive.records.read(PRIVACY, row.object_id) is not None)
            for identity in row.payload.get('source_experience_ids', []):
                source = archive.records.read('recognition_experiences', identity)
                if source is None:
                    _error('candidate_source_missing')
                private = private or self._private(archive, 'recognition_experiences', source)['private']
            for identity in row.payload.get('source_recognition_ids', []):
                source = archive.records.read('recognitions', identity)
                if source is None:
                    _error('candidate_source_missing')
                private = private or self._private(archive, 'recognitions', source)['private']
        return get('archive_copy', version='@1')({'project_private': is_private_project(archive.records, archive.project_id),
            'explicit_private': private, 'ancestor_private': False, 'authority': 'portable'})

    def _plan(self, archive, actor, destination, command, writer):
        objects = []
        for collection in ('workspace_items', 'documents', 'recognition_experiences', 'recognitions', 'recognition_candidates'):
            for row in archive.rows(collection):
                value = dict(row.payload)
                if collection == 'recognition_candidates':
                    _merged_candidate(archive, row)
                    if value.get('state') == 'published':
                        continue
                title = value.get('title') or value.get('content', '').splitlines()[0][:80]
                text = (archive.documents.markdown(row.object_id, revision=value['revision']) if collection == 'documents'
                        else value.get('source_text', '') if collection == 'workspace_items' else value.get('content'))
                if not isinstance(text, str):
                    _error('body_missing')
                privacy = self._private(archive, collection, row)
                files = []
                if collection == 'workspace_items' and value.get('original_path'):
                    for entry in group_entries(archive.records, value):
                        files.append({**archive.file(entry['path']), 'name': entry['name']})
                objects.append({'id': 'archive-object-' + uuid4().hex, 'command_id': command.object_id,
                    'collection': collection, 'source_id': row.object_id, 'source_revision': row.revision,
                    'source_lineage': {'collection': collection, 'object_id': row.object_id,
                        'fact_id': archive.records.read(HEAD_COLLECTIONS[collection], row.object_id).payload['fact_id']},
                    'source_state': value.get('status', value.get('state')), 'source_payload': value,
                    'title': title, 'text': text, 'files': files, 'private': privacy['private'],
                    'privacy_reason': privacy['reason'], 'target_item_id': 'workspace-import-' + uuid4().hex,
                    'experience_id': 'experience-import-' + uuid4().hex,
                    'candidate_id': 'candidate-import-' + uuid4().hex,
                    'recognition_id': 'recognition-import-' + uuid4().hex, 'state': 'planned'})
        source_privacy = {}
        for source in archive.sources.list('sources'):
            if source.get('project_id', 'default') != archive.project_id:
                _error('source_scope_invalid')
            source_id = source['id']
            policy = SourceEgressService(archive.records).policy(WorkScope('local-user', archive.project_id),
                'original_source', source_id)
            private = not policy['allowed_purposes']
            source_privacy[source_id] = private
            metadata = source.get('metadata', {})
            text = metadata.get('content_snapshot') or metadata.get('content') or ''
            if not isinstance(text, str):
                _error('source_body_invalid')
            objects.append({'id': 'archive-object-' + uuid4().hex, 'command_id': command.object_id,
                'collection': 'original_sources', 'source_id': source_id,
                'source_revision': archive.sources.revision('sources', source_id), 'source_lineage': None,
                'source_incarnation': archive.sources.incarnation('sources', source_id),
                'source_state': source.get('processing_state'), 'source_payload': dict(source),
                'title': source.get('title') or '原件', 'text': text, 'files': [], 'private': private,
                'privacy_reason': None, 'target_item_id': 'workspace-import-' + uuid4().hex,
                'experience_id': 'experience-import-' + uuid4().hex,
                'candidate_id': 'candidate-import-' + uuid4().hex, 'recognition_id': 'recognition-import-' + uuid4().hex,
                'state': 'planned'})
        authority = read_source_asset_authority(json_store=archive._sources,
            sqlite_records=archive._sources._records if archive._sources.sqlite_active else None,
            authority_identity=archive._sources.authority_identity)
        blobs = {blob.sha256: blob for blob in authority.blobs}
        for asset in authority.assets:
            links = [link.source_id for link in authority.links if link.asset_id == asset.asset_id]
            if not links or any(identity not in source_privacy for identity in links):
                _error('asset_source_missing')
            blob = blobs[asset.blob_sha256]
            file = archive.file('library/' + blob.active_vault_ref)
            if file['sha256'] != asset.blob_sha256 or file['size'] != asset.byte_count:
                _error('asset_binding_changed')
            original_asset = dict(archive.sources.read('workbench_original_assets', asset.asset_id))
            name = (original_asset.get('display_name') or asset.metadata.get('original_filename')
                    or PureWindowsPath(blob.active_vault_ref).name)
            objects.append({'id': 'archive-object-' + uuid4().hex, 'command_id': command.object_id,
                'collection': 'source_assets', 'source_id': asset.asset_id,
                'source_revision': archive.sources.revision('workbench_original_assets', asset.asset_id),
                'source_lineage': None, 'source_state': 'retained', 'source_payload': original_asset,
                'source_links': links, 'title': name, 'text': '', 'files': [{**file, 'name': name}],
                'private': any(source_privacy[identity] for identity in links), 'privacy_reason': None,
                'target_item_id': 'workspace-import-' + uuid4().hex, 'candidate_id': 'candidate-import-' + uuid4().hex,
                'experience_id': 'experience-import-' + uuid4().hex,
                'recognition_id': 'recognition-import-' + uuid4().hex, 'state': 'planned'})
        projects = archive.rows('v2_projects')
        if len(projects) != 1 or projects[0].object_id != archive.project_id:
            _error('project_scope_invalid')
        project = projects[0]
        source_project_private = is_private_project(archive.records, archive.project_id)
        archive.verify()
        with writer.begin() as tx:
            current = tx.read(COMMANDS, command.object_id)
            if current.revision != command.revision:
                _error('command_changed')
            tx.put('v2_projects', command.payload['project_id'], {'name': command.payload['name'],
                'scenes': list(project.payload.get('scenes', [])), 'private': True, 'builtin': None}, expected_revision=0)
            set_private_project_in_transaction(tx, command.payload['project_id'], True, 0, source_owner=destination.records)
            for value in objects:
                tx.put(OBJECTS, value['id'], value, expected_revision=0)
            payload = {**command.payload, 'state': 'copying', 'source_project_id': archive.project_id,
                'source_namespace': archive.namespace, 'backup_at': archive.backup_time,
                'source_fingerprint': archive.snapshot.source_fingerprint, 'input_stage': archive.stage.name,
                'source_project_private': source_project_private,
                'target_project_lineage': capture_lineage(tx, 'v2_projects', command.payload['project_id']),
                'target_project_revision': 1, 'target_privacy_revision': 1}
            saved = tx.put(COMMANDS, command.object_id, payload, expected_revision=current.revision)
            tx.commit()
        return saved

    def _files(self, admission, archive, value):
        result = []
        workspace = safe_backup_path(admission.destination.root / 'workspace')
        workspace.mkdir(exist_ok=True)
        for number, file in enumerate(value['files'], 1):
            if PureWindowsPath(file['name']).name != file['name']:
                _error('asset_name_invalid')
            _parts(file['name'], self.backups.limits)
            source = archive.file(file['relative_path'])
            suffix = PureWindowsPath(file['name']).suffix.lower() or '.bin'
            name = value['target_item_id'] + ('' if number == 1 else '-' + str(number)) + suffix
            target = safe_backup_path(workspace / name)
            if target.exists():
                if target.is_symlink() or target.stat().st_nlink != 1 or _sha256_file(target) != file['sha256']:
                    _error('asset_target_changed')
            else:
                stage = safe_backup_path(archive.stage.parent / ('copy-' + value['id'] + '-' + str(number) + '.part'))
                existing = stage.stat().st_size if stage.exists() else 0
                if existing > file['size'] or stage.exists() and (stage.is_symlink() or stage.stat().st_nlink != 1):
                    _error('asset_stage_changed')
                with admission.phase(max(1, file['size'] - existing)) as cap:
                    if cap.remaining < file['size'] - existing:
                        _error('asset_budget_insufficient')
                    with Path(source['path']).open('rb') as original:
                        if existing:
                            with stage.open('rb') as prefix:
                                while chunk := prefix.read(self.backups.limits['chunk_bytes']):
                                    if original.read(len(chunk)) != chunk:
                                        _error('asset_prefix_changed')
                        with stage.open('ab') as output:
                            while chunk := original.read(self.backups.limits['chunk_bytes']):
                                cap.consume(len(chunk))
                                output.write(chunk)
                    if stage.stat().st_size != file['size'] or _sha256_file(stage) != file['sha256']:
                        _error('asset_changed')
                    stage.replace(target)
            result.append({'path': str(target), 'name': file['name']})
        return result

    def _copy(self, archive, actor, admission, writer, command, record):
        value, destination = dict(record.payload), admission.destination
        old_type = {'workspace_items': 'item', 'documents': 'document', 'recognitions': 'recognition',
            'recognition_candidates': 'candidate'}.get(value['collection'])
        scene = archive.records.read('v2_scene_assignments_' + old_type, value['source_id']) if old_type else None
        preference = (archive.records.read('recognition_recall_preferences', value['source_id'])
                      if value['collection'] == 'recognitions' else None)
        if scene is not None and scene.payload.get('project_id') != archive.project_id:
            _error('scene_scope_invalid')
        if preference is not None and preference.payload.get('project_id') != archive.project_id:
            _error('recall_scope_invalid')
        if value['source_lineage'] is not None:
            verify_lineage(archive.records, value['source_lineage'])
        elif value['collection'] == 'original_sources':
            if (archive.sources.revision('sources', value['source_id']) != value['source_revision']
                    or archive.sources.incarnation('sources', value['source_id']) != value['source_incarnation']):
                _error('source_changed')
        if (value['collection'] in {'recognition_experiences', 'recognition_candidates'}
                or value['collection'] == 'recognitions' and value['source_payload'].get('state') != 'active'):
            merged = value['collection'] == 'recognition_candidates' and _merged_candidate(
                archive, archive.records.read('recognition_candidates', value['source_id']))
            self._copy_statement(actor, admission, writer, command, record, scene, merged=merged)
            return
        files = self._files(admission, archive, value)
        item = destination.records.read('workspace_items', value['target_item_id'])
        project_id = command.payload['project_id']
        text, title = value['text'], value['title'] or '共享项目副本'
        draft = _draft({'title': title, 'summary': text[:5000] or title,
            'topics': [], 'facts': [], 'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}, text)
        if item is None:
            source = value['source_payload']
            confirmed = self._confirm_basis(value)
            kind = source.get('input_kind', 'text') if value['collection'] == 'workspace_items' else ('file' if files else 'text')
            def created(tx, target):
                current = tx.read(OBJECTS, record.object_id)
                if value['private']:
                    SourceEgressService(destination.records, reader=tx).set_policy(WorkScope('local-user', project_id),
                        'original_item', target.object_id, target.revision, 0, [])
                tx.put(OBJECTS, record.object_id, {**current.payload,
                    'target_lineage': capture_lineage(tx, 'workspace_items', target.object_id)}, expected_revision=current.revision)
            destination.workspace.items.with_records(writer).create_upload({'id': value['target_item_id'],
                'project_id': project_id, 'input_kind': kind, 'title': title, 'source_text': text,
                'status': 'ready' if confirmed or source.get('status') == 'ready' else 'staged',
                'draft': draft, 'error': None, 'document_id': None, 'created_at': self.backups.registry.now(),
                **({'original_path': files[0]['path'], 'original_name': files[0]['name']} if files else {}),
                **({'url': source['url']} if kind == 'link' and source.get('url') else {})},
                images=files if len(files) > 1 else None, stage_created=created)
            item = destination.records.read('workspace_items', value['target_item_id'])
        current = destination.records.read(OBJECTS, record.object_id)
        verify_lineage(destination.records, current.payload['target_lineage'])
        if item.payload.get('source_text') != text or item.payload.get('project_id') != project_id:
            _error('target_changed')
        confirmed = self._confirm_basis(value)
        document_id = None
        repository = SQLiteDocumentRepository(writer, namespace_id=destination.namespace)
        if confirmed:
            confirmation = WorkspaceConfirmation(destination.root, writer, repository)
            confirmation.source_store = AdmittedSources(confirmation.source_store, admission)
            reviewed = self._confirmation_revision(destination.records, item, project_id, draft, text or title)
            saved = confirmation.confirm(item.object_id, project_id, reviewed, lambda _value: text or title)
            document_id = saved['document_id']
            if repository.markdown(document_id, revision=1) != (text or title):
                _error('target_document_changed')
        with writer.begin() as tx:
            current = tx.read(OBJECTS, record.object_id)
            scope = WorkScope('local-user', project_id)
            enlisted = TransactionRecords(tx)
            service = recognition_service(enlisted, source_owner=destination.records)
            target_collection, target_id = 'workspace_items', item.object_id
            if value['private']:
                policy = tx.read(POLICY_COLLECTIONS['original_item'], item.object_id)
                if policy is None or policy.revision != 1 or policy.payload.get('allowed_purposes') != []:
                    _error('target_privacy_changed')
            if value['collection'] == 'recognitions':
                experience, _revision = ensure_document_experience(
                    SQLiteDocumentRepository(enlisted, namespace_id=destination.namespace), service, project_id, document_id)
                candidate = service.propose(scope=scope, content=text, source_experience_ids=[experience],
                    candidate_id=value['candidate_id'], conditions=value['source_payload'].get('conditions', []))
                published = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision,
                    reviewer=actor.user_id, recognition_id=value['recognition_id'])
                if not published.authorized:
                    _error('personal_evidence_invalid')
                target_collection, target_id = 'recognitions', published.id
            if value['collection'] == 'documents':
                target_collection, target_id = 'documents', document_id
                if value['source_payload'].get('status') == 'archived':
                    repository = SQLiteDocumentRepository(enlisted, namespace_id=destination.namespace)
                    repository.archive(document_id, expected_revision=repository.read(document_id)['revision'])
            if scene is not None:
                assign_scene(enlisted, old_type, target_id, project_id, scene.payload['scene'])
            if value['collection'] == 'recognitions' and tx.read('recognitions', target_id).payload['state'] == 'active':
                if preference is not None and preference.payload['state'] != 'normal':
                    set_preference(enlisted, scope, target_id, recognition_revision=tx.read('recognitions', target_id).revision,
                        preference_revision=0, state=preference.payload['state'])
            target = tx.read(target_collection, target_id)
            tx.put(OBJECTS, record.object_id, {**current.payload, 'state': 'retained',
                'target_collection': target_collection, 'target_id': target_id, 'target_revision': target.revision,
                'target_record_lineage': capture_lineage(tx, target_collection, target_id), 'document_id': document_id,
                'target_item_revision': tx.read('workspace_items', item.object_id).revision,
                'target_document_revision': tx.read('documents', document_id).revision if document_id else None,
                'target_document_lineage': capture_lineage(tx, 'documents', document_id) if document_id else None},
                expected_revision=current.revision)
            tx.commit()

    @staticmethod
    def _confirmation_revision(records, item, project_id, draft, markdown):
        if item.payload.get('draft') != draft:
            _error('target_confirmation_changed')
        if item.payload.get('status') != 'confirming':
            return item.revision
        operation = records.read('workspace_confirmation_operations', 'confirm-' + item.object_id)
        value = operation.payload if operation is not None else {}
        reviewed = value.get('reviewed_revision')
        source = value.get('source_payload', {})
        metadata = source.get('metadata') if isinstance(source, dict) else None
        if (value.get('id') != 'confirm-' + item.object_id or value.get('state') != 'pending'
                or value.get('workspace_item_id') != item.object_id or value.get('project_id') != project_id
                or value.get('source_id') != 'source-' + item.object_id
                or value.get('draft') != draft or value.get('markdown') != markdown
                or not isinstance(source, dict) or source.get('workspace_item_id') != item.object_id
                or source.get('project_id') != project_id or source.get('id') != value.get('source_id')
                or not isinstance(metadata, dict) or metadata.get('content_snapshot') != item.payload.get('source_text')
                or value.get('source_refs') != _refs(item.payload, value.get('source_id'))
                or type(reviewed) is not int or reviewed < 1 or item.payload.get('reviewed_revision') != reviewed):
            _error('target_confirmation_changed')
        return reviewed

    def _copy_statement(self, actor, admission, writer, command, record, scene, *, merged):
        value, destination = record.payload, admission.destination
        with writer.begin() as tx:
            enlisted, scope = TransactionRecords(tx), WorkScope('local-user', command.payload['project_id'])
            service = recognition_service(enlisted, source_owner=destination.records)
            experience = service.stage_experience(scope=scope, content=value['text'], experience_id=value['experience_id'],
                provenance={'kind': 'user_statement', 'actor': actor.user_id, 'source_refs': []})
            if value['private']:
                SourceEgressService(destination.records, reader=tx).set_policy(scope, 'experience', experience, 1, 0, [])
            collection, identity = 'recognition_experiences', experience
            if value['collection'] in {'recognitions', 'recognition_candidates'}:
                candidate = service.propose(scope=scope, content=value['text'], source_experience_ids=[experience],
                    conditions=value['source_payload'].get('conditions', []), candidate_id=value['candidate_id'])
                collection, identity = 'recognition_candidates', candidate.id
                if value['collection'] == 'recognitions':
                    published = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision,
                        reviewer=actor.user_id, recognition_id=value['recognition_id'])
                    service.revoke(scope=scope, recognition_id=published.id, expected_revision=published.revision,
                        reason='共享备份副本保留原停止状态')
                    collection, identity = 'recognitions', published.id
                elif merged or value['source_payload'].get('state') != 'pending':
                    service.reject_candidate(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision,
                        reviewer=actor.user_id)
            elif value['source_payload'].get('state') == 'revoked':
                service.revoke_experience(scope=scope, experience_id=experience, expected_revision=1)
            if scene is not None:
                assign_scene(enlisted, 'recognition' if collection == 'recognitions' else 'candidate',
                    identity, scope.project_id, scene.payload['scene'])
            current = tx.read(OBJECTS, record.object_id)
            target = tx.read(collection, identity)
            tx.put(OBJECTS, record.object_id, {**current.payload, 'state': 'retained',
                'target_collection': collection, 'target_id': identity, 'target_revision': target.revision,
                'target_record_lineage': capture_lineage(tx, collection, identity), 'document_id': None,
                'target_item_revision': None, 'target_document_revision': None, 'target_document_lineage': None},
                expected_revision=current.revision)
            tx.commit()

    @staticmethod
    def _confirm_basis(value):
        return (value['collection'] == 'documents'
            or value['collection'] == 'workspace_items' and value['source_payload'].get('status') == 'confirmed'
            or value['collection'] == 'recognitions' and value['source_payload'].get('state') == 'active')

    def restore(self, actor, destination, backup_id, identity, name, batch_size, check):
        identity, name = command_id(identity), _name(name, 'name')
        if type(batch_size) is not int or not 1 <= batch_size <= 32:
            _error('batch_invalid')
        admission = _Admission(self, actor, destination, check)
        writer = _Writer(admission)
        with interprocess_file_lock(self.backups._area(destination, backup_id) / ('import-' + identity), timeout_seconds=0):
            command = destination.records.read(COMMANDS, identity)
            if command is None:
                self.backups._archive(actor, destination, backup_id)
                with writer.begin() as tx:
                    names = {row.payload['name'] for row in tx.list('v2_projects')}
                    actual, number = name, 2
                    while actual in names:
                        actual = _name(name + ' (' + str(number) + ')', 'name')
                        number += 1
                    command = tx.put(COMMANDS, identity, {'backup_id': backup_id, 'requested_name': name,
                        'name': actual, 'project_id': 'project-' + uuid4().hex, 'actor_user_id': actor.user_id,
                        'owner_user_id': destination.owner_user_id,
                        'by': authorize_user(self.context.users, actor, destination.owner_user_id).by,
                        'approved_at': self.backups.registry.now(), 'state': 'preparing'}, expected_revision=0)
                    tx.commit()
            elif (self.backups._owner_id(command.payload) != destination.owner_user_id or command.payload['backup_id'] != backup_id
                    or command.payload['requested_name'] != name):
                _error('command_conflict')
            if command.payload['state'] == 'completed':
                return self._view(command, destination.records)
            archive = open_archive_project(self.backups, actor, destination, backup_id, identity, check,
                input_stage=command.payload.get('input_stage'))
            try:
                if command.payload['state'] == 'preparing':
                    if command.payload.get('input_stage') is None:
                        with writer.begin() as tx:
                            current = tx.read(COMMANDS, identity)
                            if current.revision != command.revision:
                                _error('command_changed')
                            command = tx.put(COMMANDS, identity, {**current.payload,
                                'input_stage': archive.stage.name,
                                'source_fingerprint': archive.snapshot.source_fingerprint}, expected_revision=current.revision)
                            tx.commit()
                    command = self._plan(archive, actor, destination, command, writer)
                writer.command = command
                self._target(command, destination.records)
                objects = [row for row in destination.records.list(OBJECTS) if row.payload['command_id'] == identity]
                for record in [row for row in objects if row.payload['state'] != 'retained'][:batch_size]:
                    archive.verify()
                    self._target(command, destination.records)
                    self._copy(archive, actor, admission, writer, command, record)
                objects = [row for row in destination.records.list(OBJECTS) if row.payload['command_id'] == identity]
                if all(row.payload['state'] == 'retained' for row in objects):
                    archive.verify()
                    with writer.begin() as tx:
                        self._target(command, tx)
                        for row in objects:
                            verify_lineage(tx, row.payload['target_record_lineage'])
                            target = tx.read(row.payload['target_collection'], row.payload['target_id'])
                            if target.revision != row.payload['target_revision']:
                                _error('target_changed')
                            if row.payload.get('target_item_revision') is not None:
                                verify_lineage(tx, row.payload['target_lineage'])
                                item = tx.read('workspace_items', row.payload['target_item_id'])
                                if item.revision != row.payload['target_item_revision']:
                                    _error('target_changed')
                            if row.payload.get('target_document_revision') is not None:
                                verify_lineage(tx, row.payload['target_document_lineage'])
                                if tx.read('documents', row.payload['document_id']).revision != row.payload['target_document_revision']:
                                    _error('target_changed')
                        privacy = tx.read('v2_private_scopes', command.payload['project_id'])
                        set_private_project_in_transaction(tx, command.payload['project_id'], command.payload['source_project_private'],
                            privacy.revision, source_owner=destination.records)
                        current = tx.read(COMMANDS, identity)
                        command = tx.put(COMMANDS, identity, {**current.payload, 'state': 'completed'}, expected_revision=current.revision)
                        tx.commit()
                return self._view(command, destination.records)
            finally:
                archive.close()

    async def run(self, actor, backup_id, identity, *, name, batch_size, owner_user_id=None):
        context = self.context
        with context.users.records.begin_read() as tx:
            context.native_mcp.credentials.full_caller(tx, actor)
        access = self.backups._access(actor, owner_user_id)
        with user_context(access):
            async with context.pool.lease(access.target_user_id) as application:
                destination = PersonalDestination.from_application(self.backups.registry, actor, application,
                    target_user_id=access.target_user_id)
                async with context.jobs.intake(access.target_user_id) as check:
                    async def execute():
                        return await retain_until_finished(asyncio.to_thread(self.restore, actor, destination,
                            backup_id, identity, name, batch_size, check))
                    return await retain_until_finished(context.jobs.submit(access.target_user_id, 'shared-backup-import', execute,
                        retain_running=True))

    async def query(self, actor, identity, *, owner_user_id=None):
        context = self.context
        access = self.backups._access(actor, owner_user_id)
        with user_context(access):
            async with context.pool.lease(access.target_user_id) as application:
                destination = PersonalDestination.from_application(self.backups.registry, actor, application,
                    target_user_id=access.target_user_id)
                return await retain_until_finished(asyncio.to_thread(self.read, actor, destination, identity))
