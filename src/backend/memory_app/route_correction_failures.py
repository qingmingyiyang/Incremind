from copy import deepcopy

from backend.recognition import RecognitionConflict
from .route_correction_scopes import planned_child_scope_binding, source_scope, _owner
from .kernel.route_source_snapshot import native_source_snapshot

CLOSURES = 'v2_route_correction_child_closures'


def missing_part(records, parent, index, *, reader=None):
    actual = reader if reader is not None else records
    namespace = _owner(records, reader)
    child_id = parent.payload['part_turn_ids'][index]
    proof = planned_child_scope_binding(actual, parent.object_id, child_id)
    if proof is None:
        raise RecognitionConflict('route_correction_missing_child_requires_review')
    operation = actual.read('v2_route_correction_operations', proof['operation_id'])
    value = operation.payload
    part = value['plan']['parts'][index]
    observed = parent.payload['receipt']['parts'][index]
    if (value['status'] != 'partial' or observed.get('state') not in {'failed', 'not_started'}
            or actual.read('v2_turns', child_id) is not None
            or actual.read('v2_task_executions', child_id) is not None
            or any(row.payload.get('child_id') == child_id for row in actual.list('v2_route_correction_originals'))):
        raise RecognitionConflict('route_correction_missing_child_requires_review')
    if part['intent'] in {'ask', 'do'} and native_source_snapshot(records, child_id, reader=reader)['request'] is not None:
        raise RecognitionConflict('route_correction_missing_child_requires_review')
    existing_file = bool(part['item_ids'] and all(value['originals'][source]['intent'] == 'remember' for source in part['sources']))
    if existing_file:
        raise RecognitionConflict('route_correction_missing_file_requires_review')
    request_key = 'route-correction-' + child_id
    request = actual.read('v2_turn_requests', request_key)
    body = {'project_id': proof['target']['project_id'], 'thread_id': proof['target']['thread_id'],
        'text': part['span'], 'intent': part['intent']}
    if request is not None and (request.payload.get('state') != 'failed'
            or request.payload.get('instance') != value['instance']
            or request.payload.get('idempotency_key') != operation.object_id + ':' + str(index)
            or request.payload.get('body') != body):
        raise RecognitionConflict('route_correction_missing_child_requires_review')
    if request is not None and part['intent'] == 'do':
        raise RecognitionConflict('route_correction_missing_do_requires_review')
    members, attachments, originals = deepcopy(part['source_members']), [], []
    for member in members:
        row = actual.read('v2_turns', member['turn_id'])
        if row is None or row.revision != member['revision']:
            raise RecognitionConflict('route_correction_source_changed')
        if source_scope(records, row, namespace=namespace, reader=reader) != member['scope']:
            raise RecognitionConflict('route_correction_source_scope_changed')
        identity = row.payload.get('item_id')
        if identity:
            originals.append(identity)
            item = actual.read('workspace_items', identity)
            if item and item.payload['input_kind'] in {'file', 'image', 'audio', 'video'}:
                attachments.append(identity)
        member.pop('lineage', None)
        member.pop('material_lineages', None)
        member.pop('scope', None)
    return {'index': index, 'intent': part['intent'], 'span': part['span'],
        'depends_on': list(part['depends_on']), 'instruction': part['instruction'],
        'turn_id': None, 'planned_turn_id': child_id, 'revision': None, 'parent_turn_id': None,
        'item_ids': attachments, 'original_item_ids': originals, 'source_members': members,
        'missing_proof': {'schema_version': 1, 'kind': 'no_public_execution-v1', 'disposition': 'confirmed_none',
            'operation_id': operation.object_id, 'parent_id': parent.object_id, 'part_index': index,
            'child_id': child_id, 'request_key': request_key, 'request_revision': request.revision if request else 0},
        'frozen_scope': deepcopy(proof['target'])}


def close_missing_parts(records, tx, originals):
    for part in originals:
        if part['turn_id'] is not None:
            continue
        from .transaction_records import TransactionRecords
        parent = tx.read('v2_turns', part['missing_proof']['parent_id'])
        fresh = missing_part(records, parent, part['index'], reader=TransactionRecords(tx))
        if fresh['missing_proof'] != part['missing_proof']:
            raise RecognitionConflict('route_correction_missing_child_changed')
        proof = fresh['missing_proof']
        current = tx.read(CLOSURES, proof['child_id'])
        if current is None:
            tx.put(CLOSURES, proof['child_id'], proof, expected_revision=0)
        elif current.revision != 1 or current.payload != proof:
            raise RecognitionConflict('route_correction_missing_child_changed')
