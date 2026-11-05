import asyncio
from copy import deepcopy
from threading import RLock
from uuid import NAMESPACE_URL, uuid5

from core.document_engine import SQLiteDocumentRepository
from core.storage_provider.record_lineage import verify_lineage
from ..processing_lease import ProcessingLease
from ..server_jobs import retain_until_finished
from ..source_egress import recognition_service
from ..transaction_records import TransactionRecords
from ..workspace_generation import _FIELDS, _draft
from ..workspace_items import WorkspaceItems
from ..workspace_review import WorkspaceReview
from .shared_extraction_admission import payload_bytes
from .shared_organize import SharedIntakeRecords
from .shared_projects import PROJECTS, SharedProjectError, revision, segment


def validate_organized_source(reader, query, identity):
    row = reader.read('workspace_items', identity)
    value = reader.read('v2_shared_organize_results', identity)
    if row is None or row.payload.get('project_id') != query.origin_admission.lease.project_id:
        raise SharedProjectError('shared_intake_source_unavailable', status=404)
    if value is None:
        raise SharedProjectError('shared_organize_result_unavailable', status=409)
    payload = value.payload
    fields = {'item_id', 'project_id', 'source_namespace', 'source_birth', 'source_revision',
        'source_text', 'draft', 'execution_owner_user_id', 'producer'}
    producer = payload.get('producer')
    if (set(payload) != fields or payload['item_id'] != identity or payload['project_id'] != row.payload['project_id']
            or payload['source_namespace'] != query.source_namespace or payload['source_text'] != row.payload.get('source_text')
            or type(payload['source_revision']) is not int or not 0 < payload['source_revision'] < row.revision
            or not isinstance(producer, dict) or set(producer) != {'turn_id', 'desired_outcome', 'namespace_id', 'stage_key', 'stage_revision', 'result_ref'}
            or producer['desired_outcome'] != 'memory.organize' or type(producer['stage_revision']) is not int
            or producer['stage_revision'] < 2 or not isinstance(payload['source_birth'], dict)
            or payload['source_birth'].get('collection') != 'workspace_items'
            or payload['source_birth'].get('object_id') != identity):
        raise SharedProjectError('shared_organize_result_changed', status=409)
    for token in (payload['execution_owner_user_id'], producer['namespace_id'], producer['turn_id']):
        segment(token)
    if producer['result_ref'] != 'crp://session/' + producer['turn_id'] + '/organize-model-result-v1':
        raise SharedProjectError('shared_organize_result_changed', status=409)
    execution = '/'.join(('personal', payload['execution_owner_user_id'], producer['namespace_id']))
    storage = identity + '-shared-' + uuid5(NAMESPACE_URL,
        '/'.join((query.source_namespace, execution, identity))).hex
    if producer['stage_key'] != 'organize-' + storage + '-1' or _draft(payload['draft'], payload['source_text']) != payload['draft']:
        raise SharedProjectError('shared_organize_result_changed', status=409)
    verify_lineage(reader, payload['source_birth'])
    return row


async def review_shared_intake(intake, application, actor, project, identity, body, *, operation, query=None,
                               command_source=None):
    if operation not in {'draft', 'confirm', 'recognition'}:
        raise SharedProjectError('shared_intake_operation_invalid')
    fields = {'expected_revision', *_FIELDS} if operation == 'draft' else {'expected_revision'}
    if not isinstance(body, dict) or set(body) != fields:
        raise SharedProjectError('shared_intake_fields_invalid')
    revision(body['expected_revision'])
    query = intake._query(application, actor, project, query=query)
    identity = segment(identity)
    with intake.registry.records.begin_read() as central:
        intake.registry.authorize(central, actor, project, action='write')
        owner_user_id = central.read(PROJECTS, project).payload['owner_user_id']
    current = query.records.read('workspace_items', identity)
    if current is None or current.payload.get('project_id') != project:
        raise SharedProjectError('shared_intake_source_unavailable', status=404)
    size = payload_bytes({'input': body, 'source': current.payload})
    async with intake.context.jobs.intake_many((owner_user_id,)) as budgets:
        cap = budgets[owner_user_id].reserve_up_to(32 * size + 65536)
        try:
            return await retain_until_finished(asyncio.to_thread(_review, query,
                owner_user_id, cap, identity, deepcopy(body), operation, command_source))
        finally:
            cap.close()


def _review(query, owner_user_id, cap, identity, body, operation, command_source):
    project = query.origin_admission.lease.project_id
    current = query.records.read('workspace_items', identity)
    if current is None:
        raise SharedProjectError('shared_intake_source_unavailable', status=404)
    action = 'organize' if command_source is not None and operation == 'confirm' else operation
    writer = SharedIntakeRecords(query, owner_user_id, cap, action=action,
        identity=identity, title=current.payload['title'], command_source=command_source)
    with writer.begin() as transaction:
        reader = TransactionRecords(transaction)
        row = validate_organized_source(reader, query, identity)
        acceptable = {row.revision}
        if operation == 'confirm':
            acceptable.add(row.payload.get('reviewed_revision'))
        if body['expected_revision'] not in acceptable:
            raise SharedProjectError('item_revision_conflicted', status=409)
        items = WorkspaceItems(reader, ProcessingLease(reader, 'workspace_items', 'shared-review'), RLock())
        documents = SQLiteDocumentRepository(reader, namespace_id=query.origin_admission.lease.data_namespace)
        service = recognition_service(reader, source_owner=writer)
        review = WorkspaceReview(items, documents, service, None, None, query.material_owner.root / 'workspace')
        values = {'project_id': project, **body}
        if operation == 'draft':
            result = asyncio.run(review.save_draft(identity, values))
        elif operation == 'confirm':
            result = asyncio.run(review.confirm(identity, values))
        else:
            # 认识提炼与原件指针在同一个来源 UOW，保留原服务的人审规则。
            result = asyncio.run(review.recognition(identity, values))
        value = {'project_id': project, 'scope_kind': 'shared', 'namespace': query.source_namespace,
            'item': items.public_row(reader.read('workspace_items', identity))}
        if operation == 'recognition':
            value['recognition'] = result
        transaction.commit()
    return value
