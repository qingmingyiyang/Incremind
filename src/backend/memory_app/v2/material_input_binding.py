from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from threading import get_ident

from backend.recognition import RecognitionConflict, WorkScope
from ..context_adapter import format_recognition_content
from ..kernel.answer_continuations import decode, encode
from ..source_egress import SourceEgressService, recognition_service
from ..transaction_records import TransactionRecords
from .model_material_coverage import frozen_primary_plan
from .material_read_context import prepared_primary_scope, _register_primary_type
from .profile import PREFIX
from .shared_rendered_preparation import _query_current


_CURRENT = ContextVar('qualified_primary_material_input', default=None)
_INPUTS = {}


def _current_owner(query, request):
    _query_current(query)
    if query.origin_admission._store().get_request(request['turn_id']) != request:
        raise RecognitionConflict('material_origin_prepared_input_owner_changed')
    query.origin_admission.restore(request)


class PreparedMaterialInput:
    def __init__(self):
        self._token = object()

    def _issued(self):
        state = _INPUTS.get(self._token)
        return (state is not None and state['object'] is self and _CURRENT.get() is self._token
            and state['thread'] == get_ident())

    def _state(self, query):
        state = _INPUTS.get(self._token)
        if (state is None or state['object'] is not self or _CURRENT.get() is not self._token
                or state['thread'] != get_ident() or state['query'] is not query):
            raise RecognitionConflict('material_origin_prepared_input_expired')
        _current_owner(query, state['request'])
        return state

    def body(self, query, origins, *, materials=None):
        state = self._state(query)
        if (origins.admission is not query.origin_admission or origins.reference != state['reference']
                or materials is not None and materials != state['materials']):
            raise RecognitionConflict('material_origin_prepared_input_changed')
        if state['gap_proof'] is not None:
            selection = state['gap_proof']
            current = ({'chosen': decode(selection['candidates']), 'profile': decode(selection['profile'])}, None, None)
        elif state['rag_plan'] is not None:
            current = (_gap_body(state['rag_plan']), None, None)
        elif state['historical'] is None:
            current = frozen_primary_plan(query, state['request'], state['logical'])
        else:
            from .shared_history import read_model_history_answer
            binding = state['historical']
            actual = read_model_history_answer(query, binding['id'], binding['revision'])
            selected = [row for row in actual['model_inputs'] if row['input_ref'] == binding['input_ref']]
            if len(selected) != 1 or selected[0]['payload'] != binding['input']:
                raise RecognitionConflict('material_origin_history_input_changed')
            if state['rag_historical']:
                selection = selected[0]['payload']['material_proof']['selection']
                current = ({'chosen': decode(selection['candidates']), 'profile': decode(selection['profile'])}, None, None)
            else:
                current = frozen_primary_plan(query, actual['facts']['request'], state['logical'], store=actual['snapshot'])
        if encode(current) != encode(state['frozen']):
            raise RecognitionConflict('material_origin_prepared_input_changed')
        plan = current[0]
        return {'chosen': deepcopy(plan['chosen']), 'profile': deepcopy(plan['profile'])}

    def validate_material(self, owner, reader, material):
        state = _INPUTS.get(self._token)
        if state is None:
            raise RecognitionConflict('material_origin_prepared_input_expired')
        query = state['query']
        self._state(query)
        if owner.project_id != 'me' or material['type'] != 'recognition':
            return
        profile = state['frozen'][0]['profile']
        if not isinstance(profile.get('items'), list):
            raise RecognitionConflict('material_origin_model_profile_changed')
        rendered = PREFIX + ''.join('- ' + item['content'] + '\n' for item in profile['items']) if profile['items'] else ''
        if (profile.get('text') != rendered or type(profile.get('count')) is not int
                or profile['count'] != len(profile['items'])):
            raise RecognitionConflict('material_origin_model_profile_changed')
        actual_owner = query.origin_admission.destination.records
        service = recognition_service(TransactionRecords(reader), source_owner=actual_owner)
        for item in profile['items']:
            if item['id'] != material['id']:
                continue
            scope = WorkScope('local-user', 'me')
            current = service._qualified_recognition(reader,
                service._require_scope(reader, 'recognitions', item['id'], scope))
            if (current is None or not current.authorized or current.revision != item['revision']
                    or material['type'] != 'recognition' or material['revision'] != item['revision']
                    or material['namespace'] != query.origin_admission.personal['namespace']
                    or item['content'] != format_recognition_content(current.retrieval_projection(), profile=True)):
                raise RecognitionConflict('material_origin_model_profile_changed')
            snapshot = SourceEgressService(actual_owner, reader=reader).snapshot(scope,
                [{'type': 'recognition', 'id': item['id'], 'revision': item['revision']}])
            if snapshot != item['snapshot']:
                raise RecognitionConflict('material_origin_model_profile_changed')


_register_primary_type(PreparedMaterialInput)


@contextmanager
def prepared_primary_input(query, request, logical_invocation, materials, *, plan=None):
    _current_owner(query, request)
    frozen = frozen_primary_plan(query, request, logical_invocation, plan=plan)
    origins = query.origin_admission.restore(request)
    with _prepared(query, request, logical_invocation, materials, frozen, origins) as prepared:
        yield prepared


def _gap_body(plan):
    from .agentic_rag import _candidates, _validate_steps
    _validate_steps(plan)
    return {'chosen': deepcopy(_candidates(plan)), 'profile': deepcopy(plan['profile'])}


@contextmanager
def prepared_gap_input(query, request, materials, *, plan):
    _current_owner(query, request)
    if plan['project_id'] != request['scope']['project_id'] or plan['question'] != request['input']['text']:
        raise RecognitionConflict('material_origin_prepared_input_changed')
    origins = query.origin_admission.restore(request)
    frozen = (_gap_body(plan), None, None)
    with _prepared(query, request, 'rag-decision-' + str(plan['_rag']['round']), materials,
            frozen, origins, rag_plan=plan) as prepared:
        yield prepared


@contextmanager
def prepared_gap_proof(query, request, materials, *, proof):
    _current_owner(query, request)
    origins = query.origin_admission.restore(request)
    frozen = ({'chosen': decode(proof['candidates']), 'profile': decode(proof['profile'])}, None, None)
    with _prepared(query, request, 'rag-proof', materials, frozen, origins, gap_proof=proof) as prepared:
        yield prepared


@contextmanager
def prepared_historical_input(query, request, public_id, revision, input_ref):
    from .shared_history import read_model_history_answer
    _current_owner(query, request)
    actual = read_model_history_answer(query, public_id, revision)
    selected = [row for row in actual['model_inputs'] if row['input_ref'] == input_ref]
    if len(selected) != 1:
        raise RecognitionConflict('material_origin_history_input_changed')
    value = selected[0]
    rag = value['payload']['purpose'] == 'aux' and value['kind'].startswith('answer-gap-input-')
    if value['payload']['purpose'] != 'primary' and not rag:
        raise RecognitionConflict('material_origin_history_input_changed')
    selection = value['payload']['material_proof']['selection']
    frozen = (({'chosen': decode(selection['candidates']), 'profile': decode(selection['profile'])}, None, None)
        if rag else frozen_primary_plan(query, actual['facts']['request'], value['logical'], store=actual['snapshot']))
    origins = query.origin_admission.restore(request)
    materials = value['payload']['material_proof']['selection']['origin_materials']
    historical = {'id': public_id, 'revision': revision, 'input_ref': input_ref, 'input': value['payload']}
    with _prepared(query, request, value['logical'], materials, frozen, origins,
            historical=historical, rag_historical=rag) as prepared:
        yield prepared


@contextmanager
def _prepared(query, request, logical_invocation, materials, frozen, origins, *, historical=None,
              rag_plan=None, rag_historical=False, gap_proof=None):
    prepared = PreparedMaterialInput()
    _INPUTS[prepared._token] = {'object': prepared, 'query': query, 'thread': get_ident(),
        'request': deepcopy(request), 'logical': logical_invocation, 'reference': deepcopy(origins.reference),
        'materials': deepcopy(materials), 'frozen': deepcopy(frozen), 'historical': deepcopy(historical),
        'rag_plan': rag_plan, 'rag_historical': rag_historical, 'gap_proof': deepcopy(gap_proof)}
    token = _CURRENT.set(prepared._token)
    try:
        with prepared_primary_scope(prepared):
            yield prepared
    finally:
        _INPUTS.pop(prepared._token, None)
        _CURRENT.reset(token)
