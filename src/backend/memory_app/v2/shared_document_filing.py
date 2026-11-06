import asyncio
from mimetypes import guess_type
import os
import re
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from backend.recognition import WorkScope
from backend.security.user_context import user_action_allowed
from core.document_engine import SQLiteDocumentRepository
from core.document_engine.ports import DocumentDraft
from core.product_core.workbench_original_asset import _sha256_file
from core.storage_provider.persistence_budget import persistence_budget
from core.storage_provider.record_lineage import capture_lineage, verify_lineage
from ..document_recognition import DocumentRecognitionError, ensure_document_experience
from ..original_sources import original
from ..server_jobs import retain_until_finished
from ..source_egress import SourceEgressService, recognition_service
from ..transaction_records import TransactionRecords
from .document_filings import SHARED_FILINGS, SHARED_IMPORTS, reject_source_candidates
from .layers import summary_of
from .placement import record_choice
from .privacy import is_private_project
from .projects import assign_scene
from .server_deletion import _directory
from .server_users import current_caller, enabled_user
from .shared_extraction import SharedExtraction
from .shared_extraction_admission import payload_bytes
from .shared_projects import PROJECTS, SharedProjectError, revision, segment
from .shared_runtime import SharedWriteRecords, shared_read_capability


def _conflict(code):
    return SharedProjectError(code, status=409)


def _request(body):
    if not isinstance(body, dict) or set(body) != {'source_project_id', 'expected_revision', 'scene', 'target_namespace'}:
        raise SharedProjectError('shared_filing_fields_invalid')
    project, scene = segment(body['source_project_id']), body['scene']
    if project.startswith('shared-'):
        raise SharedProjectError('shared_filing_source_invalid')
    if scene is not None and (not isinstance(scene, str) or not scene or len(scene) > 100):
        raise SharedProjectError('shared_filing_scene_invalid')
    if not isinstance(body['target_namespace'], str) or not 1 <= len(body['target_namespace']) <= 300:
        raise SharedProjectError('shared_filing_target_invalid')
    return {**body, 'expected_revision': revision(body['expected_revision'])}


def _authority(lease):
    return {'project_revision': lease.project_revision, 'member_revision': lease.member_revision,
        'account_revision': lease.account_revision}


def _destination_identity(query, document, key):
    personal = query.origin_admission.destination
    return 'shared-filed-v2-' + uuid5(NAMESPACE_URL,
        '/'.join((personal.owner_user_id, personal.namespace, query.source_namespace, document, key))).hex


def _authorize_source(central, admission):
    caller = current_caller(central, admission.actor)
    personal = admission.destination
    if (enabled_user(central, personal.owner_user_id) is None
            or not user_action_allowed(caller.payload['role'], admission.actor.user_id, personal.owner_user_id, 'space')):
        raise SharedProjectError('shared_filing_source_forbidden', status=403)


def _source_document(tx, personal, document_id, request):
    document = tx.read('documents', document_id)
    project = request['source_project_id']
    if (document is None or document.payload.get('project_id') != project
            or document.payload.get('status') == 'archived'
            or document.payload.get('revision') != request['expected_revision']):
        raise _conflict('shared_filing_source_changed')
    if is_private_project(tx, project):
        raise SharedProjectError('shared_filing_private_source', status=403)
    recall = tx.read('v2_document_recall', document_id)
    if recall is not None and recall.payload.get('by') == 'moved':
        raise _conflict('shared_filing_already_moved')
    repository = SQLiteDocumentRepository(TransactionRecords(tx), namespace_id=personal.namespace)
    markdown = repository.markdown(document_id, revision=request['expected_revision'])
    if not isinstance(markdown, str) or not markdown.strip():
        raise _conflict('shared_filing_source_changed')
    return document, repository, markdown, recall


def _originals(tx, personal, project, snapshot, transfer):
    data = SimpleNamespace(root=personal.root, records=TransactionRecords(tx),
        sources=personal.workspace.query.source_store)
    scope = WorkScope('local-user', project)
    result = []
    for node in snapshot['nodes']:
        kind, source_id = node['type'], node['id']
        if kind not in {'original_item', 'original_source'}:
            continue
        source = original(tx, scope, kind, source_id)
        if source.revision != node['source_revision']:
            raise _conflict('shared_filing_source_changed')
        if kind == 'original_source' and source.payload.get('identity_method') == 'workspace_confirmation':
            # 确认源的真实原件已在同一闭包中。
            continue
        value = SharedExtraction.read_transfer_original(data, project, node, include_alias_assets=True)
        files, primary_files = value['files'], value['workspace_files']
        input_kind = source.payload.get('input_kind')
        if input_kind not in {'text', 'link', 'audio', 'video', 'image', 'file', 'conversation'}:
            input_kind = 'file' if primary_files else 'text'
        groups = [(input_kind, files[:primary_files])] if kind == 'original_item' or not files else []
        for file in files[primary_files:]:
            media, _encoding = guess_type(file['name'])
            category = media.split('/')[0] if media else ''
            groups.append((category if category in {'image', 'audio', 'video'} else 'file', [file]))
        for index, (item_kind, group) in enumerate(groups):
            item_id = 'workspace-' + uuid5(NAMESPACE_URL, f'{transfer}/{kind}/{source_id}/{index}').hex
            result.append({'item_id': item_id, 'origin': {'type': kind, 'id': source_id,
                'revision': source.revision}, 'input_kind': item_kind, 'title': value['title'],
                'text': value['text'], 'files': group,
                'url': source.payload.get('url') if item_kind == 'link' else None,
                'created_at': source.payload.get('created_at')})
    if not result:
        raise _conflict('shared_filing_original_unavailable')
    return result


def _file_matches(path, file):
    try:
        value = path.stat()
    except FileNotFoundError:
        return False
    return ([value.st_dev, value.st_ino] == file['identity'] and value.st_size == file['size']
        and value.st_mtime_ns == file['mtime_ns'])


def _regular(path):
    value = path.lstat()
    if (not path.is_file() or path.is_symlink() or getattr(value, 'st_file_attributes', 0) & 0x400
            or value.st_nlink != 1):
        raise _conflict('shared_filing_asset_path_invalid')
    return value


def _copy_assets(data, personal, originals, cap):
    root = data.root / 'workspace'
    root.mkdir(exist_ok=True)
    _directory(root)
    prepared = {}
    for item in originals:
        files = []
        for ordinal, file in enumerate(item['files'], 1):
            name = PureWindowsPath(file['name']).name
            suffix = PureWindowsPath(file['path']).suffix.lower()
            if not re.fullmatch(r'\.[a-z0-9]{1,12}', suffix):
                suffix = '.bin'
            target = root / (item['item_id'] + ('' if ordinal == 1 else f'-{ordinal}') + suffix)
            source = Path(file['path']).resolve(strict=True)
            if not source.is_relative_to(personal.root.resolve(strict=True)) or not _file_matches(source, file):
                raise _conflict('shared_filing_source_changed')
            if target.exists() or target.is_symlink():
                if _regular(target).st_size != file['size'] or _sha256_file(target) != file['sha256']:
                    raise _conflict('shared_filing_asset_conflict')
            else:
                partial = target.with_name('.' + target.name + '.part')
                exists = partial.exists() or partial.is_symlink()
                retained = _regular(partial).st_size if exists else 0
                if retained > file['size']:
                    raise _conflict('shared_filing_asset_conflict')
                cap.consume(file['size'] - retained)
                with source.open('rb') as stream, partial.open('wb' if exists else 'xb') as output:
                    remaining = file['size']
                    while remaining:
                        chunk = stream.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise _conflict('shared_filing_source_changed')
                        output.write(chunk)
                        remaining -= len(chunk)
                    if stream.read(1):
                        raise _conflict('shared_filing_source_changed')
                    output.flush()
                    os.fsync(output.fileno())
                if (not _file_matches(source, file) or partial.stat().st_size != file['size']
                        or _sha256_file(partial) != file['sha256']):
                    raise _conflict('shared_filing_source_changed')
                partial.replace(target)
            files.append({'path': str(target), 'name': name or target.name})
        prepared[item['item_id']] = files
    return prepared


class SharedDocumentFiling:
    def __init__(self, intake):
        self.intake, self.registry, self.context = intake, intake.registry, intake.context

    def _command(self, reader, query, document, key, *, existing=None):
        command = existing if existing is not None else reader.read(SHARED_FILINGS, document)
        if command is None:
            return None
        admission = query.origin_admission
        payload = command.payload
        if (payload['command_id'] != key or payload['target_project_id'] != admission.lease.project_id
                or payload['target_namespace'] != query.source_namespace
                or payload['source_namespace'] != admission.destination.namespace
                or payload['source_root_identity'] != list(admission.destination.root_identity)
                or payload['target_root_identity'] != list(admission.lease.root_identity)
                or payload['actor_user_id'] != admission.actor.user_id
                or payload['device_id'] != admission.actor.device_id
                or payload['device_revision'] != admission.actor.revision):
            raise _conflict('shared_filing_command_conflict')
        return command

    @staticmethod
    def _view(command):
        value = command.payload
        return {name: value[name] for name in ('command_id', 'source_project_id', 'source_document_id',
            'target_project_id', 'target_namespace', 'target_document_id', 'state')}

    def target(self, application, actor, project, _key=None):
        query = self.intake._query(application, actor, project)
        admission = query.origin_admission
        with self.registry.records.begin_read() as central:
            self.registry.authorize(central, actor, project, action='write', authority=_authority(admission.lease))
            _authorize_source(central, admission)
        return {'project_id': project, 'namespace': query.source_namespace, 'scenes': self._target(query, None)}

    def command(self, application, actor, project, document, key):
        query = self.intake._query(application, actor, project)
        admission = query.origin_admission
        with self.registry.records.begin_read() as central:
            self.registry.authorize(central, actor, project, authority=_authority(admission.lease))
            _authorize_source(central, admission)
            command = self._command(admission.destination.records, query, segment(document), segment(key))
        if command is None:
            raise SharedProjectError('shared_filing_not_received', status=404)
        return self._view(command)

    async def move(self, application, actor, project, document, key, body):
        request, document, key = _request(body), segment(document), segment(key)
        query = self.intake._query(application, actor, project)
        if request['target_namespace'] != query.source_namespace:
            raise _conflict('shared_filing_target_changed')
        admission = query.origin_admission
        with self.registry.records.begin_read() as central:
            self.registry.authorize(central, actor, project, action='write', authority=_authority(admission.lease))
            _authorize_source(central, admission)
            owner = central.read(PROJECTS, project).payload['owner_user_id']
        personal = admission.destination
        old = self._command(personal.records, query, document, key)
        if old is not None and old.payload['state'] == 'filed':
            if old.payload['request'] != request:
                raise _conflict('shared_filing_command_conflict')
            return self._view(old)
        async with self.context.jobs.intake_many((owner, personal.owner_user_id)) as budgets:
            return await retain_until_finished(asyncio.to_thread(self._move, query, owner, document, key, request, budgets))

    def _prepare(self, query, document_id, key, request, source_budget):
        admission, project = query.origin_admission, query.origin_admission.lease.project_id
        personal, transfer = admission.destination, _destination_identity(query, document_id, key)
        records = personal.records
        previous = records.read('documents', document_id)
        previous_size = payload_bytes(previous.payload) if previous is not None else 0
        markdown = personal.documents.markdown(document_id, revision=request['expected_revision'])
        if not isinstance(markdown, str):
            raise _conflict('shared_filing_source_changed')
        cap = source_budget.reserve_up_to(32 * (previous_size + len(markdown.encode('utf-8'))) + 262144)
        try:
            with persistence_budget(root=personal.root, databases=(records.database_path,), consume=cap.consume), records.begin() as tx:
                old = self._command(tx, query, document_id, key)
                if old is not None and old.payload['request'] != request:
                    raise _conflict('shared_filing_command_conflict')
                if old is not None and old.payload['state'] == 'filed':
                    return old, None, None
                document, repository, markdown, _recall = _source_document(tx, personal, document_id, request)
                scope = WorkScope('local-user', request['source_project_id'])
                authority = SourceEgressService(records, reader=tx)
                if old is None:
                    try:
                        experience, selected = ensure_document_experience(repository,
                            recognition_service(TransactionRecords(tx)), scope.project_id, document_id)
                    except DocumentRecognitionError:
                        raise _conflict('shared_filing_source_changed') from None
                    if selected != request['expected_revision']:
                        raise _conflict('shared_filing_source_changed')
                    snapshot = authority.snapshot(scope, [{'type': 'experience', 'id': experience,
                        'revision': tx.read('recognition_experiences', experience).revision}])
                    if any(not node['effective_purposes'] for node in snapshot['nodes']):
                        raise SharedProjectError('shared_filing_private_source', status=403)
                else:
                    snapshot = old.payload['snapshot']
                    authority.validate_snapshot(scope, snapshot)
                    verify_lineage(tx, old.payload['document_lineage'])
                originals = _originals(tx, personal, scope.project_id, snapshot, transfer)
                manifest = [{field: item[field] for field in ('item_id', 'origin', 'files')} for item in originals]
                if old is not None:
                    if old.payload['manifest'] != manifest:
                        raise _conflict('shared_filing_source_changed')
                    return old, (document, markdown), originals
                # 源库先登记原编号与完整来源。共享内容和原处状态在后续阶段提交。
                command = tx.put(SHARED_FILINGS, document_id, {
                    'command_id': key, 'source_project_id': scope.project_id, 'source_document_id': document_id,
                    'source_namespace': personal.namespace, 'target_project_id': project,
                    'target_namespace': query.source_namespace, 'target_document_id': None, 'transfer_id': transfer,
                    'source_root_identity': list(personal.root_identity),
                    'target_root_identity': list(admission.lease.root_identity),
                    'actor_user_id': admission.actor.user_id, 'device_id': admission.actor.device_id,
                    'device_revision': admission.actor.revision, 'request': request, 'state': 'pending',
                    'snapshot': snapshot, 'document_lineage': capture_lineage(tx, 'documents', document_id),
                    'source_refs': document.payload['source_refs'], 'manifest': manifest,
                }, expected_revision=0)
                tx.commit()
                return command, (document, markdown), originals
        finally:
            cap.close()

    def _move(self, query, owner, document_id, key, request, budgets):
        admission, lease = query.origin_admission, query.origin_admission.lease
        admission.validate_execution_owner()
        personal = admission.destination
        source_budget = budgets[personal.owner_user_id]
        old = self._command(personal.records, query, document_id, key)
        if old is not None and old.payload['state'] == 'filed':
            if old.payload['request'] != request:
                raise _conflict('shared_filing_command_conflict')
            return self._view(old)
        self._target(query, request['scene'])
        command, content, originals = self._prepare(query, document_id, key, request, source_budget)
        if content is None:
            return self._view(command)
        document, markdown = content
        title = document.payload['title']
        size = sum(file['size'] for item in originals for file in item['files'])
        metadata_bytes = payload_bytes({'originals': originals, 'markdown': markdown})
        source_cap = source_budget.reserve_up_to(8 * payload_bytes(command.payload) + 65536)
        try:
            destination_cap = budgets[owner].reserve_up_to(size + 32 * metadata_bytes + 65536)
            try:
                with self.registry.roots.lease(lease.project_id):
                    data = self.registry.roots._data(lease.project_id)
                    if data.namespace != lease.data_namespace or data.root_identity != lease.root_identity:
                        raise _conflict('shared_filing_target_changed')
                    files = _copy_assets(data, personal, originals, destination_cap)
                return self._finish(query, owner, command, title, markdown, originals, files, source_cap, destination_cap)
            finally:
                destination_cap.close()
        finally:
            source_cap.close()

    def _target(self, query, scene):
        with query.records.begin_read() as tx:
            row = tx.read('v2_projects', query.origin_admission.lease.project_id)
            if row is None or scene is not None and scene not in row.payload.get('scenes', []):
                raise _conflict('shared_filing_target_changed')
            return list(row.payload.get('scenes', []))

    def _finish(self, query, owner, command, title, markdown, originals, files, source_cap, destination_cap):
        admission = query.origin_admission
        lease, personal = admission.lease, admission.destination
        basis, project = command.payload, lease.project_id
        writer = SharedWriteRecords(self.registry, admission.actor, project, authority=_authority(lease),
            action='file', object_kind='document', object_id=basis['transfer_id'], object_title=title)
        with writer.begin() as target:
            capability = shared_read_capability(TransactionRecords(target), registry=self.registry)
            data = capability._data
            if (capability.lease_payload() != lease.payload()
                    or target._central.read(PROJECTS, project).payload['owner_user_id'] != owner):
                raise _conflict('shared_filing_target_changed')
            _authorize_source(target._central, admission)
            with source_cap.using_reader(target._central), destination_cap.using_reader(target._central), personal.records.begin() as source:
                latest = self._command(source, query, command.object_id, basis['command_id'])
                if latest != command:
                    if latest is not None and latest.payload['state'] == 'filed' and latest.payload['request'] == basis['request']:
                        return self._view(latest)
                    raise _conflict('shared_filing_command_conflict')
                _document, _repository, current, recall = _source_document(source, personal, command.object_id, basis['request'])
                if current != markdown:
                    raise _conflict('shared_filing_source_changed')
                verify_lineage(source, basis['document_lineage'])
                scope = WorkScope('local-user', basis['source_project_id'])
                SourceEgressService(personal.records, reader=source).validate_snapshot(scope, basis['snapshot'])
                for item in originals:
                    for file in item['files']:
                        if not _file_matches(Path(file['path']), file):
                            raise _conflict('shared_filing_source_changed')
                with persistence_budget(root=data.root, databases=(data.records.database_path,), consume=destination_cap.consume):
                    published = self._publish(query, target, data, writer, basis, title, markdown, originals, files)
                # 共享事务已提交。此处中断时按目标收据继续，原处资料保持可用。
                with persistence_budget(root=personal.root, databases=(personal.records.database_path,), consume=source_cap.consume):
                    source.put('v2_document_recall', command.object_id, {'state': 'forgotten', 'by': 'moved',
                        'moved_to': published['document_id']}, expected_revision=recall.revision if recall else 0)
                    reject_source_candidates(source, recognition_service(TransactionRecords(source)), scope, command.object_id)
                    assignment = source.read('v2_scene_assignments_document', command.object_id)
                    record_choice(source, command.object_id, scope.project_id, project, basis['request']['scene'],
                        document_revision=basis['request']['expected_revision'],
                        assignment_revision=assignment.revision if assignment else 0,
                        title=title, summary=summary_of(markdown)[0], action_id=basis['transfer_id'], action='move')
                    project_name = target._central.read(PROJECTS, project).payload['name']
                    saved = source.put(SHARED_FILINGS, command.object_id, {**basis, 'state': 'filed',
                        'target_project_name': project_name,
                        'target_document_id': published['document_id']}, expected_revision=command.revision)
                    source_cap.validate()
                    source.commit()
                return self._view(saved)

    def _publish(self, query, tx, data, writer, basis, title, markdown, originals, files):
        transfer, project = basis['transfer_id'], basis['target_project_id']
        scene = basis['request']['scene']
        project_row = tx.read('v2_projects', project)
        if project_row is None or scene is not None and scene not in project_row.payload.get('scenes', []):
            raise _conflict('shared_filing_target_changed')
        item_ids = [item['item_id'] for item in originals]
        bound = {'project_id': project, 'command_id': basis['command_id'], 'target_namespace': query.source_namespace,
            'actor_user_id': basis['actor_user_id'], 'original_ids': item_ids}
        receipt = tx.read(SHARED_IMPORTS, transfer)
        if receipt is not None:
            if receipt.revision != 1 or any(receipt.payload.get(key) != value for key, value in bound.items()):
                raise _conflict('shared_filing_target_changed')
            document = tx.read('documents', receipt.payload['document_id'])
            if (document is None or document.payload.get('project_id') != project
                    or document.payload.get('type') != transfer or document.payload.get('status') == 'archived'
                    or data.documents.markdown(document.object_id, revision=1) != markdown):
                raise _conflict('shared_filing_target_changed')
            return receipt.payload
        refs = tuple({'source_id': identity, 'locator': 'workspace://' + identity} for identity in item_ids)
        document = data.documents.create_or_replay_generated_in_uow(DocumentDraft(
            title=title, document_type=transfer, markdown=markdown, source_refs=refs, project_id=project), tx)
        writer.object_id = document['id']
        enlisted = TransactionRecords(tx)
        items = query.origin_admission.destination.workspace.items.with_records(enlisted)
        for item in originals:
            identity = item['item_id']
            attachments = files[identity]
            payload = {'id': identity, 'project_id': project, 'input_kind': item['input_kind'],
                'title': item['title'], 'source_text': item['text'], 'status': 'confirmed', 'draft': None, 'error': None,
                'document_id': document['id'], 'created_at': item['created_at'] or self.registry.now(), 'url': item['url']}
            if attachments:
                payload.update(original_path=attachments[0]['path'], original_name=attachments[0]['name'])
            items.create_upload(payload, images=attachments if len(attachments) > 1 else None)
        if scene is not None:
            assign_scene(enlisted, 'document', document['id'], project, scene)
        receipt = {**bound, 'document_id': document['id']}
        tx.put(SHARED_IMPORTS, transfer, receipt, expected_revision=0)
        tx.commit()
        return receipt
