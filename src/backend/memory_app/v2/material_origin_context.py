from copy import deepcopy

from backend.recognition import RecognitionConflict
from core.ai_kernel.product_admission import MANIFEST_KIND
from ..transaction_records import TransactionRecords
from .material_origin_readers import qualified_record_read
from .shared_auxiliary import _qualified_commit
from .shared_history import read_model_history_answer


_CONTEXT_FIELDS = {'owner_user_id', 'namespace_id', 'history', 'part_context_ref'}
_ENTRY_FIELDS = {'public_id', 'public_revision', 'native_turn_id', 'manifest_ref', 'identity_ref', 'inputs'}


def _entry(actual):
    snapshot, public = actual['snapshot'], actual['public']
    manifest = snapshot.get_immutable_payload(snapshot.turn_id, MANIFEST_KIND)
    identity = snapshot.get_immutable_payload(snapshot.turn_id, 'workbench-steer-binding-v1')
    return {'public_id': public.object_id, 'public_revision': public.revision,
        'native_turn_id': snapshot.turn_id, 'manifest_ref': manifest[0], 'identity_ref': identity[0],
        'inputs': [{'model_request_id': row['model_request_id'], 'input_ref': row['input_ref'],
            'binding_ref': row['binding_ref']} for row in actual['model_inputs']]}


def _validate_context(query, request, context):
    admission = query.origin_admission
    admission.restore(request)
    if (not isinstance(context, dict) or set(context) != _CONTEXT_FIELDS
            or context['owner_user_id'] != admission.destination.owner_user_id
            or context['namespace_id'] != admission.destination.namespace
            or not isinstance(context['history'], list) or context['part_context_ref'] is not None):
        raise RecognitionConflict('material_origin_personal_context_changed')
    seen = set()
    for entry in context['history']:
        if (not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS
                or not isinstance(entry['public_id'], str) or not entry['public_id']
                or type(entry['public_revision']) is not int or entry['public_revision'] < 1
                or entry['native_turn_id'] != entry['public_id']
                or entry['native_turn_id'] in {request['turn_id'], *seen}):
            raise RecognitionConflict('material_origin_history_changed')
        seen.add(entry['native_turn_id'])
    return context


def freeze_personal_context(query, request, plan, history):
    from .followup import _answer, _history_text
    if (not isinstance(history.get('turns'), list)
            or history.get('text', '') != plan.get('history', '')
            or _history_text(history['turns']) != history.get('text', '')):
        raise RecognitionConflict('material_origin_history_text_changed')
    rows = {}
    for turn in history['turns']:
        if not isinstance(turn, dict) or set(turn) != {'id', 'revision', 'question', 'answer'}:
            raise RecognitionConflict('material_origin_history_changed')
        actual = read_model_history_answer(query, turn['id'], turn['revision'])
        public = actual['public']
        if (turn['question'] != public.payload['user_text']
                or turn['answer'] != _answer(actual['facts']['answer']['answer'])):
            raise RecognitionConflict('material_origin_history_text_changed')
        rows[public.object_id] = _entry(actual)
    for dependency in history.get('dependencies', []):
        fields = {'turns', 'chosen', 'snapshots', 'preferences', 'multipart', 'clarifications'}
        if not isinstance(dependency, dict) or set(dependency) != fields:
            raise RecognitionConflict('material_origin_history_dependency_changed')
        for turn in dependency['turns']:
            if not isinstance(turn, dict) or set(turn) != {'id', 'revision'}:
                raise RecognitionConflict('material_origin_history_dependency_changed')
            value = _entry(read_model_history_answer(query, turn['id'], turn['revision']))
            if turn['id'] in rows and rows[turn['id']] != value:
                raise RecognitionConflict('material_origin_history_dependency_changed')
            rows[turn['id']] = value
    reference = deepcopy(plan.get('part_context_ref'))
    if plan.get('part_context') is not None or reference is not None:
        raise RecognitionConflict('material_origin_part_context_proof_unavailable')
    result = {'owner_user_id': query.origin_admission.destination.owner_user_id,
        'namespace_id': query.origin_admission.destination.namespace,
        'history': list(rows.values()), 'part_context_ref': reference}
    return _validate_context(query, request, result)


def _history_inputs(query, request, context, *, seen=()):
    _validate_context(query, request, context)
    if len(seen) >= 32:
        raise RecognitionConflict('material_origin_history_cycle')
    for entry in context['history']:
        if entry['native_turn_id'] in (*seen, request['turn_id']):
            raise RecognitionConflict('material_origin_history_cycle')
        actual = read_model_history_answer(query, entry['public_id'], entry['public_revision'])
        if _entry(actual) != entry:
            raise RecognitionConflict('material_origin_history_changed')
        yield entry, actual


def personal_context_materials(query, request, context):
    selected = []
    for _, actual in _history_inputs(query, request, context):
        for item in actual['model_inputs']:
            for material in item['payload']['material_proof']['selection']['origin_materials']:
                if material not in selected:
                    selected.append(deepcopy(material))
    return selected


def context_sql_identities(query, request, context, identities, *, seen=()):
    from .material_input_binding import prepared_historical_input
    from .material_origin_sql import freeze_sql_identities, _append, _identity
    from .model_material_coverage import frozen_primary_plan, primary_materials
    from .primary_wire_messages import PrimaryWireProjection
    from ..kernel.answer_continuations import decode
    from backend.recognition import WorkScope
    from .search import historical_search_fragments
    result = {}
    for identity in identities:
        _append(result, identity)
    origins = query.origin_admission.restore(request)
    with _qualified_commit(query.origin_admission) as qualification:
        with qualified_record_read(query.origin_admission.destination.records,
                validate=qualification._validate) as transaction:
            actor = TransactionRecords(transaction)
            qualification._actor_reader(actor)
            for entry, actual in _history_inputs(query, request, context, seen=seen):
                # 本人的旧正文作为新输入时，沿当前来源和实际旧完成证明重取全部依赖。
                _append(result, _identity(transaction, query.origin_admission.personal['namespace'],
                    'v2_turns', entry['public_id']))
                for item in actual['model_inputs']:
                    proof = item['payload']['material_proof']
                    selection = proof['selection']
                    rag = item['payload']['purpose'] == 'aux' and item['kind'].startswith('answer-gap-input-')
                    if item['payload']['purpose'] != 'primary' and not rag:
                        raise RecognitionConflict('material_origin_history_auxiliary_proof_unavailable')
                    if rag:
                        from .shared_rag_materials import RAG_FIELDS
                        from .agentic_rag import _descriptors
                        if set(selection) != RAG_FIELDS or selection['privacy'] != proof['privacy']:
                            raise RecognitionConflict('material_origin_history_auxiliary_proof_unavailable')
                        history = decode(selection['history'])
                        frozen = {'project_id': actual['facts']['request']['scope']['project_id'],
                            'scope': WorkScope('local-user', actual['facts']['request']['scope']['project_id']),
                            'chosen': decode(selection['candidates']), 'profile': decode(selection['profile']),
                            'history': history.get('text', ''), 'target': query.ask_target()}
                        if history == {} and frozen['history'] == '':
                            history = {'text': '', 'turns': []}
                        search_binding = selection['search']
                    else:
                        projection = PrimaryWireProjection(actual['snapshot'], actual['facts']['request'],
                            lambda logical: frozen_primary_plan(query, actual['facts']['request'], logical,
                                store=actual['snapshot'])[0])
                        projection.validate(item['logical'], item['payload']['messages'],
                            invocation_key=item['invocation_key'], require_projected=True)
                        frozen, guards, history = frozen_primary_plan(query, actual['facts']['request'], item['logical'],
                            store=actual['snapshot'])
                        search_binding = guards.get('search_guard')
                    expected_context = freeze_personal_context(query, request, frozen, history)
                    if expected_context != selection['personal_context']:
                        raise RecognitionConflict('material_origin_history_dependency_changed')
                    if rag:
                        expected = _descriptors(frozen['chosen'], source_namespace=query.source_namespace)
                        expected.extend({'type': 'recognition', 'id': value['id'], 'revision': value['revision'],
                            'project_id': 'me', 'namespace': query.origin_admission.personal['namespace']}
                            for value in frozen['profile'].get('items', []))
                        expected = [value for number, value in enumerate(expected) if value not in expected[:number]]
                    else:
                        expected = primary_materials(query, frozen)
                    for material in personal_context_materials(query, request, expected_context):
                        if material not in expected:
                            expected.append(material)
                    if expected != selection['origin_materials']:
                        raise RecognitionConflict('material_origin_history_materials_changed')
                    if historical_search_fragments(query, frozen, binding=search_binding) != selection['search_fragments']:
                        raise RecognitionConflict('material_origin_history_search_changed')
                    query._validate_ask_sources({'project_id': frozen['project_id'], 'scope': frozen['scope'],
                        'chosen': [value for value in frozen['chosen'] if value['kind'] != 'search'], 'target': query.ask_target()})
                    with prepared_historical_input(query, request, entry['public_id'], entry['public_revision'],
                            item['input_ref']) as prepared:
                        current = freeze_sql_identities(origins, selection['origin_materials'],
                            query=query, prepared_input=prepared)
                    current = context_sql_identities(query, request, selection['personal_context'], current,
                        seen=(*seen, entry['native_turn_id']))
                    if current != selection['sql_identities']:
                        raise RecognitionConflict('material_origin_history_sql_identity_changed')
                    for identity in current:
                        _append(result, identity)
            qualification._actor_reader(actor)
    return [result[key] for key in sorted(result)]
