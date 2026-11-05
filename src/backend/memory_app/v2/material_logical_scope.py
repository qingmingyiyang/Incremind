from copy import deepcopy

from backend.recognition import RecognitionConflict
from backend.recognition.external_turn_facts import source_store
from backend.recognition.material_logical_scope import (
    KINDS, PREFIX, native_material_origin, native_material_scope, read_material_logical_scope,
)
from backend.security.audited_records import _AuditedTransaction
from core.ai_kernel.product_admission import ProductAdmissionItem
from core.storage_provider.record_lineage import FACTS, capture_lineage
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordUnitOfWork
from ..transaction_records import TransactionRecords
from .material_origin_admission import read_origin_item


def bind_material_scope_in_uow(tx, records, *, material_type, identity, project_id, namespace_id,
        source_request=None, source_document_id=None, source_item_id=None, source_id=None):
    if material_type not in KINDS:
        raise RecognitionConflict('material_logical_scope_unavailable')
    row = tx.read(KINDS[material_type], identity)
    if row is None or row.payload.get('project_id') != project_id:
        raise RecognitionConflict('material_logical_scope_unavailable')
    native_id = (source_request['turn_id'] if source_request is not None else
        row.payload.get('search_turn_id') if material_type == 'workspace_item' else
        _document_turn(tx, identity))
    if not isinstance(native_id, str) or not native_id:
        raise RecognitionConflict('material_logical_scope_unavailable')
    request, authority, origin = native_material_scope(TransactionRecords(tx), native_id)
    if source_request is not None and request != source_request:
        raise RecognitionConflict('material_logical_scope_request_changed')
    if origin is None:
        if tx.read(PREFIX + material_type, identity) is not None:
            raise RecognitionConflict('material_logical_scope_changed')
        return None
    execution = origin['payload']['execution']
    if (material_type == 'workspace_item' and request['desired_outcome'] != 'web.search'
            or material_type == 'document' and source_document_id is None and source_item_id is None
            and request['desired_outcome'] not in {'project.task', 'project.answer'}):
        raise RecognitionConflict('material_logical_scope_native_kind_changed')
    read_origin_item(authority, ProductAdmissionItem(**origin),
        actor_user_id=origin['payload']['actor_user_id'], personal=execution)
    root = source_store(records).root.parent
    observed = root.stat()
    if (request['scope']['project_id'] != project_id or execution['namespace_id'] != namespace_id
            or execution['root_identity'] != [observed.st_dev, observed.st_ino]
            or getattr(records, 'user_id', execution['owner_user_id']) != execution['owner_user_id']):
        raise RecognitionConflict('material_logical_scope_owner_changed')
    current = tx.read(PREFIX + material_type, identity)
    if current is not None:
        value = read_material_logical_scope(TransactionRecords(tx), material_type=material_type,
            identity=identity, project_id=project_id, namespace_id=namespace_id)
        if value['native_turn_id'] != native_id or value['origin'] != origin:
            raise RecognitionConflict('material_logical_scope_changed')
        return value
    actual = TransactionRecords(tx)._transaction
    actual = actual.transaction if isinstance(actual, _AuditedTransaction) else actual
    # 只有原创建事务的真实 INSERT 可以登记逻辑归属，后续同库事务不得补出生。
    if (not isinstance(actual, SQLiteStructuredRecordUnitOfWork) or actual.closed or actual._read_only
            or row.revision != 1 or [entry for entry in actual._records
                if entry.collection == row.collection and entry.object_id == row.object_id] != [row]):
        raise RecognitionConflict('material_logical_scope_birth_uow_required')
    lineage = capture_lineage(tx, KINDS[material_type], identity)
    fact = tx.read(FACTS, lineage['fact_id'])
    if fact.payload['origin'] != 'created' or fact.payload['observed_revision'] != 1:
        raise RecognitionConflict('material_logical_scope_birth_uow_required')
    birth = {'collection': KINDS[material_type], 'object_id': identity, 'revision': 1, 'lineage': lineage}
    if material_type == 'document':
        version, markdown = tx.read('document_revisions', identity + '~r1'), tx.read('document_markdown', identity + '~r1')
        if version is None or markdown is None:
            raise RecognitionConflict('material_logical_scope_birth_unavailable')
        refs = version.payload.get('source_snapshot', {}).get('source_refs')
        if source_item_id is not None:
            source = read_material_logical_scope(TransactionRecords(tx), material_type='original_source',
                identity=source_id, project_id=project_id, namespace_id=namespace_id)
            if source is None or source['origin'] != origin or source['birth']['source_item_id'] != source_item_id:
                raise RecognitionConflict('material_logical_scope_birth_unavailable')
            operation = tx.read('workspace_confirmation_operations', source['birth']['confirmation_operation_id'])
            if (operation is None or operation.payload.get('document_id') != identity
                    or operation.payload.get('source_refs') != refs):
                raise RecognitionConflict('material_logical_scope_birth_unavailable')
            birth.update(source_item_id=source_item_id, source_id=source_id,
                confirmation_operation_id=operation.object_id, confirmation_revision=operation.revision,
                parent_lineage=deepcopy(source['birth']['parent_lineage']))
        elif source_document_id is None:
            if (not isinstance(refs, list) or len(refs) != 1 or refs[0].get('source_id') != native_id
                    or refs[0].get('locator') not in {'task://' + native_id, 'conversation://' + native_id}):
                raise RecognitionConflict('material_logical_scope_birth_unavailable')
        else:
            operation = tx.read('v2_outcome_restorations', refs[0]['source_id'])
            if (operation is None or operation.revision != 1
                    or operation.payload.get('document_id') != identity
                    or operation.payload.get('source_document_id') != source_document_id
                    or operation.payload.get('project_id') != project_id
                    or refs[0]['locator'] != 'outcome://' + operation.object_id):
                raise RecognitionConflict('material_logical_scope_birth_unavailable')
            birth.update(source_document_id=source_document_id, restoration_revision=operation.revision)
        birth.update(revision_record_revision=version.revision, markdown_record_revision=markdown.revision,
            source_refs=deepcopy(refs), source_locator=refs[0]['locator'], document_type=row.payload['type'])
    elif row.payload.get('origin') != 'search' or row.payload.get('search_turn_id') != native_id:
        raise RecognitionConflict('material_logical_scope_birth_unavailable')
    value = {'schema_version': 1, 'material_type': material_type, 'identity': identity,
        'project_id': project_id, 'scope_kind': 'shared', 'execution': deepcopy(execution),
        'origin': deepcopy(origin), 'birth': birth, 'native_turn_id': native_id}
    tx.put(PREFIX + material_type, identity, value, expected_revision=0)
    return value


def inherit_material_scope_in_uow(tx, records, *, identity, project_id, namespace_id, source_document_id):
    reader = TransactionRecords(tx)
    parent = read_material_logical_scope(reader, material_type='document', identity=source_document_id,
        project_id=project_id, namespace_id=namespace_id)
    if parent is None:
        return None
    request, origin = native_material_origin(reader, parent['native_turn_id'])
    if origin != parent['origin']:
        raise RecognitionConflict('material_logical_scope_changed')
    return bind_material_scope_in_uow(tx, records, material_type='document', identity=identity,
        project_id=project_id, namespace_id=namespace_id, source_request=request,
        source_document_id=source_document_id)


def bind_confirmed_materials_in_uow(tx, records, *, document_id, source_item_id, source_id, project_id, namespace_id):
    from ..original_sources import alias
    from backend.recognition import WorkScope
    reader = TransactionRecords(tx)
    parent = read_material_logical_scope(reader, material_type='workspace_item', identity=source_item_id,
        project_id=project_id, namespace_id=namespace_id)
    if parent is None:
        return None
    store = source_store(records)
    with store.locked('sources', source_id):
        body = store.read('sources', source_id)
        if body is None or alias(reader, WorkScope('local-user', project_id), source_id, body) != ('original_item', source_item_id):
            raise RecognitionConflict('material_logical_scope_confirmation_changed')
        operation = tx.read('workspace_confirmation_operations', body['confirmation_operation_id'])
        actual = reader._transaction
        actual = actual.transaction if isinstance(actual, _AuditedTransaction) else actual
        # JSON 已由原确认流程写入；这里只绑定其真实 incarnation 与本次 SQL 提交。
        if (not isinstance(actual, SQLiteStructuredRecordUnitOfWork) or actual.closed or actual._read_only
                or operation.payload.get('document_id') != document_id
                or operation not in actual._records or store.revision('sources', source_id) != 1
                or not store.incarnation('sources', source_id)):
            raise RecognitionConflict('material_logical_scope_birth_uow_required')
        value = {**deepcopy(parent), 'material_type': 'original_source', 'identity': source_id,
            'birth': {'collection': 'sources', 'object_id': source_id, 'revision': 1,
                'incarnation': store.incarnation('sources', source_id), 'source_item_id': source_item_id,
                'parent_lineage': deepcopy(parent['birth']['lineage']),
                'confirmation_operation_id': operation.object_id}}
        tx.put(PREFIX + 'original_source', source_id, value, expected_revision=0)
        request, origin = native_material_origin(reader, parent['native_turn_id'])
        if origin != parent['origin']:
            raise RecognitionConflict('material_logical_scope_changed')
        return bind_material_scope_in_uow(tx, records, material_type='document', identity=document_id,
            project_id=project_id, namespace_id=namespace_id, source_request=request,
            source_item_id=source_item_id, source_id=source_id)


def _document_turn(reader, identity):
    row = reader.read('document_revisions', identity + '~r1')
    refs = row.payload.get('source_snapshot', {}).get('source_refs') if row is not None else None
    if (not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], dict)
            or not isinstance(refs[0].get('source_id'), str)
            or refs[0].get('locator') not in {'task://' + refs[0]['source_id'], 'conversation://' + refs[0]['source_id']}):
        raise RecognitionConflict('material_logical_scope_birth_unavailable')
    return refs[0]['source_id']
