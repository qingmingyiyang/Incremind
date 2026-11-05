import base64
from copy import deepcopy
import os
from pathlib import PurePosixPath
import stat
from urllib.parse import urlencode

from backend.shared.secret_detection import redact_secrets
from core.external_extension_runtime.windows_handle_io import WindowsHandleIoError, WindowsHandleTreeIo

from .external_output_contract import frozen_output_policy
from .external_progress import bound_product
from .external_runner import ARCHIVE
from .external_runs import ExternalRuns
from ..workspace_contracts import _now


FILES_KIND = 'external-task-files-v1'
_RESULT = 'external-task-result-v1'
_PROOF = 'external-task-receipt-v1'


class ExternalTaskFilesError(ValueError):
    pass


def _identity(value):
    return [value.st_dev, value.st_ino, value.st_mode]


def _file_state(value):
    return (_identity(value), value.st_size, value.st_nlink, value.st_mtime_ns, value.st_ctime_ns)


def _posix_tree(root, expected_identity, spec):
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if _identity(os.fstat(root_fd)) != expected_identity:
            raise ExternalTaskFilesError('external_output_root_changed')
        output = os.open(spec['directory'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            state = {'entries': 0, 'files': 0, 'bytes': 0}
            def read(directory, prefix):
                result = {}
                names = []
                with os.scandir(directory) as entries:
                    for entry in entries:
                        state['entries'] += 1
                        if state['entries'] > spec['max_files'] * 257 + 1:
                            raise ExternalTaskFilesError('external_output_limit')
                        names.append(entry.name)
                for name in sorted(names):
                    relative = prefix + name
                    if (len(relative.encode('utf-8')) > 512
                            or relative.count('/') >= 256):
                        raise ExternalTaskFilesError('external_output_limit')
                    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    try:
                        before = os.fstat(child)
                        if stat.S_ISDIR(before.st_mode):
                            result.update(read(child, relative + '/'))
                            continue
                        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                            raise ExternalTaskFilesError('external_output_file_invalid')
                        state['files'] += 1
                        state['bytes'] += before.st_size
                        if (state['files'] > spec['max_files'] or before.st_size > spec['max_file_bytes']
                                or state['bytes'] > spec['max_total_bytes']):
                            raise ExternalTaskFilesError('external_output_limit')
                        with os.fdopen(child, 'rb', closefd=False) as stream:
                            data = stream.read(before.st_size + 1)
                        if len(data) != before.st_size or _file_state(before) != _file_state(os.fstat(child)):
                            raise ExternalTaskFilesError('external_output_file_changed')
                        result[relative] = data
                    finally:
                        os.close(child)
                return result
            return read(output, '')
        finally:
            os.close(output)
    finally:
        os.close(root_fd)


def freeze_output_tree(root, expected_identity, spec):
    if os.name == 'nt':
        return WindowsHandleTreeIo().read_bounded_tree(root, (spec['directory'],),
            max_files=spec['max_files'], max_file_bytes=spec['max_file_bytes'],
            max_total_bytes=spec['max_total_bytes'], expected_root_identity=expected_identity,
            require_single_link=True)
    return _posix_tree(root, expected_identity, spec)


def clean_output_files(files, spec, redact):
    accepted, rejected = [], []
    for relative, data in sorted(files.items()):
        path = PurePosixPath(relative)
        reason = None
        if (not path.parts or path.is_absolute() or path.as_posix() != relative or any(part in {'.', '..'} for part in path.parts)
                or any(any(ord(character) < 32 or character in '<>:"\\|?*' for character in part) for part in path.parts)
                or redact(relative) != relative):
            reason = 'external_output_name_unsafe'
        elif path.suffix.lower() not in spec['text_suffixes']:
            reason = 'external_output_type_not_supported'
        else:
            try:
                text = data.decode('utf-8-sig')
            except UnicodeDecodeError:
                reason = 'external_output_encoding_invalid'
            else:
                if '\x00' in text:
                    reason = 'external_output_encoding_invalid'
                else:
                    safe = redact(text).encode('utf-8')
                    if not safe or len(safe) > spec['max_file_bytes']:
                        reason = 'external_output_size_invalid'
                    else:
                        accepted.append({'name': relative, 'content': base64.b64encode(safe).decode('ascii')})
        if reason is not None:
            rejected.append(reason)
    return accepted, rejected


def snapshot_files(value, spec):
    if (spec is None or value.get('state') not in {'captured', 'partial', 'blocked', 'empty'}
            or set(value) != {'version', 'binding', 'state', 'files', 'rejected', 'captured_at'}
            or value['version'] != FILES_KIND
            or not isinstance(value['files'], list) or len(value['files']) > spec['max_files']
            or not isinstance(value['rejected'], list) or any(not isinstance(code, str)
                or not code.startswith('external_output_') for code in value['rejected'])
            or not isinstance(value['captured_at'], str)):
        raise ValueError('external_output_snapshot_invalid')
    files = {}
    for file in value['files']:
        if not isinstance(file, dict) or set(file) != {'name', 'content'} or file['name'] in files:
            raise ValueError('external_output_snapshot_invalid')
        files[file['name']] = base64.b64decode(file['content'], validate=True)
    cleaned, rejected = clean_output_files(files, spec, redact_secrets)
    if (cleaned != value['files'] or rejected or sum(map(len, files.values())) > spec['max_total_bytes']
            or bool(files) != (value['state'] in {'captured', 'partial'})
            or bool(value['rejected']) != (value['state'] in {'partial', 'blocked'})):
        raise ValueError('external_output_snapshot_invalid')
    return files


def capture_task_files(lease, *, records, turns, turn_id, owner_id, external_context, result_ref, receipt_ref):
    request = turns.get_request(turn_id)
    saved = turns.get_immutable_payload(turn_id, ARCHIVE)
    result, proof = turns.get_immutable_payload(turn_id, _RESULT), turns.get_immutable_payload(turn_id, _PROOF)
    if (saved is None or result is None or proof is None or result[0] != result_ref or proof[0] != receipt_ref
            or saved[1]['request'] != request or saved[1]['owner_id'] != owner_id
            or proof[1].get('payload_ref') != result_ref or result[1].get('owner_id') != owner_id
            or result[1].get('turn_id') != turn_id or proof[1].get('turn_id') != turn_id
            or proof[1].get('owner_id') != owner_id):
        raise ValueError('external_output_binding_invalid')
    spec = frozen_output_policy(request)
    binding = {'turn_id': turn_id, 'owner_id': owner_id, 'archive_ref': saved[0],
        'result_ref': result_ref, 'receipt_ref': receipt_ref}
    value = {'version': FILES_KIND, 'binding': binding, 'state': 'not_agreed', 'files': [], 'rejected': [], 'captured_at': _now()}
    if spec is None:
        return turns.get_or_create_immutable_payload(turn_id, FILES_KIND, value)
    if (lease.plan.executor not in {'claude-code', 'codex'} or lease.plan.preset != 'workspace'
            or saved[1]['launch']['materials']['directory_identity'] is None):
        raise ValueError('external_output_binding_invalid')
    if result[1]['status'] != 'completed':
        value['state'] = 'not_completed'
        return turns.get_or_create_immutable_payload(turn_id, FILES_KIND, value)
    owner = lease._process_owner
    run = ExternalRuns(records).read(turn_id, owner_id=owner_id)
    if owner is None or not owner.closed or result[1]['exit_code'] != 0 or proof[1].get('effect_certainty') != 'confirmed_applied':
        raise ValueError('external_output_owner_not_closed')
    if (run['status'] != 'completed' or run['exit_code'] != 0 or run['started_at'] is None or run['ended_at'] is None
            or proof[1] != {'turn_id': turn_id, 'owner_id': owner_id, 'payload_ref': result_ref,
                'run_revision': run['revision'], 'status': 'completed', 'effect_certainty': 'confirmed_applied'}
            or result[1]['usage'] != run['usage']):
        raise ValueError('external_output_proof_invalid')
    product_id = request['session_id'].removeprefix('session-')
    execution = records.read('v2_task_executions', product_id)
    product_owner = execution.payload.get('owner') if execution else None
    def validate():
        lease._validate_native_current()
        if lease._process_owner is not owner or not owner.closed or turns.get_request(turn_id) != request:
            raise ValueError('external_output_owner_changed')
        bound_product(records, product_id=product_id, product_owner=product_owner,
            request=request, executor=lease.plan.executor, turn_id=turn_id)
        if (external_context is None or external_context.owner_id != owner_id or external_context.records is not records
                or external_context.qualified_delivery(saved[1]['delivery']['turn_id']) != saved[1]['delivery']):
            raise ValueError('external_output_binding_invalid')
    validate()
    binding.update(product_id=product_id, product_owner=product_owner)
    try:
        files = freeze_output_tree(lease.plan.cwd, saved[1]['launch']['materials']['directory_identity'], spec)
    except FileNotFoundError as error:
        missing_output = error.filename == spec['directory'] or error.args == (spec['directory'],)
        value.update(state='empty' if missing_output else 'blocked',
            rejected=[] if missing_output else ['external_output_tree_unavailable'])
    except (OSError, WindowsHandleIoError, ExternalTaskFilesError, UnicodeError):
        value.update(state='blocked', rejected=['external_output_tree_unavailable'])
    else:
        value['files'], value['rejected'] = clean_output_files(files, spec, lease.redact)
        value['state'] = ('partial' if value['rejected'] else 'captured') if value['files'] else (
            'blocked' if value['rejected'] else 'empty')
    validate()
    return turns.get_or_create_immutable_payload(turn_id, FILES_KIND, value)


def qualified_task_files(records, turns, request, product_id, executor, *, owner):
    saved = turns.get_immutable_payload(request['turn_id'], FILES_KIND)
    if saved is None:
        return {'state': 'unavailable' if frozen_output_policy(request) else 'not_agreed', 'files': [], 'rejected': []}
    ref, value = saved
    archive = turns.get_immutable_payload(request['turn_id'], ARCHIVE)
    result, proof = turns.get_immutable_payload(request['turn_id'], _RESULT), turns.get_immutable_payload(request['turn_id'], _PROOF)
    expected = {'turn_id': request['turn_id'], 'owner_id': archive[1]['owner_id'],
        'archive_ref': archive[0], 'result_ref': result[0], 'receipt_ref': proof[0]} if archive and result and proof else None
    if (value.get('version') == FILES_KIND and value.get('binding') == expected
            and value.get('state') == 'not_agreed' and frozen_output_policy(request) is None
            and value.get('files') == [] and value.get('rejected') == []):
        return deepcopy(value) | {'immutable_ref': ref}
    if (value.get('version') != FILES_KIND or archive is None or result is None or proof is None
            or value.get('binding') != {'turn_id': request['turn_id'], 'owner_id': archive[1]['owner_id'],
                'archive_ref': archive[0], 'result_ref': result[0], 'receipt_ref': proof[0],
                'product_id': product_id, 'product_owner': owner}
            or archive[1]['request'] != request or result[1]['status'] != 'completed'
            or proof[1].get('status') != 'completed' or proof[1].get('effect_certainty') != 'confirmed_applied'
            or proof[1].get('payload_ref') != result[0] or result[1].get('exit_code') != 0):
        raise ValueError('external_output_binding_invalid')
    run = ExternalRuns(records).read(request['turn_id'], owner_id=archive[1]['owner_id'])
    if (run['status'] != 'completed' or run['started_at'] is None or run['ended_at'] is None
            or run['exit_code'] != 0 or result[1].get('usage') != run['usage']
            or result[1].get('owner_id') != archive[1]['owner_id'] or result[1].get('turn_id') != request['turn_id']
            or proof[1] != {'turn_id': request['turn_id'], 'owner_id': archive[1]['owner_id'],
                'payload_ref': result[0], 'run_revision': run['revision'], 'status': 'completed',
                'effect_certainty': 'confirmed_applied'}):
        raise ValueError('external_output_proof_invalid')
    snapshot_files(value, frozen_output_policy(request))
    bound_product(records, product_id=product_id, product_owner=owner, request=request, executor=executor, turn_id=request['turn_id'])
    return deepcopy(value) | {'immutable_ref': ref}


class QualifiedTaskFiles:
    def __init__(self, records, turns, request, product_id, executor, owner):
        self.records, self.turns, self.request = records, turns, deepcopy(request)
        self.product_id, self.executor, self.owner = product_id, executor, owner
        self.value = qualified_task_files(records, turns, self.request, product_id, executor, owner=owner)

    def validate(self, reader):
        value = qualified_task_files(reader, self.turns, self.request, self.product_id, self.executor, owner=self.owner)
        if value != self.value:
            raise ValueError('external_output_binding_invalid')
        return deepcopy(value)


def generated_attachments(records, project, document, runtime_root):
    row = records.read('v2_external_task_attachments', document['id'])
    if row is None:
        return []
    value = row.payload
    operation = records.read('v2_task_draft_operations', value.get('operation'))
    if (row.revision != 1 or set(value) != {'project_id', 'document_id', 'birth_revision', 'turn_id', 'product_id',
                'owner_id', 'product_owner', 'operation', 'immutable_ref', 'files', 'source_births', 'parent_experience'}
            or value.get('project_id') != project or value.get('document_id') != document['id']
            or operation is None or operation.payload.get('result', {}).get('document_id') != document['id']
            or operation.payload['result'].get('document_revision') != value.get('birth_revision')
            or operation.payload.get('inputs', {}).get('project_id') != project
            or operation.payload['inputs'].get('turn_id') != value.get('turn_id')):
        raise ValueError('external_attachment_binding_invalid')
    from backend.recognition import WorkScope
    from backend.recognition.product_draft_dependencies import product_draft_source, read_product_draft_dependencies
    from backend.recognition.kernel_read_facts import authority_facts
    from backend.recognition.external_turn_facts import source_store
    from core.storage_provider.record_lineage import verify_lineage
    scope = WorkScope('local-user', project)
    bound = product_draft_source(records, scope, document['id'], value['birth_revision'])
    if (bound is None or bound.public_turn_id != value['product_id'] or value['owner_id'] != scope.user_id
            or not isinstance(value['files'], list) or not value['files']
            or len(value['files']) != len(value['source_births'])):
        raise ValueError('external_attachment_binding_invalid')
    request, identity = bound.request, value['turn_id']
    turns, _ = authority_facts(source_store(records).root / 'ai-turns.sqlite3')
    def immutable(kind):
        ref = turns.immutable_payload_reference(identity, kind)
        return ref, turns.get(ref)
    archive_ref, archive = immutable(ARCHIVE)
    result_ref, native = immutable(_RESULT)
    proof_ref, proof = immutable(_PROOF)
    files_ref, frozen = immutable(FILES_KIND)
    run = ExternalRuns(records).read(identity, owner_id=scope.user_id)
    if (turns.get_request(identity) != request or turns.events_after(identity)[-1]['type'] != 'turn.completed'
            or archive['request'] != request or archive['owner_id'] != scope.user_id
            or archive['launch']['executor'] not in {'claude-code', 'codex'}
            or files_ref != value['immutable_ref'] or frozen['binding'] != {'turn_id': identity,
                'owner_id': scope.user_id, 'archive_ref': archive_ref, 'result_ref': result_ref,
                'receipt_ref': proof_ref, 'product_id': value['product_id'], 'product_owner': value['product_owner']}
            or run['status'] != 'completed' or run['exit_code'] != 0 or run['started_at'] is None or run['ended_at'] is None
            or native['turn_id'] != identity or native['owner_id'] != scope.user_id or native['status'] != 'completed'
            or native['exit_code'] != 0 or native['usage'] != run['usage']
            or proof != {'turn_id': identity, 'owner_id': scope.user_id, 'payload_ref': result_ref,
                'run_revision': run['revision'], 'status': 'completed', 'effect_certainty': 'confirmed_applied'}):
        raise ValueError('external_attachment_binding_invalid')
    frozen_files = snapshot_files(frozen, frozen_output_policy(request))
    parent = records.read('recognition_experiences', value['parent_experience']['id'])
    verify_lineage(records, value['parent_experience']['lineage'])
    parent_bound = read_product_draft_dependencies(records, scope, parent.payload) if parent else None
    if (parent_bound is None or parent_bound.request != request or parent_bound.public_turn_id != bound.public_turn_id
            or parent.payload.get('provenance', {}).get('source_refs') != [{'type': 'turn', 'id': identity},
                {'type': 'document', 'id': document['id'], 'revision': value['birth_revision']}]
            or parent.revision < value['parent_experience']['revision']
            or value['parent_experience']['lineage']['collection'] != 'recognition_experiences'
            or value['parent_experience']['lineage']['object_id'] != parent.object_id
            or len(frozen_files) != len(value['files'])):
        raise ValueError('external_attachment_parent_invalid')
    from ..uploaded_media import original_path
    result = []
    for ordinal, (file, birth, name) in enumerate(zip(value['files'], value['source_births'], frozen_files), 1):
        source_id = 'workspace-agent-' + identity.removeprefix('turn-') + '-' + str(ordinal)
        original = records.read('workspace_items', file['source_id'])
        payload = birth['payload']
        inherited = {'source_refs': [{'type': 'experience', 'id': parent.object_id, 'revision': value['parent_experience']['revision']}]}
        fact = records.read('record_lineage_facts', birth['lineage']['fact_id'])
        if (file != {'source_id': source_id, 'name': name, 'bytes': len(frozen_files[name])}
                or birth['source_id'] != source_id or birth['revision'] != 1
                or birth['lineage']['collection'] != 'workspace_items' or birth['lineage']['object_id'] != source_id
                or payload.get('id') != source_id or payload.get('project_id') != project
                or payload.get('document_id') != document['id'] or payload.get('input_kind') != 'file'
                or payload.get('source_text') != frozen_files[name].decode('utf-8') or payload.get('created_at') != frozen['captured_at']
                or payload.get('provenance') != inherited or original is None or original.revision < 1
                or any(original.payload.get(key) != payload.get(key) for key in ('id', 'project_id', 'input_kind', 'created_at', 'provenance'))
                or fact is None or fact.revision != 1 or fact.payload != {'schema_version': 1, 'collection': 'workspace_items',
                    'object_id': source_id, 'origin': 'created', 'observed_revision': 1}):
            raise ValueError('external_attachment_binding_invalid')
        verify_lineage(records, birth['lineage'])
        try:
            path = original_path(runtime_root, original.payload)
        except (OSError, ValueError):
            result.append({'source_id': file['source_id'], 'name': file['name'], 'bytes': file['bytes'],
                'available': False, 'download_url': None})
        else:
            available = path.stat().st_size == file['bytes']
            result.append({'source_id': file['source_id'], 'name': file['name'], 'bytes': file['bytes'],
                'available': available, 'download_url': '/api/workspace/v1/items/' + file['source_id']
                    + '/original?' + urlencode({'project_id': project}) if available else None})
    return result
