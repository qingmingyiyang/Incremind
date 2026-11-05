import json
from copy import deepcopy
from .contracts import validate_governed_payload
from .product_admission import (
    ADMISSION_KINDS, MANIFEST_KIND, ProductAdmissionBundle, ProductAdmissionItem,
    admission_identity, validate_manifest, validate_bundle,
)


def admission_reference_tail(request, manifest):
    logical = {'kind':'session_event','object_id':request['turn_id'],
        'uri':f"crp://session/{request['turn_id']}/{MANIFEST_KIND}"}
    tail = [*[item['ref'] for item in manifest['items']],logical]
    if request['input']['refs'][-len(tail):] != tail:
        raise ValueError('product_admission_reference_order_changed')
    return tail


def restore_admission_snapshot(request, immutable):
    manifest = immutable.get(MANIFEST_KIND)
    if manifest is None:
        raise ValueError('product_admission_manifest_unavailable')
    validate_manifest(request,manifest[1],persisted=True)
    admission_reference_tail(request,manifest[1])
    items = []
    for declaration in manifest[1]['items']:
        actual = immutable.get(declaration['kind'])
        if actual is None or actual[0] != declaration['payload_ref']:
            raise ValueError('product_admission_snapshot_item_changed')
        validate_governed_payload(actual[1])
        items.append(ProductAdmissionItem(declaration['kind'],deepcopy(declaration['ref']),
            deepcopy(declaration['scope']),deepcopy(actual[1])))
    planned = {**deepcopy(manifest[1]),'items':[{key:deepcopy(value) for key,value in item.items() if key != 'payload_ref'}
        for item in manifest[1]['items']]}
    bundle = ProductAdmissionBundle(planned,tuple(items))
    validate_bundle(request,bundle)
    return bundle


def admission_base_refs(request, immutable):
    manifest = immutable.get(MANIFEST_KIND)
    logical = {'kind':'session_event','object_id':request['turn_id'],
        'uri':f"crp://session/{request['turn_id']}/{MANIFEST_KIND}"}
    if manifest is None:
        if logical in request['input']['refs'] or any(kind in immutable for kind in ADMISSION_KINDS):
            raise ValueError('product_admission_manifest_unavailable')
        return deepcopy(request['input']['refs'])
    bundle = restore_admission_snapshot(request,immutable)
    return deepcopy(request['input']['refs'][:-(len(bundle.items)+1)])


def read_admission_base_refs(connection, expected):
    if not connection.in_transaction:
        raise ValueError('product_snapshot_transaction_required')
    row = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?',(expected['turn_id'],)).fetchone()
    if row is None:
        raise ValueError('product_snapshot_request_unavailable')
    actual = json.loads(row[0])
    if (admission_identity(actual) != admission_identity(expected)
            or any(actual.get(key) != expected.get(key) for key in ('desired_outcome','policy_versions','created_at','execution_policy'))):
        raise ValueError('product_snapshot_request_changed')
    immutable = {}
    for kind in (MANIFEST_KIND,*sorted(ADMISSION_KINDS)):
        row = connection.execute('SELECT payload_ref,payload_json FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?',
            (expected['turn_id'],kind)).fetchone()
        if row is not None:
            immutable[kind] = (row[0],json.loads(row[1]))
    return admission_base_refs(actual,immutable)


def read_model_attempt_bindings(connection, turn_id):
    if not connection.in_transaction:
        raise ValueError('product_snapshot_transaction_required')
    fields = ('model_request_id','attempt_id','attempt_number','status','dispatch_ref','terminal_ref',
        'lease_owner_id','lease_generation','dispatch','terminal','effect_state')
    result = []
    for row in connection.execute('SELECT r.model_request_id,r.attempt_id,r.attempt_number,r.status,r.dispatch_payload_ref,'
            'r.terminal_receipt_ref,r.lease_owner_id,r.lease_generation,d.payload_json,t.payload_json,e.state '
            'FROM ai_model_attempt_reservations r LEFT JOIN ai_turn_payloads d ON d.payload_ref=r.dispatch_payload_ref '
            'LEFT JOIN ai_turn_payloads t ON t.payload_ref=r.terminal_receipt_ref '
            'LEFT JOIN effect e ON e.operation_id=r.attempt_id WHERE r.turn_id=? ORDER BY r.model_request_id,r.attempt_number',
            (turn_id,)):
        value = dict(zip(fields,row))
        value['dispatch'] = json.loads(value['dispatch']) if value['dispatch'] is not None else None
        value['terminal'] = json.loads(value['terminal']) if value['terminal'] is not None else None
        result.append(value)
    return tuple(result)


_STEER_PREFIXES = ('answer-effective-', 'answer-steer-', 'turn-steer-', 'task-steer-',
    'answer-inherited-', 'answer-phase-', 'answer-legacy-')


def _steer_protocol_evidence(immutable):
    return ('workbench-steer-binding-v1' in immutable
        or any(kind.startswith(_STEER_PREFIXES) for kind in immutable))


def requires_product_answer_snapshot(request, immutable):
    turn_id = request['turn_id']
    marker = {'kind': 'session_event', 'object_id': turn_id, 'uri': f'crp://session/{turn_id}/{MANIFEST_KIND}'}
    return (_steer_protocol_evidence(immutable)
        or 'answer-auxiliary-protocol-v1' in immutable or MANIFEST_KIND in immutable
        or any(kind in immutable for kind in ADMISSION_KINDS) or marker in request.get('input', {}).get('refs', []))


def read_product_turn_snapshot(connection, turn_id):
    if not connection.in_transaction:
        raise ValueError('product_snapshot_transaction_required')
    row = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?', (turn_id,)).fetchone()
    if row is None:
        raise ValueError('product_snapshot_request_unavailable')
    request = json.loads(row[0])
    immutable = {kind: (reference, json.loads(raw)) for kind, reference, raw in connection.execute(
        'SELECT kind,payload_ref,payload_json FROM ai_turn_immutable_payloads WHERE turn_id=?', (turn_id,))}
    auxiliary = immutable.get('answer-auxiliary-protocol-v1')
    if auxiliary is not None and auxiliary[1] != {'schema_version': '1.0.0', 'turn_id': turn_id,
            'request_identity': admission_identity(request)}:
        raise ValueError('product_snapshot_auxiliary_protocol_changed')
    if auxiliary is None and not _steer_protocol_evidence(immutable):
        if requires_product_answer_snapshot(request, immutable):
            restore_admission_snapshot(request, immutable)
    events = [json.loads(row[0]) for row in connection.execute(
        'SELECT event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence', (turn_id,))]
    by_ref = {reference: payload for reference, payload in immutable.values()}
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)",
        ('ai_turn_steers', 'ai_turn_steer_model_grants'))}
    if tables == {'ai_turn_steers', 'ai_turn_steer_model_grants'}:
        steers = [{'payload_ref': row[0], 'payload': by_ref[row[0]], 'state': row[1], 'input_ref': row[2],
            'decision_ref': row[3], 'revision': row[4]} for row in connection.execute(
                'SELECT payload_ref,state,input_ref,decision_ref,revision FROM ai_turn_steers WHERE turn_id=? ORDER BY rowid', (turn_id,))]
        fields = ('tool_invocation_id', 'invocation_key', 'public_turn_id', 'steer_ref', 'input_ref', 'interruption_ref', 'model_request_id')
        grants = [dict(zip(fields, row)) for row in connection.execute(
            'SELECT tool_invocation_id,invocation_key,public_turn_id,steer_ref,input_ref,interruption_ref,model_request_id '
            'FROM ai_turn_steer_model_grants WHERE turn_id=? ORDER BY rowid', (turn_id,))]
    else:
        if (tables or request.get('policy_versions', {}).get('steer') == '@2'
                or any(event['type'].startswith('turn.steer.') for event in events)
                or _steer_protocol_evidence(immutable)):
            raise ValueError('product_snapshot_steer_schema_unavailable')
        steers, grants = [], []
    payloads = {}
    for event in events:
        if event['type'] == 'turn.resumed':
            reference = event['data'].get('payload_ref')
            row = connection.execute('SELECT payload_json FROM ai_turn_payloads WHERE turn_id=? AND payload_ref=?', (turn_id, reference)).fetchone()
            if row is None:
                raise ValueError('product_snapshot_resume_action_unavailable')
            payloads[reference] = json.loads(row[0])
        if event['type'] in {'model.attempt.terminal', 'model.completed', 'model.failed'} and event['data'].get('receipt_ref'):
            reference = event['data']['receipt_ref']
            row = connection.execute('SELECT payload_json FROM ai_turn_payloads WHERE turn_id=? AND payload_ref=?', (turn_id, reference)).fetchone()
            if row is None:
                raise ValueError('product_snapshot_model_receipt_unavailable')
            payloads[reference] = json.loads(row[0])
    return {'request': request, 'immutable': immutable, 'events': events, 'steers': steers,
        'grants': grants, 'payloads': payloads, 'attempts': read_model_attempt_bindings(connection, turn_id)}
