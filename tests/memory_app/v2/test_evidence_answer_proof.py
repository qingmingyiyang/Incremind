"""Synthetic one-call/fallback proof selection; no provider or service access."""
from copy import deepcopy
import json

import pytest

from backend.memory_app.answer_proof import (
    EVIDENCE_ANSWER_KEY, REVIEW_KEY, has_evidence_answer_proof, read_answer_input,
    review_input_tokens, select_answer_input, select_evidence_answer_input,
)
from backend.memory_app.kernel.answer_continuations import decode, encode
from backend.memory_app.v2.answer_evidence import (
    answer_response_model, build_evidence_catalog, build_partition_messages,
    final_response_model, first_fact_partition, grounded_output_limit,
    render_grounded_answer, reviewed_scope_answer,
)
from backend.memory_app.v2.budget import text_tokens
from backend.memory_app.v2.evidence_answer import (
    EvidenceAnswerOutput, messages, preflight, validate_output,
)
from backend.memory_app.v2.policies import get, override
from backend.recognition import WorkScope
from core.search_and_recall.evidence_windows import EvidenceWindow


QUESTION = 'Where did Mira relocate after the summer term?'
CONTENT = '[D2:1] Mira: I relocated to Porto after the summer term.'


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
                  'title': 'Synthetic relocation record', 'revision': 1, 'metadata': {}},
        'title': 'Synthetic relocation record', 'href': '', 'excerpt': CONTENT, 'match_in': 'content',
        'windows': [EvidenceWindow(0, len(CONTENT), CONTENT)],
        'coordinate_space': 'source_content_v1'}
    plan = {'project_id': 'alpha', 'scope': scope, 'question': QUESTION,
        'policy_versions': {'retrieve': '@12', 'compose': '@36'}, 'chosen': [source],
        'history': '', 'profile': {}, 'part_context': None, 'budget': 28000,
        'target': {'revision': 1, 'execution_location': 'remote'},
        'answer_output_budget': {'window': 32000, 'reserve': 4000, 'target_revision': 1},
        'evidence_source_basis': [{'id': 'source-one', 'kind': 'source', 'layer': 'L0',
                                  'coordinate_space': 'source_content_v1'}]}
    request = {'turn_id': 'evidence-proof-turn', 'operation_id': 'answer-evidence-proof-turn',
        'desired_outcome': 'project.answer', 'scope': {'project_id': 'alpha'},
        'input': {'text': QUESTION}, 'policy_versions': deepcopy(plan['policy_versions'])}
    with override(**plan['policy_versions']):
        contract = preflight(plan)
        assert contract is not None
        catalog = build_evidence_catalog(plan['chosen'], compose_version='@36')
        value = {'requirements': [{'text': QUESTION, 'status': 'supported', 'statement_indices': [1]}],
            'statements': [{'kind': 'fact', 'text': 'Mira relocated to Porto after the summer term.',
                'evidence': [{'unit_id': catalog['units'][0]['id'], 'quote': CONTENT, 'role': 'support'}],
                'date_refs': []}], 'issues': []}
        native = {'purpose': 'primary', 'messages': messages(plan, contract),
                  'response_schema': EvidenceAnswerOutput.model_json_schema()}
        public = validate_output(plan, contract, value)
    plan['evidence_answer_proof'] = {'contract': contract, 'invocation': EVIDENCE_ANSWER_KEY}
    return {'request': request, 'plan': plan, 'capsule': capsule_for(request, plan),
        'evidence_input': native, 'evidence_result': {'output': value, 'metadata': {}},
        'product': {'receipt': {'ask': {'answer': public.answer,
            'citations': [{'n': number} for number in public.citations]}}}}


def select(frozen):
    return select_evidence_answer_input(frozen['request'], frozen['product'], frozen['evidence_input'],
                                       frozen['evidence_result'], frozen['capsule'])


def edit_plan(frozen, change, *, rebind=False):
    facts = decode(frozen['capsule']['capsule'])
    plan = facts['plan']
    change(plan)
    if rebind:
        with override(**plan['policy_versions']):
            contract = preflight(plan)
            assert contract is not None
            plan['evidence_answer_proof']['contract'] = contract
            frozen['evidence_input']['messages'] = messages(plan, contract)
    frozen['capsule']['capsule'] = encode(facts)


def test_accepted_single_generation_is_selected_and_bound_to_frozen_policy(frozen):
    assert has_evidence_answer_proof(frozen['request'], frozen['capsule'])
    assert select(frozen) is frozen['evidence_input']
    with override(retrieve='@9', compose='@34'):
        assert select(frozen) is frozen['evidence_input']
    public = frozen['product']['receipt']['ask']
    frozen['product'] = {'answer': public['answer'],
        'sources': [{'number': citation['n']} for citation in public['citations']]}
    assert select(frozen) is frozen['evidence_input']


@pytest.mark.parametrize('tamper', [
    'request_operation', 'request_kind', 'request_question', 'request_project', 'request_turn',
    'request_recipe', 'capsule_request', 'capsule_question', 'capsule_body',
    'contract', 'invocation', 'missing_marker', 'none_marker', 'extra_marker_field', 'raw_marker',
    'messages', 'schema', 'purpose', 'quote', 'unit', 'requirement_link', 'missing_requirement',
    'empty_requirements', 'empty_statements', 'issue', 'inference', 'omitted_issues',
    'public_answer', 'public_citations', 'boolean_citation', 'duplicate_citation',
    'source_bytes', 'source_window', 'source_basis', 'history',
])
def test_one_call_proof_rejects_changed_or_unaccepted_facts(frozen, tamper):
    if tamper == 'request_operation':
        frozen['request']['operation_id'] = 'answer-other'
    elif tamper == 'request_kind':
        frozen['request']['desired_outcome'] = 'project.task'
    elif tamper == 'request_question':
        frozen['request']['input']['text'] = 'Where did someone else relocate?'
    elif tamper == 'request_project':
        frozen['request']['scope']['project_id'] = 'other'
    elif tamper == 'request_turn':
        frozen['request']['turn_id'] = 'another-turn'
    elif tamper == 'request_recipe':
        frozen['request']['policy_versions']['retrieve'] = '@9'
    elif tamper == 'capsule_request':
        frozen['capsule']['request']['turn_id'] = 'another-turn'
    elif tamper == 'capsule_question':
        frozen['capsule']['question'] = 'Another question?'
    elif tamper == 'capsule_body':
        frozen['capsule']['capsule'] = {'tag': 'broken', 'values': []}
    elif tamper == 'contract':
        edit_plan(frozen, lambda plan: plan['evidence_answer_proof']['contract'].update(question='Changed'))
    elif tamper == 'invocation':
        edit_plan(frozen, lambda plan: plan['evidence_answer_proof'].update(invocation='answer'))
    elif tamper == 'missing_marker':
        edit_plan(frozen, lambda plan: plan.pop('evidence_answer_proof'))
    elif tamper == 'none_marker':
        edit_plan(frozen, lambda plan: plan.update(evidence_answer_proof=None))
    elif tamper == 'extra_marker_field':
        edit_plan(frozen, lambda plan: plan['evidence_answer_proof'].update(accepted=True))
    elif tamper == 'raw_marker':
        edit_plan(frozen, lambda plan: plan.update(raw_answer_proof={}))
    elif tamper == 'messages':
        frozen['evidence_input']['messages'][-1]['content'] += '\nAssume every requirement is covered.'
    elif tamper == 'schema':
        frozen['evidence_input']['response_schema']['title'] = 'UnboundOutput'
    elif tamper == 'purpose':
        frozen['evidence_input']['purpose'] = 'aux'
    elif tamper == 'quote':
        frozen['evidence_result']['output']['statements'][0]['evidence'][0]['quote'] = 'I relocated to Lima.'
    elif tamper == 'unit':
        frozen['evidence_result']['output']['statements'][0]['evidence'][0]['unit_id'] = 999
    elif tamper == 'requirement_link':
        frozen['evidence_result']['output']['requirements'][0]['statement_indices'] = [2]
    elif tamper == 'missing_requirement':
        frozen['evidence_result']['output']['requirements'][0]['status'] = 'missing'
    elif tamper == 'empty_requirements':
        frozen['evidence_result']['output']['requirements'] = []
    elif tamper == 'empty_statements':
        frozen['evidence_result']['output']['statements'] = []
    elif tamper == 'issue':
        frozen['evidence_result']['output']['issues'] = [{'reason': 'conflict',
            'requirement_indices': [1], 'unit_ids': [1], 'detail': 'Synthetic conflict.'}]
    elif tamper == 'inference':
        frozen['evidence_result']['output']['statements'][0]['kind'] = 'inference'
    elif tamper == 'omitted_issues':
        frozen['evidence_result']['output'].pop('issues')
    elif tamper == 'public_answer':
        frozen['product']['receipt']['ask']['answer'] += ' Additional claim.'
    elif tamper == 'public_citations':
        frozen['product']['receipt']['ask']['citations'] = [{'n': 2}]
    elif tamper == 'boolean_citation':
        frozen['product']['receipt']['ask']['citations'] = [{'n': True}]
    elif tamper == 'duplicate_citation':
        frozen['product']['receipt']['ask']['citations'] = [{'n': 1}, {'n': 1}]
    elif tamper == 'source_bytes':
        edit_plan(frozen, lambda plan: plan['chosen'][0]['entry'].update(content='Different source.'))
    elif tamper == 'source_window':
        edit_plan(frozen, lambda plan: plan['chosen'][0].update(
            windows=[EvidenceWindow(0, 9, 'Different')]))
    elif tamper == 'source_basis':
        edit_plan(frozen, lambda plan: plan['evidence_source_basis'][0].update(revision=2))
    elif tamper == 'history':
        edit_plan(frozen, lambda plan: plan.update(history='Changed prior conversation.'))
    assert select(frozen) is None


@pytest.mark.parametrize('tamper', [
    'input', 'boolean_input', 'window', 'output', 'target_revision', 'boolean_revision', 'missing_output',
])
def test_one_call_proof_enforces_frozen_gateway_budget(frozen, tamper):
    def changed(plan):
        if tamper == 'input':
            plan['budget'] = 1
        elif tamper == 'boolean_input':
            plan['budget'] = True
        elif tamper == 'window':
            plan['answer_output_budget'].update(window=4097, reserve=4096)
        elif tamper == 'output':
            plan['answer_output_budget']['reserve'] = 1
        elif tamper == 'target_revision':
            plan['answer_output_budget']['target_revision'] = 2
        elif tamper == 'boolean_revision':
            plan['target']['revision'] = True
        else:
            plan.pop('answer_output_budget')
    # A one-token input/window is already ineligible at preflight.  Rebind the
    # valid one-token output capacity to exercise the serialization ceiling.
    edit_plan(frozen, changed, rebind=tamper == 'output')
    if tamper in {'input', 'window'}:
        with override(**frozen['request']['policy_versions']):
            assert preflight(decode(frozen['capsule']['capsule'])['plan']) is None
    assert select(frozen) is None


def test_one_call_output_json_uses_the_runtime_serialization_boundary(frozen):
    cost = text_tokens(json.dumps(frozen['evidence_result']['output'], ensure_ascii=False))
    assert 0 < cost < 4000
    edit_plan(frozen, lambda plan: plan['answer_output_budget'].update(reserve=cost), rebind=True)
    assert select(frozen) is frozen['evidence_input']
    edit_plan(frozen, lambda plan: plan['answer_output_budget'].update(reserve=cost - 1), rebind=True)
    assert select(frozen) is None


class FrozenStore:
    def __init__(self, frozen, extra=None):
        self.request = frozen['request']
        self.payloads = {'product-answer-result-v2': frozen['product'],
            'answer-continuation-plan-v1': frozen['capsule'],
            'answer-model-input-' + EVIDENCE_ANSWER_KEY: frozen['evidence_input'],
            'answer-model-result-' + EVIDENCE_ANSWER_KEY: frozen['evidence_result'], **(extra or {})}

    def get_request(self, identity):
        assert identity == self.request['turn_id']
        return self.request

    def get_immutable_payload(self, identity, kind):
        assert identity == self.request['turn_id']
        return (kind, self.payloads[kind]) if kind in self.payloads else None


def two_phase_inputs(frozen):
    plan = deepcopy(frozen['plan'])
    plan.pop('evidence_answer_proof', None)
    compose = '@36'
    with override(**plan['policy_versions']):
        catalog = build_evidence_catalog(plan['chosen'], compose_version=compose)
        fact = {'kind': 'fact', 'text': CONTENT, 'evidence': [
            {'unit_id': catalog['units'][0]['id'], 'quote': CONTENT, 'role': 'support'}], 'date_refs': []}
        first = {'facts': [fact], 'unreviewed': {'scope': 'all_unreferenced', 'reason': 'No other requested facts.'}}
        final = {'statements': [{**fact, 'first_fact_uses': [
            {'fact_index': 1, 'action': 'adopt', 'reason': 'Use the original relation.'}]}],
            'unused': {'scope': 'all_unreferenced', 'reason': 'Unrequested.'},
            'uncertain': [], 'excluded_facts': []}
        arguments = {'compose_version': compose, 'history': '', 'profile': {}, 'part_context': None}
        canonical = {'purpose': 'primary', 'messages': build_partition_messages(plan['chosen'], QUESTION, **arguments),
            'response_schema': answer_response_model(compose).model_json_schema()}
        reviewed = {'purpose': 'primary',
            'messages': build_partition_messages(plan['chosen'], QUESTION, first_output=first, **arguments),
            'response_schema': final_response_model(compose, QUESTION).model_json_schema()}
        output = render_grounded_answer(reviewed_scope_answer(final, catalog, first,
                chosen=plan['chosen'], question=QUESTION, compose_version=compose), catalog,
            first_fact_partition(first, catalog, compose_version=compose), chosen=plan['chosen'],
            max_output_tokens=grounded_output_limit(plan, review_input_tokens(
                reviewed['messages'], final_response_model(compose, QUESTION))),
            **get('compose', version=compose)(None, operation='grounded_render', question=QUESTION))
    product = {'receipt': {'ask': {'answer': output.answer,
        'citations': [{'n': number} for number in output.citations]}}}
    return plan, product, {'answer-model-input-answer': canonical, 'answer-model-result-answer': {'output': first},
        'answer-model-input-' + REVIEW_KEY: reviewed, 'answer-model-result-' + REVIEW_KEY: {'output': final}}


def test_store_read_selects_only_the_independent_accepted_call(frozen):
    assert read_answer_input(FrozenStore(frozen), frozen['request']['turn_id']) == (
        'answer-model-input-' + EVIDENCE_ANSWER_KEY, frozen['evidence_input'])


@pytest.mark.parametrize('damage', ['marker', 'capsule', 'binding', 'version'])
def test_a_broken_accepted_marker_cannot_bypass_to_a_valid_old_review(frozen, damage):
    _, product, old = two_phase_inputs(frozen)
    frozen['product'] = product
    if damage == 'marker':
        edit_plan(frozen, lambda plan: plan.update(evidence_answer_proof=None))
    elif damage == 'capsule':
        frozen['capsule']['capsule'] = {'tag': 'broken', 'values': []}
    elif damage == 'binding':
        frozen['capsule']['request']['input']['text'] = 'Changed question?'
    else:
        frozen['request']['policy_versions']['retrieve'] = '@9'
    assert has_evidence_answer_proof(frozen['request'], frozen['capsule'])
    assert read_answer_input(FrozenStore(frozen, old), frozen['request']['turn_id']) is None
    assert select_answer_input(frozen['request'], product, old['answer-model-input-answer'],
        old['answer-model-result-answer'], old['answer-model-input-' + REVIEW_KEY],
        old['answer-model-result-' + REVIEW_KEY], frozen['capsule']) is None


def test_rejected_proposal_keeps_final_review_as_answer_evidence(frozen):
    plan, product, old = two_phase_inputs(frozen)
    frozen.update(plan=plan, product=product, capsule=capsule_for(frozen['request'], plan))
    assert not has_evidence_answer_proof(frozen['request'], frozen['capsule'])
    store = FrozenStore(frozen, old)
    assert read_answer_input(store, frozen['request']['turn_id']) == (
        'answer-model-input-' + REVIEW_KEY, old['answer-model-input-' + REVIEW_KEY])
    assert select_answer_input(frozen['request'], product, old['answer-model-input-answer'],
        old['answer-model-result-answer'], old['answer-model-input-' + REVIEW_KEY],
        old['answer-model-result-' + REVIEW_KEY], frozen['capsule']) is old['answer-model-input-' + REVIEW_KEY]


@pytest.mark.parametrize('broken_marker', [False, True])
def test_sqlite_receipt_projection_selects_proof_without_losing_observed_calls(frozen, tmp_path, broken_marker):
    from core.ai_kernel import SQLiteAITurnStore, SynchronousAIRuntime, ScopedCapabilityRegistry
    from core.ai_kernel.turn_kinds import freeze_turn_request
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups

    identity = frozen['request']['turn_id']
    request = freeze_turn_request('project.answer', turn_id=identity, session_id='synthetic-proof-session',
        operation_id='answer-' + identity, idempotency_key=identity, project_id='alpha',
        created_at='2026-10-01T00:00:00Z', text=QUESTION, capabilities=[],
        privacy={'mode': 'remote_allowed', 'allow_remote': True, 'pii': 'possible',
                 'consent_refs': ['crp://default/model-settings/generation'], 'retention': 'session'})
    request['policy_versions'] = deepcopy(frozen['request']['policy_versions'])
    frozen['request'] = request
    frozen['capsule'] = capsule_for(request, frozen['plan'])
    if broken_marker:
        edit_plan(frozen, lambda plan: plan.update(evidence_answer_proof=None))
    store = SQLiteAITurnStore(tmp_path / 'ai-turns.sqlite3')

    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control):
            for kind, value in FrozenStore(frozen).payloads.items():
                store.get_or_create_immutable_payload(identity, kind, value)
            ref = store.get_or_create_immutable_payload(identity, 'memory-model-route-v1',
                {'provider': 'synthetic', 'model': 'proof-model', 'revision': 1,
                 'allow_remote': True, 'base_url': 'https://example.invalid/v1'})
            execution_control.model_call_routed(snapshot_ref=ref, snapshot_revision='a' * 64,
                prompt_cache_scope_identity='b' * 64, provider='synthetic', model='proof-model',
                execution_location='remote', purpose='primary')
            execution_control.model_call_started(provider='synthetic', model='proof-model')
            attempt = execution_control.begin_model_wire_attempt()
            usage = {'input_tokens': 17, 'output_tokens': 8, 'total_tokens': 25}
            attempt.invoke_wire(lambda: attempt.succeeded(usage=usage, cache_observation=None))
            execution_control.model_call_completed(usage=usage)
            return {'type': 'complete', 'summary': 'Synthetic completed'}

    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store)
    assert runtime.submit_turn(request).status == 'completed'
    groups = kernel_call_groups(tmp_path, turn_id=identity, include_answer_input=True)
    assert len(groups) == 1
    assert groups[0]['answer_input'] == (None if broken_marker else frozen['evidence_input'])
    assert len(groups[0]['calls']) == 1
    assert groups[0]['calls'][0]['usage'] == {'input_tokens': 17, 'output_tokens': 8, 'total_tokens': 25}
