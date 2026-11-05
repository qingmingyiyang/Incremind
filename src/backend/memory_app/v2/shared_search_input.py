from copy import deepcopy

from backend.recognition import RecognitionConflict


_FIELDS = {'schema_version', 'producer', 'parent_turn_id', 'round', 'question', 'key', 'input_ref',
           'binding_ref', 'result_ref', 'model_request_id', 'receipt_ref'}
_QUESTION_FIELDS = {'schema_version', 'producer', 'parent_turn_id', 'question', 'key', 'manifest_ref'}


def validate_search_input_value(value):
    fields = _QUESTION_FIELDS if isinstance(value, dict) and value.get('producer') == 'question' else _FIELDS
    if (not isinstance(value, dict) or set(value) != fields
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or any(not isinstance(value[key], str) or not value[key]
                for key in fields - {'schema_version', 'round'})
            or fields == _FIELDS and (value['producer'] != 'rag-decision'
                or type(value['round']) is not int or value['round'] < 1
                or value['key'] != value['parent_turn_id'] + ':rag:' + str(value['round']))
            or fields == _QUESTION_FIELDS and value['key'] != value['parent_turn_id']):
        raise RecognitionConflict('shared_search_input_changed')


def validate_search_input_shape(request, value):
    validate_search_input_value(value)
    if (value['parent_turn_id'] == request['turn_id']
            or request['desired_outcome'] != 'web.search' or request['input']['text'] != value['question']):
        raise RecognitionConflict('shared_search_input_changed')


def question_facts(query, parent, question, key):
    from .search import _execution_records, _native_search_snapshot
    from .shared_rendered_preparation import _query_current
    _query_current(query)
    snapshot = _native_search_snapshot(_execution_records(query), parent)
    fact, request = _read_question(snapshot, parent, question, key)
    if request['scope']['project_id'] != query.origin_admission.lease.project_id:
        raise RecognitionConflict('shared_search_input_changed')
    query.origin_admission.restore(request)
    query.validate_answer_request(query.models, request)
    return fact, request


def _read_question(snapshot, parent, question, key):
    from core.ai_kernel.product_admission import MANIFEST_KIND
    request = snapshot.get_request(parent)
    manifest = snapshot.get_immutable_payload(parent, MANIFEST_KIND)
    if (request is None or manifest is None or request['desired_outcome'] != 'project.answer'
            or request['input']['text'] != question or key != parent
            or len([event for event in snapshot.events_after(parent) if event['type'] == 'turn.accepted']) != 1):
        raise RecognitionConflict('shared_search_input_changed')
    return {'schema_version': 1, 'producer': 'question', 'parent_turn_id': parent,
        'question': question, 'key': key, 'manifest_ref': manifest[0]}, request


def current_search_input(query, value):
    validate_search_input_value(value)
    if value['producer'] == 'question':
        return question_facts(query, value['parent_turn_id'], value['question'], value['key'])[0]
    return decision_facts(query, value['parent_turn_id'], value['round'], value['question'])[0]


def decision_facts(query, parent, number, question):
    from .agentic_rag import validate_material_proof
    from .search import _execution_records, _native_search_snapshot
    from .shared_rendered_preparation import _query_current
    _query_current(query)
    store = _native_search_snapshot(_execution_records(query), parent)
    fact, request, value = _read_decision(store, parent, number, question)
    if request['scope']['project_id'] != query.origin_admission.lease.project_id:
        raise RecognitionConflict('rag_search_decision_changed')
    query.origin_admission.restore(request)
    query.validate_answer_request(query.models, request)
    validate_material_proof(query, request['scope']['project_id'], query.ask_target(),
        value['material_proof']['selection'], request=request)
    return fact, request, value


def _read_decision(store, parent, number, question):
    from ..answer_effective import _completed_results
    from .agentic_rag import RagDecision
    from .policies import get
    from .shared_rag_materials import RAG_FIELDS
    from .material_origin_sql import validate_sql_shape
    if type(number) is not int or number < 1 or not isinstance(question, str) or not question:
        raise RecognitionConflict('rag_search_decision_changed')
    request = store.get_request(parent)
    if request is None or request['desired_outcome'] != 'project.answer':
        raise RecognitionConflict('rag_search_decision_changed')
    policy = get('retrieve', version=request['policy_versions']['retrieve'])
    if not getattr(policy, 'agentic', False) or number > policy(None, operation='agentic_limits')['decisions']:
        raise RecognitionConflict('rag_search_decision_changed')
    invocation = 'rag-decision-' + str(number)
    prepared = store.get_immutable_payload(parent, 'answer-gap-input-' + invocation + '-v1')
    result = store.get_immutable_payload(parent, 'answer-model-result-' + invocation)
    if (prepared is None or result is None or set(result[1]) != {'output', 'metadata'}
            or not isinstance(result[1]['metadata'], dict)):
        raise RecognitionConflict('rag_search_decision_unavailable')
    decision = RagDecision.model_validate(result[1]['output']).model_dump()
    if decision['action'] != 'search' or decision['source'] != 'web' or decision['query'] != question:
        raise RecognitionConflict('rag_search_decision_changed')
    value = prepared[1]
    fields = {'messages', 'material_proof', 'policy_versions', 'purpose', 'response_schema',
              'max_tokens', 'route_ref', 'route_revision'}
    if (set(value) != fields or value['purpose'] != 'aux'
            or value['policy_versions'] != request['policy_versions']
            or value['response_schema'] != RagDecision.model_json_schema()
            or value['max_tokens'] != policy(None, operation='agentic_limits')['output_tokens']
            or not isinstance(value['material_proof'], dict)
            or set(value['material_proof']) != {'privacy', 'selection'}):
        raise RecognitionConflict('rag_search_decision_changed')
    selection = value['material_proof']['selection']
    if (not isinstance(selection, dict) or set(selection) != RAG_FIELDS
            or type(selection['schema_version']) is not int or selection['schema_version'] != 2
            or selection['privacy'] != value['material_proof']['privacy']):
        raise RecognitionConflict('rag_material_origin_changed')
    validate_sql_shape(selection['sql_identities'])
    previous = value['material_proof']['selection'].get('search')
    if previous is not None and (previous.get('version') != 2 or previous.get('parent_turn_id') != parent
            or any(type(branch.get('round')) is not int or not 0 < branch['round'] < number
                for branch in previous.get('branches', []))):
        raise RecognitionConflict('rag_search_decision_changed')
    events = tuple(store.events_after(parent))
    completed = _completed_results(store, events, result[1])
    if len(completed) != 1 or completed[0]['turn_id'] != parent:
        raise RecognitionConflict('rag_search_decision_unavailable')
    terminal = completed[0]
    matches = []
    for kind, (reference, binding) in store.value['immutable'].items():
        if not kind.startswith('answer-gap-binding-' + invocation + '-v1'):
            continue
        expected = {'version': 1, 'turn_id': parent, 'invocation_key': invocation, 'purpose': 'aux',
            'input_ref': prepared[0], 'route_ref': value['route_ref'], 'route_revision': value['route_revision']}
        if (not isinstance(binding, dict) or set(binding) != {*expected, 'model_request_id'}
                or any(binding[key] != entry for key, entry in expected.items())
                or not isinstance(binding['model_request_id'], str) or not binding['model_request_id']):
            raise RecognitionConflict('rag_search_decision_changed')
        own = [event for event in events if event['correlation'].get('model_request_id') == binding['model_request_id']]
        requested = [event for event in own if event['type'] == 'model.requested'
            and event['data'].get('model_call_purpose') == 'aux']
        if len(requested) == 1 and terminal['correlation']['model_request_id'] == binding['model_request_id']:
            matches.append((reference, binding['model_request_id'], terminal['data']['receipt_ref']))
    if len(matches) != 1:
        raise RecognitionConflict('rag_search_decision_unavailable')
    binding_ref, model_id, receipt_ref = matches[0]
    fact = {'schema_version': 1, 'producer': 'rag-decision', 'parent_turn_id': parent, 'round': number, 'question': question,
        'key': parent + ':rag:' + str(number), 'input_ref': prepared[0], 'binding_ref': binding_ref,
        'result_ref': result[0], 'model_request_id': model_id, 'receipt_ref': receipt_ref}
    return deepcopy(fact), request, deepcopy(value)


def validate_search_claim_sql(origins, request, scope, actor_reader):
    _validate_search_source_sql(origins, request, scope, actor_reader)


def validate_search_publication_sql(origins, request, scope, actor_reader):
    _validate_search_source_sql(origins, request, scope, actor_reader)


def _validate_search_source_sql(origins, request, scope, actor_reader):
    from .material_origin_admission import read_origin_item
    from .search import _native_search_snapshot
    from .shared_rag_materials import RAG_FIELDS
    value = origins.auxiliary['search_input']
    validate_search_input_shape(request, value)
    if scope._admission is not origins.admission:
        raise RecognitionConflict('material_origin_commit_owner_changed')
    scope._actor_reader(actor_reader)
    snapshot = _native_search_snapshot(origins.admission.destination.records, value['parent_turn_id'])
    if any(event['type'] in {'turn.completed', 'turn.failed', 'turn.cancelled'}
            for event in snapshot.events_after(value['parent_turn_id'])):
        raise RecognitionConflict('shared_search_parent_closed')
    if value['producer'] == 'question':
        fact, parent = _read_question(snapshot, value['parent_turn_id'], value['question'], value['key'])
        identities = []
    else:
        fact, parent, prepared = _read_decision(snapshot, value['parent_turn_id'], value['round'], value['question'])
        proof = prepared['material_proof']
        selection = proof['selection']
        if (not isinstance(selection, dict) or set(selection) != RAG_FIELDS
                or type(selection['schema_version']) is not int or selection['schema_version'] != 2
                or selection['privacy'] != proof['privacy']):
            raise RecognitionConflict('rag_material_origin_changed')
        identities = selection['sql_identities']
    if fact != value:
        raise RecognitionConflict('shared_search_input_changed')
    bundle = snapshot.restore_admission(parent)
    sources = [item for item in bundle.items if item.kind == 'material-origin-v1'] if bundle is not None else []
    if len(sources) != 1:
        raise RecognitionConflict('material_origin_binding_missing')
    lease, _ = read_origin_item(parent, sources[0], actor_user_id=origins.admission.actor.user_id,
        personal=origins.admission.personal)
    if lease != origins.lease:
        raise RecognitionConflict('material_origin_scope_changed')
    constraints = getattr(origins.admission, 'constraint_input', None)
    if constraints is None or getattr(constraints, '_shared_origin', None) is not origins.admission:
        raise RecognitionConflict('shared_search_input_changed')
    constraints.validate(parent, reader=actor_reader)
    scope.validate_sql(identities, actor_reader=actor_reader)
    scope._actor_reader(actor_reader)
