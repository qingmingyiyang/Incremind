"""Pure proposal checks, with synthetic evidence and no model/owner calls."""
from copy import deepcopy
import hashlib
import json

import pytest
from pydantic import ValidationError

from core.search_and_recall.evidence_windows import EvidenceWindow
from backend.memory_app.v2.evidence_answer import (
    EvidenceAnswerOutput, followup_hints, messages, preflight, validate_output,
)


QUESTION = '林舟平时怎么去青岚工坊？'
BODY = '[D1:1] 林舟: 我平时乘地铁去青岚工坊。\n'
STATEMENT = '林舟平时乘地铁去青岚工坊。'


def plan_for(question=QUESTION, body=BODY, *, prefix='', suffix=''):
    content = prefix + body + suffix
    window = EvidenceWindow(len(prefix), len(prefix) + len(body), body)
    row = {'id': 'synthetic-source', 'kind': 'source', 'layer': 'L0',
           'title': 'Synthetic diary', 'coordinate_space': 'source_content_v1',
           'entry': {'content': content, 'revision': 1}, 'excerpt': body, 'windows': (window,)}
    # This fixture checks pure binding only.  Runtime integration separately
    # obtains the real basis through the source owner; this is not confirmation.
    basis = {key: row[key] for key in ('id', 'kind', 'layer', 'coordinate_space')}
    basis.update(scope={'user_id': 'local-user', 'project_id': 'synthetic-project'}, revision=1,
        snapshot={'type': 'synthetic_owner_snapshot'},
        content_sha256=hashlib.sha256(content.encode()).hexdigest())
    return {'question': question, 'history': '', 'profile': {}, 'chosen': [row],
            'evidence_source_basis': [basis], 'policy_versions': {'retrieve': '@12', 'compose': '@36'},
            'budget': 28000, 'target': {'revision': 1},
            'answer_output_budget': {'window': 32000, 'reserve': 4000, 'target_revision': 1}}


def proposal(contract, text=STATEMENT, *, unit=None):
    item = unit or contract['evidence_catalog']['units'][0]
    return {'requirements': [{'text': contract['question'], 'status': 'supported', 'statement_indices': [1]}],
            'statements': [{'kind': 'fact', 'text': text, 'evidence': [
                {'unit_id': item['id'], 'quote': item['text'], 'role': 'support'}], 'date_refs': []}],
            'issues': []}


@pytest.mark.parametrize('question,body,text', [
    (QUESTION, BODY, STATEMENT),
    ('How does Elin usually get to the Cedar studio?',
     '[D1:1] Elin: I normally cycle to the Cedar studio.\n', 'Elin normally cycles to the Cedar studio.'),
])
def test_ordinary_natural_questions_have_a_single_proposal_contract(question, body, text):
    plan = plan_for(question, body)
    before = deepcopy(plan)
    contract = preflight(plan)
    assert contract is not None and contract['question'] == question
    output = proposal(contract, text)
    public = validate_output(plan, contract, EvidenceAnswerOutput.model_validate(output))
    assert public.answer == text + ' [1]' and public.citations == [1]
    assert plan == before


def test_partial_l0_windows_and_dialogue_do_not_require_the_old_literal_gate():
    plan = plan_for(prefix='An earlier unselected passage.\n', suffix='An unselected later passage.\n')
    contract = preflight(plan)
    assert contract is not None
    assert contract['evidence_catalog']['units'][0]['span'][0] > 0
    assert validate_output(plan, contract, proposal(contract)).citations == [1]


def test_history_profile_and_original_question_are_frozen_as_context_not_evidence():
    plan = plan_for()
    plan.update(history='先前的问题提到青岚工坊。', profile={'text': '请简洁回答。'}, retrieval_question='青岚工坊 通勤')
    plan['evidence_source_basis'][0]['snapshot']['private_marker'] = 'OWNER_INTERNAL_NOT_MODEL_EVIDENCE'
    contract = preflight(plan)
    prompt = messages(plan, contract)
    payload = json.loads(prompt[-1]['content'])
    assert payload['evidence_answer_contract']['question'] == QUESTION
    assert payload['evidence_answer_contract']['history'] == plan['history']
    assert payload['evidence_answer_contract']['profile'] == plan['profile']
    assert 'OWNER_INTERNAL_NOT_MODEL_EVIDENCE' not in prompt[-1]['content']
    assert 'never factual evidence' in prompt[0]['content']
    assert 'coverage judgment can be wrong' in prompt[0]['content']
    assert set(payload) == {'evidence_answer_contract', 'evidence_catalog', 'date_provenance'}


@pytest.mark.parametrize('fault', ['version', 'compose', 'empty', 'basis', 'identity', 'opaque',
                                  'history', 'part_context', 'situation', 'budget', 'revision', 'window'])
def test_unavailable_native_context_source_or_budget_does_not_dispatch(fault):
    plan = plan_for()
    if fault == 'version':
        plan['policy_versions']['retrieve'] = '@11'
    elif fault == 'compose':
        plan['policy_versions']['compose'] = '@34'
    elif fault == 'empty':
        plan['chosen'] = []
    elif fault == 'basis':
        plan.pop('evidence_source_basis')
    elif fault == 'identity':
        plan['evidence_source_basis'][0]['id'] = 'another-source'
    elif fault == 'opaque':
        plan['profile']['opaque'] = object()
    elif fault == 'history':
        plan['history'] = {'not': 'the actual native history string'}
    elif fault in {'part_context', 'situation'}:
        plan[fault] = {'text': 'Unbound parent context.'}
    elif fault == 'budget':
        plan['budget'] = 1
    elif fault == 'revision':
        plan['target']['revision'] = 2
    else:
        plan['chosen'][0]['windows'] = (EvidenceWindow(0, len(BODY), 'x' * len(BODY)),)
    assert preflight(plan) is None


@pytest.mark.parametrize('field', ['question', 'history', 'profile', 'basis', 'budget', 'source'])
def test_mutated_frozen_input_is_rejected_before_render(field):
    plan = plan_for()
    contract = preflight(plan)
    output = proposal(contract)
    if field == 'question':
        plan['question'] += '还问其他事。'
    elif field == 'history':
        plan['history'] = 'Changed context.'
    elif field == 'profile':
        plan['profile']['text'] = 'Changed context.'
    elif field == 'basis':
        plan['evidence_source_basis'][0]['revision'] = 2
    elif field == 'budget':
        plan['answer_output_budget']['reserve'] -= 1
    else:
        plan['chosen'][0]['entry']['content'] = 'Changed source.'
    with pytest.raises(ValueError, match='contract_changed_or_unavailable'):
        validate_output(plan, contract, output)


@pytest.mark.parametrize('fault', ['missing', 'uncertain', 'unbound', 'out_of_range', 'duplicate_indices',
                                  'unlinked_statement', 'inference', 'limitation', 'no_statements'])
def test_local_direct_admission_requires_all_declared_requirements_and_statements(fault):
    plan = plan_for()
    contract = preflight(plan)
    output = proposal(contract)
    if fault in {'missing', 'uncertain'}:
        output['requirements'][0]['status'] = fault
    elif fault == 'unbound':
        output['requirements'][0]['statement_indices'] = []
    elif fault == 'out_of_range':
        output['requirements'][0]['statement_indices'] = [2]
    elif fault == 'duplicate_indices':
        output['requirements'][0]['statement_indices'] = [1, 1]
    elif fault == 'unlinked_statement':
        output['statements'].append(deepcopy(output['statements'][0]))
    elif fault in {'inference', 'limitation'}:
        output['statements'][0]['kind'] = fault
    else:
        output['statements'] = []
    with pytest.raises(ValueError, match='evidence_answer_'):
        validate_output(plan, contract, output)
    assert followup_hints(output)


@pytest.mark.parametrize('reason', ['missing', 'low_confidence', 'conflict', 'multi_hop', 'attribution', 'unresolved_time_scope'])
def test_any_reported_risk_falls_back_without_forwarding_candidate_text(reason):
    plan = plan_for()
    contract = preflight(plan)
    output = proposal(contract)
    marker = 'CANDIDATE_TEXT_MUST_NOT_ENTER_INVESTIGATION'
    output['requirements'][0]['text'] = marker
    output['statements'][0]['text'] = marker
    output['issues'] = [{'reason': reason, 'requirement_indices': [1], 'unit_ids': [1], 'detail': marker}]
    with pytest.raises(ValueError, match='risks_reported'):
        validate_output(plan, contract, output)
    hints = followup_hints(output)
    assert marker not in json.dumps(hints) and hints[0]['reason'] == reason
    assert not any(key in item for item in hints for key in ('text', 'detail', 'statements'))


@pytest.mark.parametrize('fault', ['quote', 'unit', 'context_only', 'manual_citation', 'unknown_ref'])
def test_source_and_reference_guard_rejects_locally_invalid_proposals(fault):
    plan = plan_for()
    contract = preflight(plan)
    output = proposal(contract)
    if fault == 'quote':
        output['statements'][0]['evidence'][0]['quote'] = 'An invented quote.'
    elif fault == 'unit':
        output['statements'][0]['evidence'][0]['unit_id'] = 999
    elif fault == 'context_only':
        output['statements'][0]['evidence'][0]['role'] = 'context'
    elif fault == 'manual_citation':
        output['statements'][0]['text'] += ' [99]'
    else:
        output['statements'][0]['date_refs'] = [999]
    with pytest.raises(ValueError):
        validate_output(plan, contract, output)


def test_calendar_v7_is_called_directly_without_first_or_review_fabrication():
    body = '[D1:1] Elin: I calibrated the Amber gauge two days ago.\n'
    plan = plan_for('When did Elin calibrate the Amber gauge?', body)
    plan['chosen'][0]['title'] = 'Synthetic record 2045-05-16'
    contract = preflight(plan)
    ref = contract['date_provenance'][0]
    result = ref['options'][0]['start']
    output = proposal(contract, f'Elin calibrated the gauge on {result} (two days before {ref["anchor"]}).')
    output['statements'][0].update(kind='date', date_refs=[ref['date_ref_id']])
    before = deepcopy(output)
    assert result in validate_output(plan, contract, output).answer
    assert output == before
    output['statements'][0]['kind'] = 'fact'
    with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
        validate_output(plan, contract, output)
    output['statements'][0].update(kind='date', text='Elin calibrated it on 2045-07-01.')
    with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
        validate_output(plan, contract, output)


def test_strict_schema_excludes_free_answer_review_fields_and_coerced_indices():
    plan = plan_for()
    output = proposal(preflight(plan))
    for key, value in [('answer', 'An unbound free answer.'), ('first_fact_uses', []), ('covered', True)]:
        with pytest.raises(ValidationError):
            EvidenceAnswerOutput.model_validate({**output, key: value})
    output['requirements'][0]['statement_indices'] = ['1']
    assert followup_hints(output) == [{'kind': 'structural', 'code': 'evidence_answer_output_invalid'}]
    with pytest.raises(ValidationError):
        EvidenceAnswerOutput.model_validate(output)


def test_multiple_requirements_can_reference_actual_same_source_statements():
    body = '[D1:1] Elin: I normally cycle.\n[D1:2] Elin: I leave at dawn.\n'
    plan = plan_for('How and when does Elin travel?', body)
    contract = preflight(plan)
    output = proposal(contract, 'Elin normally cycles.')
    output['requirements'][0]['text'] = 'How Elin travels.'
    output['requirements'].append({'text': 'When Elin leaves.', 'status': 'supported', 'statement_indices': [2]})
    unit = contract['evidence_catalog']['units'][1]
    output['statements'].append({'kind': 'fact', 'text': 'Elin leaves at dawn.',
        'evidence': [{'unit_id': unit['id'], 'quote': unit['text'], 'role': 'support'}], 'date_refs': []})
    assert validate_output(plan, contract, output).answer == 'Elin normally cycles. [1]\nElin leaves at dawn. [1]'


def test_v12_contract_and_messages_keep_the_original_frozen_bytes():
    plan = plan_for()
    contract = preflight(plan)
    encoded = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()
    assert hashlib.sha256(encoded(contract)).hexdigest() == '7d5c0c7c25a917fbeef573eda61bc957387795b7b092f1ddd075d9cc306ad6ac'
    assert hashlib.sha256(encoded(messages(plan, contract))).hexdigest() == '2350b69962e46b8e3424eca65d082f2ff8b8f244ce00201f10f588bf77e4018f'


def test_v13_freezes_and_sends_the_complete_schema_without_changing_the_original_question():
    from openai.lib._parsing._completions import type_to_response_format_param

    old_plan = plan_for()
    old_contract = preflight(old_plan)
    plan = deepcopy(old_plan)
    plan['policy_versions']['retrieve'] = '@13'
    contract = preflight(plan)
    schema = EvidenceAnswerOutput.model_json_schema()
    assert contract['response_schema'] == schema
    assert {key: value for key, value in contract.items() if key != 'response_schema'} == old_contract
    prompt = messages(plan, contract)
    assert prompt[0] == messages(old_plan, old_contract)[0]
    assert prompt[-1] == messages(old_plan, old_contract)[-1]
    assert prompt[1]['role'] == 'system'
    assert json.loads(prompt[1]['content'].split('\n', 1)[1]) == schema
    assert schema['required'] == ['requirements', 'statements', 'issues']
    assert schema['$defs']['EvidenceRequirement']['required'] == ['text', 'status', 'statement_indices']
    assert schema['$defs']['ReferencedStatement']['required'] == ['kind', 'text', 'evidence', 'date_refs']
    native = type_to_response_format_param(EvidenceAnswerOutput)
    assert native['json_schema']['strict'] is True
    assert native['json_schema']['schema']['properties'] == schema['properties']
    assert validate_output(plan, contract, proposal(contract)).citations == [1]


def test_v13_schema_is_part_of_frozen_input_and_returned_messages_are_isolated():
    plan = plan_for()
    plan['policy_versions']['retrieve'] = '@13'
    contract = preflight(plan)
    prompt = messages(plan, contract)
    prompt[1]['content'] = 'Changed instruction.'
    assert messages(plan, contract)[1]['content'] != prompt[1]['content']
    contract['response_schema']['required'].remove('issues')
    with pytest.raises(ValueError, match='contract_changed_or_unavailable'):
        messages(plan, contract)
    with pytest.raises(ValueError, match='contract_changed_or_unavailable'):
        validate_output(plan, contract, proposal(contract))


def test_v13_schema_instruction_counts_toward_input_budget():
    from backend.memory_app.answer_proof import review_input_tokens

    old_plan = plan_for()
    old_cost = review_input_tokens(messages(old_plan, preflight(old_plan)), EvidenceAnswerOutput)
    plan = deepcopy(old_plan)
    plan['policy_versions']['retrieve'] = '@13'
    new_cost = review_input_tokens(messages(plan, preflight(plan)), EvidenceAnswerOutput)
    assert new_cost > old_cost
    old_plan['budget'] = old_cost
    plan['budget'] = old_cost
    assert preflight(old_plan) is not None
    assert preflight(plan) is None
    plan['budget'] = new_cost
    assert preflight(plan) is not None


@pytest.mark.parametrize('stream', [False, True])
def test_v13_real_sdk_json_responses_validate_with_the_schema_present_in_json_mode(stream):
    from openai.types.chat import ChatCompletion, ChatCompletionChunk
    from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway

    plan = plan_for()
    plan['policy_versions']['retrieve'] = '@13'
    contract = preflight(plan)
    output = proposal(contract)
    raw = json.dumps(output, ensure_ascii=False)
    calls = []

    class Lease:
        def finish(self, status, *, error_code=None):
            assert status == 'succeeded'

    def completion(**request):
        calls.append(request)
        assert request['response_format'] == {'type': 'json_object'}
        schema_messages = [item for item in request['messages']
                           if item['role'] == 'system' and '\n' in item['content']]
        assert len(schema_messages) == 1
        assert json.loads(schema_messages[0]['content'].split('\n', 1)[1]) == contract['response_schema']
        common = {'id': 'synthetic-sdk-response', 'created': 0, 'model': 'synthetic'}
        usage = {'prompt_tokens': 9, 'completion_tokens': 4, 'total_tokens': 13}
        if request.get('stream'):
            return iter([
                ChatCompletionChunk.model_validate({**common, 'object': 'chat.completion.chunk',
                    'choices': [{'index': 0, 'delta': {'reasoning_content': 'Synthetic private reasoning.'},
                                 'finish_reason': None}]}),
                ChatCompletionChunk.model_validate({**common, 'object': 'chat.completion.chunk',
                    'choices': [{'index': 0, 'delta': {'content': '```json\n' + raw + '\n```'},
                                 'finish_reason': None}]}),
                ChatCompletionChunk.model_validate({**common, 'object': 'chat.completion.chunk',
                    'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}),
                ChatCompletionChunk.model_validate({**common, 'object': 'chat.completion.chunk',
                    'choices': [], 'usage': usage}),
            ])
        return ChatCompletion.model_validate({**common, 'object': 'chat.completion',
            'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant',
                'content': '```json\n' + raw + '\n```', 'reasoning_content': 'Private reasoning.'}}],
            'usage': usage})

    gateway = LiteLLMCompletionGateway(provider='deepseek', model='synthetic',
        base_url='https://api.deepseek.com', api_key='synthetic-only',
        completion_fn=completion, egress_guard=lambda *args: Lease())
    prompt = messages(plan, contract)
    if stream:
        emitted = []
        value, usage = gateway.stream_structured_with_usage(prompt, response_model=EvidenceAnswerOutput,
            field=None, on_delta=emitted.append, validate_current=lambda: None)
        assert emitted == []
    else:
        value, usage, _ = gateway.complete_structured_with_usage(prompt,
            response_model=EvidenceAnswerOutput, retries=0, max_wire_attempts=1)
    assert value.model_dump() == output
    assert validate_output(plan, contract, value).citations == [1]
    assert usage == {'input_tokens': 9, 'output_tokens': 4, 'total_tokens': 13}
    assert len(calls) == 1
