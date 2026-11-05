from copy import deepcopy
import json

from backend.recognition import RecognitionConflict
from core.ai_kernel.product_admission import ProductAdmissionItem, bundle_from_payload
from .shared_auxiliary import PREPARED, SharedAuxiliaryTurn
from .shared_rendered_preparation import _query_current
from .shared_search_input import decision_facts, question_facts, current_search_input, validate_search_input_shape


class SharedSearchTurn(SharedAuxiliaryTurn):
    def __init__(self, query, *, parent, number, question, key, validate):
        _query_current(query)
        if number == 0:
            fact, request = question_facts(query, parent, question, key)
            materials = []
        else:
            fact, request, prepared = decision_facts(query, parent, number, question)
            materials = prepared['material_proof']['selection']['origin_materials']
        if key != fact['key']:
            raise RecognitionConflict('shared_search_input_changed')
        self.query, self.search_input = query, fact
        from .policies import override
        with override(search=request['policy_versions']['search']):
            super().__init__(query.origin_admission, query.models, kind='web.search',
                project=query.origin_admission.lease.project_id, key=key, text=question,
                materials=materials, validate=validate, purpose='search')

    def _current_input(self):
        expected = self.search_input
        current = current_search_input(self.query, expected)
        if current != expected:
            raise RecognitionConflict('shared_search_input_changed')

    def validate_wire_parent(self):
        from ..kernel.answer_turns import ACTIVE_ANSWER
        from ..kernel.ai_execution_control import execution_checkpoint
        from .search import _execution_records, _native_search_snapshot
        parent = self.search_input['parent_turn_id']
        active = ACTIVE_ANSWER.get()
        if (active is None or active[0]['turn_id'] != parent
                or active[1].get_request(parent) != self.origin_admission._store().get_request(parent)
                or execution_checkpoint(active[0]) is None):
            raise RecognitionConflict('shared_search_parent_control_unavailable')
        snapshot = _native_search_snapshot(_execution_records(self.query), parent)
        if any(event['type'] in {'turn.completed', 'turn.failed', 'turn.cancelled'} for event in snapshot.events_after(parent)):
            raise RecognitionConflict('shared_search_parent_closed')

    def _seal(self, request, items):
        self._current_input()
        sources = [item for item in items if item.kind == 'material-origin-v1']
        if len(sources) != 1:
            raise RecognitionConflict('material_origin_binding_missing')
        item = sources[0]
        payload = {**item.payload, 'auxiliary': {**item.payload['auxiliary'], 'search_input': deepcopy(self.search_input)}}
        return super()._seal(request, tuple(ProductAdmissionItem(item.kind, item.ref, item.scope, payload)
            if entry is item else entry for entry in items))

    def _validate_request(self, records, models, request, *, purpose):
        super()._validate_request(records, models, request, purpose=purpose)
        store = self.origin_admission._store()
        if store.get_request(request['turn_id']) is not None:
            bundle = store.restore_admission(request)
        else:
            row = records.read(PREPARED, request['turn_id'])
            bundle = bundle_from_payload(row.payload['bundle']) if row is not None else records.pending.get(request['turn_id'])
        items = [item for item in bundle.items if item.kind == 'material-origin-v1']
        if len(items) != 1 or items[0].payload['auxiliary'].get('search_input') != self.search_input:
            raise RecognitionConflict('shared_search_input_changed')
        validate_search_input_shape(request, self.search_input)
        self._current_input()


def stage_search_wire(turn, control, policy):
    if type(turn) is not SharedSearchTurn:
        raise RecognitionConflict('search_execution_owner_changed')
    turn.validate_wire_parent()
    request, store = turn.request, turn.store
    route = store.get_immutable_payload(turn.turn_id, 'memory-model-route-v1')
    if route is None or route[1].get('purpose') != 'search':
        raise RecognitionConflict('search_authority_changed')
    payload = {'schema_version': 1, 'turn_id': turn.turn_id, 'question': request['input']['text'],
        'policy_version': request['policy_versions']['search'], 'parameters': policy.parameters(),
        'messages': policy.messages(request['input']['text']), 'route_ref': route[0]}
    reference = store.get_or_create_immutable_payload(turn.turn_id, 'shared-search-wire-input-v1', payload)
    store.get_or_create_immutable_payload(turn.turn_id, 'shared-search-wire-binding-v1',
        {'schema_version': 1, 'turn_id': turn.turn_id, 'model_request_id': control.model_request_id,
         'input_ref': reference, 'route_ref': route[0]})


def read_search_wire_facts(snapshot, request, route, output):
    from core.ai_kernel.contracts import validate_model_call_receipt
    from .policies import get
    turn_id = request['turn_id']
    prepared = snapshot.get_immutable_payload(turn_id, 'shared-search-wire-input-v1')
    binding = snapshot.get_immutable_payload(turn_id, 'shared-search-wire-binding-v1')
    policy = get('search', version=request['policy_versions']['search'])
    expected = {'schema_version': 1, 'turn_id': turn_id, 'question': request['input']['text'],
        'policy_version': request['policy_versions']['search'], 'parameters': policy.parameters(),
        'messages': policy.messages(request['input']['text']), 'route_ref': route[0]}
    if (prepared is None or prepared[1] != expected or binding is None
            or not isinstance(binding[1], dict)
            or set(binding[1]) != {'schema_version', 'turn_id', 'model_request_id', 'input_ref', 'route_ref'}
            or type(binding[1]['schema_version']) is not int or binding[1]['schema_version'] != 1
            or binding[1]['turn_id'] != turn_id or binding[1]['input_ref'] != prepared[0]
            or binding[1]['route_ref'] != route[0] or route[1].get('purpose') != 'search'):
        raise RecognitionConflict('shared_search_wire_input_changed')
    model_id = binding[1]['model_request_id']
    events = tuple(snapshot.events_after(turn_id))
    own = [event for event in events if event['correlation'].get('model_request_id') == model_id]
    routed = [event for event in own if event['type'] == 'model.routed'
        and event['data'].get('payload_ref') == route[0]]
    completed = [event for event in own if event['type'] == 'model.completed']
    if (not isinstance(model_id, str) or not model_id or len(routed) != 1 or len(completed) != 1
            or not events or events[-1]['type'] != 'turn.completed'
            or events[-1]['data'].get('payload_ref') != output[0]):
        raise RecognitionConflict('shared_search_wire_result_changed')
    receipt = validate_model_call_receipt(snapshot.get(completed[0]['data']['receipt_ref']))
    if (receipt.get('turn_id') != turn_id
            or receipt.get('model_request_id') != model_id or receipt.get('status') != 'completed'):
        raise RecognitionConflict('shared_search_wire_result_changed')
    return {'input': list(prepared), 'binding': list(binding), 'receipt_ref': completed[0]['data']['receipt_ref']}


def find_shared_search_index(query, key):
    from .material_origin_admission import read_origin_item
    admission = query.origin_admission
    admission.validate_execution_owner()
    records, store, matches = admission.destination.records, admission._store(), []
    expected = {'schema_version': 2, 'key': key, 'purpose': 'search',
                'origin': admission.lease.payload(), 'execution': admission.personal}
    for row in records.list('v2_memory_turn_keys'):
        identity = row.payload.get('identity', {})
        if identity.get('kind') != 'web.search' or identity.get('purpose') != 'search' or identity.get('project') != admission.lease.project_id:
            continue
        try:
            composite = json.loads(identity.get('key'))
        except (TypeError, ValueError):
            continue
        if (not isinstance(composite, dict) or composite.get('key') != key
                or composite.get('origin') != expected['origin'] or composite.get('execution') != expected['execution']):
            continue
        if (set(composite) != {*expected, 'materials', 'proof', 'sql_identities'}
                or type(composite['schema_version']) is not int
                or any(composite[field] != value for field, value in expected.items())):
            raise RecognitionConflict('search_identity_conflicted')
        request = store.get_request(row.object_id)
        if request is None:
            continue
        if request != row.payload['request']:
            raise RecognitionConflict('search_identity_conflicted')
        bundle = store.restore_admission(request)
        sources = [item for item in bundle.items if item.kind == 'material-origin-v1'] if bundle is not None else []
        if len(sources) != 1:
            raise RecognitionConflict('material_origin_binding_missing')
        source = sources[0]
        execution = source.payload['execution']
        if (execution['owner_user_id'] != admission.destination.owner_user_id
                or execution['namespace_id'] != admission.destination.namespace):
            raise RecognitionConflict('search_execution_owner_changed')
        lease, auxiliary = read_origin_item(request, source,
            actor_user_id=source.payload['actor_user_id'], personal=execution)
        if (lease != admission.lease or auxiliary is None or auxiliary.get('search_input', {}).get('key') != key
                or composite['materials'] != auxiliary['materials']
                or composite['sql_identities'] != auxiliary['selection'].get('sql_identities')):
            raise RecognitionConflict('shared_search_input_changed')
        matches.append(row)
    if len(matches) > 1:
        raise RecognitionConflict('search_identity_conflicted')
    return matches[0] if matches else None
