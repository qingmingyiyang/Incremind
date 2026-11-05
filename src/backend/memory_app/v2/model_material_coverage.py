from copy import deepcopy

from backend.recognition import RecognitionConflict, WorkScope
from core.ai_kernel.product_admission import admission_identity
from ..answer_proof import REVIEW_KEY, RAW_ANSWER_KEY, EVIDENCE_ANSWER_KEY
from ..kernel.answer_continuations import PLAN_KIND, decode, encode


_PLAN_FIELDS = ('project_id', 'question', 'chosen', 'profile', 'history', 'history_count',
    'part_context', 'part_context_ref', 'target', 'policy_versions', '_project_constraints', 'origin_lease')


def frozen_primary_plan(query, request, logical_invocation, *, plan=None, store=None):
    store = query.origin_admission._store() if store is None else store
    if logical_invocation not in {'answer', REVIEW_KEY, RAW_ANSWER_KEY, EVIDENCE_ANSWER_KEY}:
        from .primary_wire_messages import PrimaryWireProjection
        logical_invocation = PrimaryWireProjection(store, request, None).logical(logical_invocation, logical_invocation)
    if store.get_request(request['turn_id']) != request:
        raise RecognitionConflict('material_origin_native_request_changed')
    if logical_invocation in {'answer', REVIEW_KEY}:
        kind = PLAN_KIND
    elif logical_invocation in {RAW_ANSWER_KEY, EVIDENCE_ANSWER_KEY}:
        kind = 'answer-fast-trial-' + logical_invocation
    else:
        raise RecognitionConflict('material_origin_model_invocation_changed')
    saved = store.get_immutable_payload(request['turn_id'], kind)
    if saved is None:
        raise RecognitionConflict('material_origin_model_plan_unavailable')
    body = saved[1]
    if (not isinstance(body, dict) or set(body) != {'turn_id', 'project_id', 'question', 'request', 'lease', 'capsule'}
            or body['turn_id'] != request['turn_id'] or body['request'] != request
            or body['project_id'] != request['scope']['project_id'] or body['question'] != request['input']['text']):
        raise RecognitionConflict('material_origin_model_plan_changed')
    capsule = decode(body['capsule'])
    if not isinstance(capsule, dict) or set(capsule) != {'plan', 'guards', 'history', 'preview_lifetime'}:
        raise RecognitionConflict('material_origin_model_plan_changed')
    frozen = capsule['plan']
    if (not isinstance(frozen, dict) or frozen.get('scope') != WorkScope('local-user', body['project_id'])
            or frozen.get('project_id') != body['project_id'] or frozen.get('question') != body['question']
            or frozen.get('policy_versions') != request['policy_versions']
            or not isinstance(frozen.get('chosen'), list) or not isinstance(frozen.get('profile'), dict)
            or not isinstance(capsule['guards'], dict) or not isinstance(capsule['history'], dict)
            or capsule['history'].get('text', '') != frozen.get('history', '')):
        raise RecognitionConflict('material_origin_model_plan_changed')
    if kind != PLAN_KIND and (not isinstance(frozen.get('_fast_trial'), dict)
            or frozen['_fast_trial'].get('invocation') != logical_invocation):
        raise RecognitionConflict('material_origin_model_plan_changed')
    if plan is not None and any(encode(plan.get(field)) != encode(frozen.get(field)) for field in _PLAN_FIELDS):
        raise RecognitionConflict('material_origin_model_input_coverage_changed')
    return frozen, capsule['guards'], capsule['history']


def validate_primary_guards(query, frozen, guards, history):
    if set(guards) - {'overview_guard', 'bookshelf_guard', 'search_guard'}:
        raise RecognitionConflict('material_origin_model_guards_changed')
    project = frozen['project_id']
    if 'overview_guard' in guards:
        from .overviews import validate_navigation
        value = guards['overview_guard']
        if not isinstance(value, dict) or set(value) != {'project', 'scene', 'overview'} or value['project'] != project:
            raise RecognitionConflict('material_origin_model_guards_changed')
        validate_navigation(query, project, value['scene'], value['overview'])
    if 'bookshelf_guard' in guards:
        from .bookshelf import _validate
        value = guards['bookshelf_guard']
        if not isinstance(value, dict) or set(value) != {'spines', 'version', 'project', 'scene'} or value['project'] != project:
            raise RecognitionConflict('material_origin_model_guards_changed')
        _validate(query, value['spines'], value['version'], project, value['scene'])
    if 'search_guard' in guards:
        from .search import validate_search_binding
        validate_search_binding(query, guards['search_guard'], frozen)
    if history.get('text'):
        from .followup import validate_history
        validate_history(query, project, history, frozen['target'])


def validate_primary_input(query, request, logical_invocation, input_ref, material_proof, messages):
    store = query.origin_admission._store()
    matches = []
    for event in store.events_after(request['turn_id']):
        if event['type'] != 'model.requested' or event['data'].get('model_call_purpose') != 'primary':
            continue
        identity = event['correlation']['model_request_id']
        bound = store.get_immutable_payload(request['turn_id'], 'answer-invocation-binding-' + identity)
        if bound is None or bound[1].get('input_ref') != input_ref:
            continue
        binding = bound[1]
        fields = {'turn_id', 'project_id', 'model_request_id', 'invocation_key', 'logical_invocation',
            'tool_call_id', 'input_ref', 'run_owner_id', 'run_generation', 'request_identity'}
        if set(binding) not in (fields, fields | {'phase', 'phase_logical'}):
            raise RecognitionConflict('material_origin_model_input_changed')
        actual = store.get_immutable_payload(request['turn_id'], 'answer-model-input-' + binding['invocation_key'])
        input_fields = {'messages', 'purpose', 'response_schema', 'material_proof'}
        if (actual is None or actual[0] != input_ref or binding['turn_id'] != request['turn_id']
                or set(actual[1]) != input_fields | ({'phase', 'phase_logical'} if 'phase' in binding else set())
                or actual[1].get('purpose') != 'primary' or not isinstance(actual[1].get('response_schema'), dict)
                or binding['project_id'] != request['scope']['project_id'] or binding['model_request_id'] != identity
                or not isinstance(binding['run_owner_id'], str) or not binding['run_owner_id']
                or type(binding['run_generation']) is not int or binding['run_generation'] < 1
                or binding['request_identity'] != admission_identity(request)
                or binding['tool_call_id'] != event['correlation']['tool_call_id']
                or actual[1].get('messages') != messages or actual[1].get('material_proof') != material_proof):
            raise RecognitionConflict('material_origin_model_input_changed')
        logical = binding.get('phase_logical', binding['logical_invocation'])
        if binding.get('phase') is None:
            from ..answer_legacy import read_legacy_input
            legacy = read_legacy_input(store, request['turn_id'], binding['logical_invocation'])
            if legacy is not None:
                logical = legacy['intent'][1]['logical_invocation']
        elif (binding['phase'] not in {'single', 'first', 'final-review'}
                or actual[1].get('phase') != binding['phase'] or actual[1].get('phase_logical') != logical):
            raise RecognitionConflict('material_origin_model_input_changed')
        if logical_invocation is not None and logical != logical_invocation:
            raise RecognitionConflict('material_origin_model_invocation_changed')
        matches.append(logical)
    if not matches or len(set(matches)) != 1:
        raise RecognitionConflict('material_origin_model_input_unavailable')
    return matches[0]


def validate_primary_material_proof_shape(material_proof, *, native_request):
    from core.ai_kernel import validate_turn_request
    from .material_origin_sql import validate_sql_shape
    fields = {'origin_ref', 'origin_materials', 'search_fragments', 'personal_context', 'schema_version', 'sql_identities'}
    if (not isinstance(material_proof, dict) or set(material_proof) != {'privacy', 'selection'}
            or not isinstance(material_proof['selection'], dict) or set(material_proof['selection']) != fields
            or type(material_proof['selection']['schema_version']) is not int
            or material_proof['selection']['schema_version'] != 2
            or not isinstance(material_proof['selection']['origin_materials'], list)
            or not isinstance(material_proof['selection']['search_fragments'], list)):
        raise RecognitionConflict('material_origin_model_proof_changed')
    validate_turn_request({**native_request, 'privacy': deepcopy(material_proof['privacy'])})
    validate_sql_shape(material_proof['selection']['sql_identities'])
    from .search import validate_search_fragments_shape
    validate_search_fragments_shape(material_proof['selection']['search_fragments'],
        project=native_request['scope']['project_id'])


def primary_materials(query, frozen):
    selected = []
    for candidate in frozen['chosen']:
        if candidate['kind'] == 'search':
            continue
        kind = 'original_source' if candidate['kind'] == 'source' else candidate['kind']
        material = {'type': kind, 'id': candidate['entry']['id'], 'revision': candidate['entry']['revision'],
            'project_id': candidate['scope'].project_id,
            'namespace': candidate.get('namespace', query.source_namespace)}
        if material not in selected:
            selected.append(material)
    for item in frozen['profile'].get('items', []):
        material = {'type': 'recognition', 'id': item['id'], 'revision': item['revision'],
            'project_id': 'me', 'namespace': query.origin_admission.personal['namespace']}
        if material not in selected:
            selected.append(material)
    return selected


def build_primary_material_proof(query, request, logical_invocation, messages, *, plan=None,
        invocation_key=None, wire=False):
    from .material_input_binding import prepared_primary_input
    from .material_origin_context import freeze_personal_context, personal_context_materials, context_sql_identities
    from .material_origin_sql import freeze_sql_identities
    from .shared_auxiliary import _qualified_commit
    from .primary_wire_messages import PrimaryWireProjection

    query._validate_material_model_owner(query.models)
    store = query.origin_admission._store()
    projection = PrimaryWireProjection(store, request,
        lambda logical: frozen_primary_plan(query, request, logical)[0],
        execution=query.answer_turns.active.get(request['turn_id']))
    logical_invocation = projection.logical(logical_invocation, invocation_key or logical_invocation)
    projection.validate(logical_invocation, messages, invocation_key=invocation_key, require_projected=wire)
    frozen, guards, history = frozen_primary_plan(query, request, logical_invocation, plan=plan)
    origins = query.origin_admission.restore(request)
    with _qualified_commit(query.origin_admission):
        validate_primary_guards(query, frozen, guards, history)
        guarded = frozen
        if 'search_guard' in guards:
            from .search import validate_search_binding
            # 原冻结清单单独保存来源证明，实际计划校验仍消费同一个证明。
            guarded = {**frozen, 'search_guard': lambda: validate_search_binding(query, guards['search_guard'], frozen)}
        query.validate_ask_plan(guarded)
        context = freeze_personal_context(query, request, frozen, history)
        selected = primary_materials(query, frozen)
        for material in personal_context_materials(query, request, context):
            if material not in selected:
                selected.append(material)
        allowed, privacy, origin = query.freeze_materials(query.models, frozen['project_id'], selected,
            local_only=frozen['target']['execution_location'] == 'local', native_request=request)
        if (len(allowed) != len(selected) or origin['origin_materials'] != selected
                or frozen['target']['execution_location'] == 'remote' and not privacy['allow_remote']):
            raise RecognitionConflict('material_origin_model_egress_changed')
        with prepared_primary_input(query, request, logical_invocation, selected, plan=frozen) as prepared:
            identities = freeze_sql_identities(origins, selected, query=query, prepared_input=prepared)
        identities = context_sql_identities(query, request, context, identities)
        from .search import shared_search_fragments
        fragments = shared_search_fragments(query, frozen, binding=guards.get('search_guard'))
        proof = {'privacy': privacy, 'selection': {**origin, 'schema_version': 2,
            'sql_identities': identities, 'personal_context': context, 'search_fragments': fragments}}
        validate_primary_material_proof_shape(proof, native_request=request)
        return proof


def validate_primary_material_proof(query, models, material_proof, *, native_request, messages,
        logical_invocation=None, input_ref=None, purpose='generation', invocation_key=None):
    query._validate_material_model_owner(models)
    if models is not query.models or purpose != 'generation':
        raise RecognitionConflict('material_origin_model_owner_changed')
    validate_primary_material_proof_shape(material_proof, native_request=native_request)
    if input_ref is not None:
        logical_invocation = validate_primary_input(query, native_request, logical_invocation,
            input_ref, material_proof, messages)
        from .primary_wire_messages import PrimaryWireProjection
        projection = PrimaryWireProjection(query.origin_admission._store(), native_request,
            lambda logical: frozen_primary_plan(query, native_request, logical)[0])
        binding, _ = projection.binding(input_ref)
        if invocation_key is not None and invocation_key != binding['invocation_key']:
            raise RecognitionConflict('material_origin_model_invocation_changed')
        invocation_key = binding['invocation_key']
    if logical_invocation is None:
        raise RecognitionConflict('material_origin_model_input_unavailable')
    if build_primary_material_proof(query, native_request, logical_invocation, messages,
            invocation_key=invocation_key, wire=True) != material_proof:
        raise RecognitionConflict('material_origin_model_input_coverage_changed')
    query.project_constraint_input.validate(native_request)
    query.validate_retrieval_origin()
