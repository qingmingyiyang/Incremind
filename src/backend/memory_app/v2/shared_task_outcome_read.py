from sqlite3 import Error as SQLiteError
import json

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from backend.recognition import RecognitionConflict
from backend.recognition.material_logical_scope import read_material_logical_scope
from backend.security.audited_records import AuditedRecordStore
from backend.security.user_context import USER_ACCESS
from core.ai_kernel.contracts import validate_event_transition, validate_turn_request
from core.ai_kernel.product_admission import MANIFEST_KIND
from core.ai_kernel.product_snapshot import read_product_turn_snapshot
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider.record_lineage import FACTS, HEAD_COLLECTIONS, WITNESS_COLLECTIONS, verify_lineage

from ..answer_effective import NativeAnswerSnapshot
from ..kernel.receipt_projection import frozen_public_turn_requests
from ..kernel.route_source_snapshot import native_source_connection
from ..transaction_records import TransactionRecords
from ..workbench_identity_binding import execution_owner
from ..workspace_contracts import _project
from .outcomes import completed_composition
from .task_drafts import COLLECTION as DRAFT_OPERATIONS
from .turn_logical_scope import TurnLogicalScope, logical_scope


_CHANGED = 'task_outcome_identity_changed'
_NOT_FOUND = 'task_outcome_not_found'


def install_shared_task_outcome_route(router, application):
    @router.get('/turns/{turn_id}/outcome')
    def task_outcome(turn_id: str, project_id: str, scope_kind: str):
        try:
            kind = logical_scope(scope_kind)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        if kind != 'shared':
            raise HTTPException(404, _NOT_FOUND)
        turn_id, project = _project(turn_id), _project(project_id)
        try:
            result = read_shared_task_outcome(application, turn_id=turn_id, project=project)
        except (RecognitionConflict, SQLiteError, ValueError, KeyError, TypeError):
            raise HTTPException(409, _CHANGED) from None
        if result is None:
            raise HTTPException(404, _NOT_FOUND)
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})


def read_shared_task_outcome(application, *, turn_id, project):
    state = application.state
    records = state.recognition_records
    if USER_ACCESS.get() is None or not isinstance(records, AuditedRecordStore):
        return None
    with records.begin_read() as tx:
        reader = TransactionRecords(tx)
        row = reader.read('v2_turns', turn_id)
        if row is None or row.payload['project_id'] != project:
            return None
        document = read_task_outcome(application, reader, row, kind='shared')
        return {'scope_kind': 'shared', 'turn_id': turn_id, 'document': document} if document is not None else None


def read_task_outcome(application, reader, row, *, kind):
    state = application.state
    records, documents = state.recognition_records, state.recognition_documents
    if (USER_ACCESS.get() is None or not isinstance(records, AuditedRecordStore) or row.payload['intent'] != 'do'):
        return None
    owner = execution_owner(application, records, documents)
    receipt = row.payload['receipt']['do']
    kind = logical_scope(kind)
    if receipt['state'] not in {'done', 'partial'} or receipt.get('document_id') is None:
        return None
    if receipt.get('executor', 'builtin') != 'builtin':
        context = getattr(state, 'external_context', None)
        if kind != 'personal' or context is None or context.records is not records or context.documents is not documents:
            return None
        return _read_personal_external_outcome(records, reader, row, owner, external_owner=context.owner_id)
    if TurnLogicalScope(application).read(row, reader=reader) != kind:
        return None
    turn_id, project = row.object_id, row.payload['project_id']
    entries = frozen_public_turn_requests(records, state.recognition_runtime_root, turn_id, reader=reader)
    if len(entries) != 1 or not entries[0]['qualified'] or entries[0]['public_revision'] != row.revision:
        raise RecognitionConflict(_CHANGED)
    request = entries[0]['native_request']
    execution = reader.read('v2_task_executions', turn_id)
    if (execution is None or execution.payload.get('started') is not True
            or execution.payload['owner'] is not None or execution.payload.get('executor', 'builtin') != 'builtin'):
        raise RecognitionConflict(_CHANGED)
    expected_markdown = _completed_markdown(records, reader, request, receipt)
    return _read_delivery(reader, owner, project, request['turn_id'], receipt, expected_markdown, kind=kind)


def _completed_native(records, reader, request):
    turn_id = request['turn_id']
    with native_source_connection(records, reader=reader) as connection:
        snapshot = read_product_turn_snapshot(connection, turn_id)
    if snapshot['request'] != request:
        raise RecognitionConflict(_CHANGED)
    event = None
    for raw in snapshot['events']:
        event = validate_event_transition(event, raw)
        if event['turn_id'] != turn_id or event['session_id'] != request['session_id']:
            raise RecognitionConflict(_CHANGED)
    if event is None or event['type'] != 'turn.completed' or event['data']['status'] != 'completed':
        raise RecognitionConflict(_CHANGED)
    return snapshot, event


def _completed_markdown(records, reader, request, receipt):
    snapshot, event = _completed_native(records, reader, request)
    summary = event['data']['summary']
    if not isinstance(summary, str):
        raise RecognitionConflict(_CHANGED)
    failed = [item for item in receipt['experts'] if item['state'] == 'failed']
    if receipt['state'] != ('partial' if failed else 'done'):
        raise RecognitionConflict(_CHANGED)
    composed = completed_composition(NativeAnswerSnapshot(snapshot), request, summary, events=snapshot['events'])
    if composed is not None:
        return composed['markdown']
    if failed:
        summary += '\n\n未完成：' + '；'.join(item.get('goal', item.get('role', '')) for item in failed)
    return summary


def _read_personal_external_outcome(records, reader, row, owner, *, external_owner):
    from .external_execution import RECEIPT_KIND
    from .external_executor_plugins import EXECUTOR_CLIENTS
    from .external_runner import ARCHIVE, CAPABILITY
    from .external_task_do import read_external_task_observation

    project, public_id = row.payload['project_id'], row.object_id
    receipt = row.payload['receipt']['do']
    execution = reader.read('v2_task_executions', public_id)
    thread = reader.read('v2_threads', row.payload['thread_id'])
    executor, native_id = receipt.get('executor'), receipt.get('kernel_turn_id')
    if (reader.read('v2_turns', public_id) != row or row.payload['intent'] != 'do'
            or row.payload.get('parent_turn_id') is not None
            or thread is None or thread.payload['project_id'] != project
            or receipt['state'] != 'done' or receipt.get('error') or receipt.get('interruption')
            or not receipt.get('document_id') or executor not in EXECUTOR_CLIENTS or not isinstance(native_id, str)
            or execution is None or execution.payload['project_id'] != project
            or execution.payload['executor'] != executor or execution.payload['started'] is not True
            or execution.payload['task_text'] != row.payload['user_text']
            or execution.payload['owner'] is not None):
        raise RecognitionConflict(_CHANGED)
    request = validate_turn_request(execution.payload['request'])
    if (request['turn_id'] != native_id or request['session_id'] != 'session-' + public_id
            or request['operation_id'] != 'op-' + native_id or request['idempotency_key'] != 'task-' + public_id
            or request['desired_outcome'] != 'project.task' or request['execution_policy']['template_version'] != 2
            or request['scope'] != {'kind': 'project', 'project_id': project, 'series_id': None}
            or request['capability_request'] != {'mode': 'execute_exact_v1', 'capability_id': CAPABILITY,
                'arguments': {'binding_ref': f'crp://session/{native_id}/{ARCHIVE}'}}
            or json.loads(request['input']['text']).get('task') != execution.payload['task_text']):
        raise RecognitionConflict(_CHANGED)
    snapshot, terminal = _completed_native(records, reader, request)
    native = NativeAnswerSnapshot(snapshot)
    native.admission_base_refs(request)
    if ('material-origin-v1' in snapshot['immutable']
            or any(ref['uri'].startswith('crp://material-origin/') for ref in request['input']['refs'])):
        raise RecognitionConflict(_CHANGED)
    if MANIFEST_KIND in snapshot['immutable']:
        bundle = native.restore_admission(request)
        bindings = [item.payload for item in bundle.items if item.kind == 'workbench-steer-binding-v1']
        expected = {**owner, 'project_id': project, 'thread_id': thread.object_id,
            'public_turn_id': row.payload.get('parent_turn_id') or public_id,
            'target_turn_id': public_id, 'turn_id': native_id}
        if (any(item.kind == 'material-origin-v1' for item in bundle.items) or len(bindings) != 1
                or any(bindings[0].get(key) != value for key, value in expected.items())):
            raise RecognitionConflict(_CHANGED)
    _, result, completed = read_external_task_observation(native, reader, execution.payload, public_id,
        owner_id=external_owner)
    finished = [event for event in snapshot['events'] if event['type'] == 'tool.completed'
        and event['data'].get('capability_id') == CAPABILITY]
    proof = native.get_immutable_payload(native_id, RECEIPT_KIND)
    if (not completed or proof is None or len(finished) != 1 or finished[0]['sequence'] >= terminal['sequence']
            or finished[0]['data']['payload_ref'] != result[0] or finished[0]['data']['receipt_ref'] != proof[0]
            or receipt.get('usage') != result[1]['usage']):
        raise RecognitionConflict(_CHANGED)
    message = result[1]['message']
    if message.strip():
        if receipt.get('external_text') != {'state': 'saved'}:
            raise RecognitionConflict(_CHANGED)
    else:
        from backend.recognition.external_turn_facts import source_store
        from .external_task_files import generated_attachments

        documents = SQLiteDocumentRepository(reader, namespace_id=owner['namespace_id'])
        document = documents.read(receipt['document_id'])
        files = receipt.get('external_files', {})
        if (receipt.get('external_text') != {'state': 'empty'} or document is None
                or type(files.get('count')) is not int or files['count'] < 1
                or len(generated_attachments(reader, project, document, source_store(records).root.parent)) != files['count']):
            raise RecognitionConflict(_CHANGED)
        message = '附件'
    return _read_delivery(reader, owner, project, native_id, receipt, message, kind='personal', external=True)


def _read_delivery(reader, owner, project, native_id, receipt, expected_markdown, *, kind, external=False):
    identity, namespace = receipt['document_id'], owner['namespace_id']
    documents = SQLiteDocumentRepository(reader, namespace_id=namespace)
    document = documents.read(identity)
    if document is None or document.get('id') != identity or document.get('project_id') != project:
        raise RecognitionConflict(_CHANGED)
    birth = read_material_logical_scope(reader, material_type='document', identity=identity,
        project_id=project, revision=document['revision'], namespace_id=namespace)
    operation_id = 'deliver-' + native_id
    if kind == 'shared':
        if (birth is None or birth['native_turn_id'] != native_id
                or birth['execution']['owner_user_id'] != owner['owner_user_id']
                or birth['birth']['document_type'] != 'agent-result-' + native_id):
            raise RecognitionConflict(_CHANGED)
    else:
        _personal_delivery_birth(reader, documents, document, birth, native_id, operation_id, external=external)
    operation = reader.read(DRAFT_OPERATIONS, operation_id)
    title = receipt['title']
    markdown = documents.markdown(identity, revision=1)
    if (operation is None or operation.revision != 1 or not isinstance(title, str) or not title.strip()
            or not isinstance(markdown, str) or not markdown.strip() or markdown != expected_markdown
            or operation.payload != {'inputs': {'turn_id': native_id, 'project_id': project, 'title': title, 'markdown': markdown},
                'result': {'document_id': identity, 'document_revision': 1,
                    'receipt_ref': f'crp://{namespace}/{DRAFT_OPERATIONS}/{operation_id}'}}):
        raise RecognitionConflict(_CHANGED)
    return {'id': identity, 'project_id': project, 'revision': 1, 'title': title, 'markdown': markdown}


def _personal_delivery_birth(reader, documents, document, logical_birth, native_id, operation_id, *, external):
    identity = document['id']
    revision = documents.revision(identity, 1)
    if (logical_birth is not None or document['type'] != 'agent-result-' + native_id
            or revision is None or revision['document_id'] != identity or revision['revision'] != 1
            or revision['source_snapshot']['source_refs'] != [{'source_id': native_id, 'locator': 'task://' + native_id}]):
        raise RecognitionConflict(_CHANGED)
    for collection, object_id in (('documents', identity), ('document_revisions', identity + '~r1'),
            ('document_markdown', identity + '~r1'), (DRAFT_OPERATIONS, operation_id)):
        witness = reader.read(WITNESS_COLLECTIONS[collection], object_id)
        if witness is None:
            # 旧外部交付有独立的原生结果和回执，早期 operation 未写出生旁路。
            if external and collection == DRAFT_OPERATIONS and reader.read(HEAD_COLLECTIONS[collection], object_id) is None:
                continue
            raise RecognitionConflict(_CHANGED)
        first = witness.payload['first_fact_id']
        verify_lineage(reader, {'collection': collection, 'object_id': object_id, 'fact_id': first})
        fact = reader.read(FACTS, first)
        if fact.payload['origin'] != 'created' or fact.payload['observed_revision'] != 1:
            raise RecognitionConflict(_CHANGED)
