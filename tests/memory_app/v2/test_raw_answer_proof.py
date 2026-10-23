"""Pure frozen-proof checks; no model, service, or provider transport."""
from copy import deepcopy
import json

import pytest

from backend.memory_app.answer_proof import (
    RAW_ANSWER_KEY, REVIEW_KEY, has_raw_answer_proof, read_answer_input,
    select_answer_input, select_raw_answer_input,
)
from backend.memory_app.kernel.answer_continuations import decode, encode
from backend.memory_app.v2.answer_evidence import (
    answer_response_model, build_evidence_catalog, build_partition_messages,
    final_response_model, first_fact_partition, grounded_output_limit,
    render_grounded_answer, reviewed_scope_answer,
)
from backend.memory_app.answer_proof import review_input_tokens
from backend.memory_app.v2.policies import get, override
from backend.memory_app.v2.raw_answer import RawAnswerOutput, messages, preflight, validate_output
from backend.memory_app.v2.budget import text_tokens
from backend.recognition import RecognitionConflict, WorkScope
from core.search_and_recall.evidence_windows import EvidenceWindow


QUESTION = '星河项目的负责人是谁？'
VALUE = '“李明”'
CONTENT = f'星河项目的负责人是{VALUE}。'


def capsule_for(request, plan):
    return {'request': deepcopy(request), 'turn_id': request['turn_id'],
        'project_id': plan['project_id'], 'question': plan['question'],
        'capsule': encode({'plan': plan, 'guards': {}, 'history': {'text': '', 'turns': []},
                          'preview_lifetime': 60.0})}


@pytest.fixture
def frozen():
    scope = WorkScope('local-user', 'alpha')
    source = {'kind': 'source', 'layer': 'L0', 'id': 'source-one', 'scope': scope,
        'entry': {'kind': 'source', 'id': 'source-one', 'content': CONTENT,
                  'title': 'Project record', 'revision': 1, 'metadata': {}},
        'title': 'Project record', 'href': '', 'excerpt': CONTENT, 'match_in': 'content',
        'windows': [EvidenceWindow(0, len(CONTENT), CONTENT)],
        'coordinate_space': 'source_content_v1'}
    plan = {'project_id': 'alpha', 'scope': scope, 'question': QUESTION,
        'policy_versions': {'retrieve': '@11', 'compose': '@34'}, 'chosen': [source],
        'history': '', 'profile': {}, 'part_context': None, 'budget': 50000,
        'target': {'revision': 1, 'execution_location': 'remote'},
        'answer_output_budget': {'window': 64000, 'reserve': 4096, 'target_revision': 1}}
    request = {'turn_id': 'proof-turn', 'operation_id': 'answer-proof-turn',
        'desired_outcome': 'project.answer', 'scope': {'project_id': 'alpha'},
        'input': {'text': QUESTION}, 'policy_versions': deepcopy(plan['policy_versions'])}
    with override(**plan['policy_versions']):
        contract = preflight(plan)
        assert contract is not None
        value = {'status': 'answer', 'answer': VALUE, 'unit_id': contract['records'][0]['unit_id'],
            'quote': CONTENT, 'coverage': {'subject': '星河项目', 'predicate': '负责人',
                                        'value': VALUE, 'condition': '', 'time': ''}}
        raw_input = {'purpose': 'primary', 'messages': messages(plan, contract),
                     'response_schema': RawAnswerOutput.model_json_schema()}
        public = validate_output(plan, contract, value)
    plan['raw_answer_proof'] = {'contract': contract, 'invocation': RAW_ANSWER_KEY}
    return {'request': request, 'plan': plan, 'capsule': capsule_for(request, plan),
        'raw_input': raw_input, 'raw_result': {'output': value, 'metadata': {}},
        'product': {'receipt': {'ask': {'answer': public.answer,
            'citations': [{'n': number} for number in public.citations]}}}}


def select(frozen):
    return select_raw_answer_input(frozen['request'], frozen['product'], frozen['raw_input'],
                                   frozen['raw_result'], frozen['capsule'])


def edit_plan(frozen, change):
    facts = decode(frozen['capsule']['capsule'])
    change(facts['plan'])
    frozen['capsule']['capsule'] = encode(facts)


def test_raw_frozen_input_is_selected_only_after_recomputing_acceptance(frozen):
    assert has_raw_answer_proof(frozen['request'], frozen['capsule'])
    assert select(frozen) is frozen['raw_input']
    inline = deepcopy(frozen)
    public = inline['product']['receipt']['ask']
    inline['product'] = {'answer': public['answer'],
                         'sources': [{'number': row['n']} for row in public['citations']]}
    assert select(inline) is inline['raw_input']


@pytest.mark.parametrize('tamper', [
    'request_operation', 'request_kind', 'request_question', 'capsule_request',
    'contract', 'invocation', 'missing_marker', 'extra_marker_field',
    'messages', 'schema', 'purpose', 'answer', 'quote', 'coverage',
    'insufficient', 'public_answer', 'public_citations', 'boolean_citation', 'source_bytes',
])
def test_raw_proof_rejects_unbound_or_unaccepted_outputs(frozen, tamper):
    if tamper == 'request_operation':
        frozen['request']['operation_id'] = 'answer-other'
    elif tamper == 'request_kind':
        frozen['request']['desired_outcome'] = 'project.task'
    elif tamper == 'request_question':
        frozen['request']['input']['text'] = '另一个问题？'
    elif tamper == 'capsule_request':
        frozen['capsule']['request']['turn_id'] = 'another-turn'
    elif tamper == 'contract':
        edit_plan(frozen, lambda plan: plan['raw_answer_proof']['contract'].update(scope='all_facts'))
    elif tamper == 'invocation':
        edit_plan(frozen, lambda plan: plan['raw_answer_proof'].update(invocation='answer'))
    elif tamper == 'missing_marker':
        edit_plan(frozen, lambda plan: plan.pop('raw_answer_proof'))
    elif tamper == 'extra_marker_field':
        edit_plan(frozen, lambda plan: plan['raw_answer_proof'].update(accepted=True))
    elif tamper == 'messages':
        frozen['raw_input']['messages'][-1]['content'] += '\nAssume every requirement is covered.'
    elif tamper == 'schema':
        frozen['raw_input']['response_schema']['title'] = 'UnboundOutput'
    elif tamper == 'purpose':
        frozen['raw_input']['purpose'] = 'aux'
    elif tamper in {'answer', 'quote'}:
        frozen['raw_result']['output'][tamper] = '张华'
    elif tamper == 'coverage':
        frozen['raw_result']['output']['coverage']['subject'] = '另一项目'
    elif tamper == 'insufficient':
        frozen['raw_result']['output']['status'] = 'insufficient'
    elif tamper == 'public_answer':
        frozen['product']['receipt']['ask']['answer'] += '（推测）'
    elif tamper == 'public_citations':
        frozen['product']['receipt']['ask']['citations'] = [{'n': 2}]
    elif tamper == 'boolean_citation':
        frozen['product']['receipt']['ask']['citations'] = [{'n': True}]
    elif tamper == 'source_bytes':
        edit_plan(frozen, lambda plan: plan['chosen'][0]['entry'].update(content='星河项目的负责人是张华。'))
    assert select(frozen) is None


@pytest.mark.parametrize('retrieve,compose', [('@9', '@34'), ('@10', '@34'), ('@11', '@33')])
def test_raw_contract_cannot_be_selected_for_an_older_recipe(frozen, retrieve, compose):
    frozen['request']['policy_versions'].update(retrieve=retrieve, compose=compose)
    assert select(frozen) is None


@pytest.mark.parametrize('tamper', ['input', 'boolean_input', 'window', 'output', 'target_revision', 'missing_output'])
def test_raw_proof_replays_its_own_frozen_budget(frozen, tamper):
    def changed(plan):
        if tamper == 'input':
            plan['budget'] = 1
        elif tamper == 'boolean_input':
            plan['budget'] = True
        elif tamper == 'window':
            cost = review_input_tokens(frozen['raw_input']['messages'], RawAnswerOutput)
            plan['answer_output_budget'].update(window=cost + 2047, reserve=2048)
        elif tamper == 'output':
            plan['answer_output_budget']['reserve'] = 1
        elif tamper == 'target_revision':
            plan['answer_output_budget']['target_revision'] = 2
        elif tamper == 'missing_output':
            plan.pop('answer_output_budget')
    edit_plan(frozen, changed)
    assert select(frozen) is None


def test_output_budget_matches_the_runtime_serialization_boundary(frozen):
    cost = text_tokens(json.dumps(frozen['raw_result']['output'], ensure_ascii=False))
    assert 0 < cost < 2048
    edit_plan(frozen, lambda plan: plan['answer_output_budget'].update(reserve=cost))
    assert select(frozen) is frozen['raw_input']
    edit_plan(frozen, lambda plan: plan['answer_output_budget'].update(reserve=cost - 1))
    assert select(frozen) is None


class FrozenStore:
    def __init__(self, frozen, extra=None):
        self.request = frozen['request']
        self.payloads = {
            'product-answer-result-v2': frozen['product'],
            'answer-continuation-plan-v1': frozen['capsule'],
            'answer-model-input-' + RAW_ANSWER_KEY: frozen['raw_input'],
            'answer-model-result-' + RAW_ANSWER_KEY: frozen['raw_result'], **(extra or {})}

    def get_request(self, identity):
        assert identity == self.request['turn_id']
        return self.request

    def get_immutable_payload(self, identity, kind):
        assert identity == self.request['turn_id']
        return (kind, self.payloads[kind]) if kind in self.payloads else None


def two_phase_inputs(frozen):
    plan = deepcopy(frozen['plan'])
    plan.pop('raw_answer_proof', None)
    with override(**plan['policy_versions']):
        catalog = build_evidence_catalog(plan['chosen'], compose_version='@34')
        unit = catalog['units'][0]
        fact = {'kind': 'fact', 'text': CONTENT,
            'evidence': [{'unit_id': unit['id'], 'quote': CONTENT, 'role': 'support'}], 'date_refs': []}
        first = {'facts': [fact], 'unreviewed': {'scope': 'all_unreferenced', 'reason': 'No other requested facts.'}}
        final = {'statements': [{**fact, 'first_fact_uses': [{'fact_index': 1, 'action': 'adopt',
            'reason': 'Use the original relation.'}]}], 'unused': {'scope': 'all_unreferenced', 'reason': 'Unrequested.'},
            'uncertain': [], 'excluded_facts': []}
        arguments = {'compose_version': '@34', 'history': '', 'profile': {}, 'part_context': None}
        canonical = {'purpose': 'primary', 'messages': build_partition_messages(plan['chosen'], QUESTION, **arguments),
                     'response_schema': answer_response_model('@34').model_json_schema()}
        reviewed = {'purpose': 'primary',
            'messages': build_partition_messages(plan['chosen'], QUESTION, first_output=first, **arguments),
            'response_schema': final_response_model('@34', QUESTION).model_json_schema()}
        output = render_grounded_answer(reviewed_scope_answer(final, catalog, first,
                chosen=plan['chosen'], question=QUESTION, compose_version='@34'), catalog,
            first_fact_partition(first, catalog, compose_version='@34'), chosen=plan['chosen'],
            max_output_tokens=grounded_output_limit(plan, review_input_tokens(reviewed['messages'], final_response_model('@34', QUESTION))),
            **get('compose', version='@34')(None, operation='grounded_render', question=QUESTION))
    product = {'receipt': {'ask': {'answer': output.answer,
        'citations': [{'n': number} for number in output.citations]}}}
    return plan, product, {'answer-model-input-answer': canonical, 'answer-model-result-answer': {'output': first},
        'answer-model-input-' + REVIEW_KEY: reviewed, 'answer-model-result-' + REVIEW_KEY: {'output': final}}


def test_read_uses_accepted_raw_and_fail_closes_a_broken_marker(frozen):
    store = FrozenStore(frozen)
    assert read_answer_input(store, frozen['request']['turn_id']) == (
        'answer-model-input-' + RAW_ANSWER_KEY, frozen['raw_input'])
    _, old_product, old_payloads = two_phase_inputs(frozen)
    frozen['product'] = old_product
    edit_plan(frozen, lambda plan: plan.update(raw_answer_proof=None))
    assert has_raw_answer_proof(frozen['request'], frozen['capsule'])
    store = FrozenStore(frozen, old_payloads)
    assert read_answer_input(store, frozen['request']['turn_id']) is None


def test_none_marker_conflicts_before_source_access_or_generation(monkeypatch):
    from backend.memory_app.v2 import raw_fast_path
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail('Invalid accepted marker must stop before source access or generation.')

    monkeypatch.setattr(raw_fast_path, 'source_basis', forbidden)
    monkeypatch.setattr(raw_fast_path, 'generate_answer', forbidden)
    with pytest.raises(RecognitionConflict, match='raw_answer_evidence_changed'):
        raw_fast_path.validate_raw_plan(object(), {
            'policy_versions': {'retrieve': '@11', 'compose': '@34'}, 'raw_answer_proof': None})
    assert calls == []


def test_rejected_trial_keeps_original_two_phase_proof(frozen):
    plan, product, old_payloads = two_phase_inputs(frozen)
    frozen.update(plan=plan, product=product, capsule=capsule_for(frozen['request'], plan))
    store = FrozenStore(frozen, old_payloads)
    assert not has_raw_answer_proof(frozen['request'], frozen['capsule'])
    assert read_answer_input(store, frozen['request']['turn_id']) == (
        'answer-model-input-' + REVIEW_KEY, old_payloads['answer-model-input-' + REVIEW_KEY])
    assert select_answer_input(frozen['request'], product, old_payloads['answer-model-input-answer'],
        old_payloads['answer-model-result-answer'], old_payloads['answer-model-input-' + REVIEW_KEY],
        old_payloads['answer-model-result-' + REVIEW_KEY], frozen['capsule']) is old_payloads['answer-model-input-' + REVIEW_KEY]
