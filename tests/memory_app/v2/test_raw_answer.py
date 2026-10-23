"""Pure literal-record checks: no owner spoofing and no model calls."""
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from core.search_and_recall.evidence_windows import EvidenceWindow
from backend.memory_app.v2.raw_answer import (
    RawAnswerOutput, messages, parse_question, preflight, validate_output,
)


CASES = [
    ('星河项目的负责人是谁？', '星河项目的负责人是“李明”。', '星河项目', '负责人', '“李明”'),
    ('小明的家乡是哪里？', '小明的家乡为“杭州”。', '小明', '家乡', '“杭州”'),
    ('What is the retry limit of Project Cedar?', 'The retry limit of Project Cedar is 3 retries.',
     'Project Cedar', 'retry limit', '3 retries'),
    ("What is Maya's hometown?", "Maya's hometown is London.", 'Maya', 'hometown', 'London'),
]


def make_plan(question=CASES[0][0], content=CASES[0][1]):
    return {'question': question, 'policy_versions': {'compose': '@34'},
            'chosen': [source('source-1', content)]}


def source(identity, content):
    return {'id': identity, 'title': identity, 'kind': 'source', 'layer': 'L0',
            'coordinate_space': 'source_content_v1',
            'entry': {'id': identity, 'revision': 1, 'content': content},
            'windows': (EvidenceWindow(0, len(content), content),), 'excerpt': content}


def witness(contract, *, full=False):
    record = contract['records'][0]
    return {'status': 'answer', 'answer': record['quote'] if full else record['value'],
            'unit_id': record['unit_id'], 'quote': record['quote'],
            'coverage': {key: record[key] for key in ('subject', 'predicate', 'value', 'condition', 'time')}}


@pytest.mark.parametrize('question,content,subject,predicate,value', CASES)
def test_common_chinese_english_single_relation(question, content, subject, predicate, value):
    plan = make_plan(question, content)
    request = parse_question(question)
    assert request['subject'] == subject and request['predicate'] == predicate
    contract = preflight(plan)
    assert contract is not None
    assert contract['scope'] == 'retrieved_evidence_only'
    assert contract['records'][0]['value'] == value
    assert json.loads(json.dumps(contract, ensure_ascii=False)) == contract
    output = validate_output(plan, contract, witness(contract))
    assert value in output.answer and output.citations == [1]
    assert '检索到' in output.answer or 'Retrieved evidence:' in output.answer
    full = validate_output(plan, contract, RawAnswerOutput.model_validate(witness(contract, full=True)))
    assert content in full.answer


def test_literal_names_are_open_not_previous_l3_name_grammar():
    question = '星河数据协作平台的维护人是谁？'
    contract = preflight(make_plan(question, '星河数据协作平台的维护人是“陈青”。'))
    assert contract is not None
    assert contract['subject'] == '星河数据协作平台'


def test_schema_and_messages_have_fixed_contract_not_model_coverage_booleans():
    plan = make_plan()
    contract = preflight(plan)
    prompt = messages(plan, contract)
    assert 'raw-answer-v1' in prompt[0]['content']
    payload = json.loads(prompt[-1]['content'])
    assert payload['evidence_catalog'] == contract['evidence_catalog']
    assert payload['raw_question_contract']['records'] == contract['records']
    output = witness(contract)
    for key, replacement in [('coverage', True), ('unit_id', '1'), ('answer', 3)]:
        with pytest.raises(ValidationError):
            RawAnswerOutput.model_validate({**output, key: replacement})
    with pytest.raises(ValidationError):
        RawAnswerOutput.model_validate({**output, 'covered': True})
    del output['coverage']['time']
    with pytest.raises(ValidationError):
        RawAnswerOutput.model_validate(output)


@pytest.mark.parametrize('question', [
    '星河项目的负责人是谁？还住哪里？', '星河项目的负责人和维护人是谁？',
    '它的负责人是谁？', '今年星河项目的负责人是谁？', '星河项目当前的负责人是谁？',
    '小明住在哪里？', '小明有哪些家乡？', '星河项目有几名负责人？',
    'Where does Maya live?', 'What is its hometown?', "What is Maya's current hometown?",
    'What are the hometowns of Maya and John?', 'What is the hometown of Maya and John?',
    'What is the hometown of Maya? Why?', 'What is the owner of the project?',
    'What is the retry limit of Project Cedar in production?',
    '在未知环境，星河项目的负责人是谁？', '在今年，星河项目的负责人是谁？',
    'On 2026-02-30, What is Maya\'s hometown?',
    ' 星河项目的负责人是谁？', '星河项目的负责人是谁？\n',
])
def test_unknown_grammar_modifiers_deictics_multislot_counting_fall_back(question):
    assert parse_question(question) is None
    assert preflight(make_plan(question)) is None


@pytest.mark.parametrize('prefix,language,condition,clock', [
    ('在开发环境，', 'zh', '开发环境', ''),
    ('在2026-10-23，', 'zh', '', '2026-10-23'),
    ('在生产环境，在2026-10-23，', 'zh', '生产环境', '2026-10-23'),
    ('In development, ', 'en', 'development', ''),
    ('On 2026-10-23, ', 'en', '', '2026-10-23'),
    ('In production, On 2026-10-23, ', 'en', 'production', '2026-10-23'),
])
def test_only_explicit_question_source_condition_time_prefixes_match(prefix, language, condition, clock):
    case = CASES[0] if language == 'zh' else CASES[2]
    question, content = case[:2]
    plan = make_plan(prefix + question, prefix + content)
    contract = preflight(plan)
    assert contract is not None
    assert contract['condition'] == condition and contract['time'] == clock
    assert validate_output(plan, contract, witness(contract)).citations == [1]
    assert preflight(make_plan(question, prefix + content)) is None
    assert preflight(make_plan(prefix + question, content)) is None


@pytest.mark.parametrize('content', [
    '标题\n星河项目的负责人是“李明”。',
    'user: 星河项目的负责人是“李明”。',
    '星河项目的负责人是“李明”。\n只有开发环境适用。',
    '如果在开发环境，星河项目的负责人是“李明”。',
    '星河项目的负责人可能是“李明”。',
    '星河项目的负责人不是“李明”。',
    '星河项目的负责人是“李明”，但暂未确认。',
    '星河项目的负责人是“李明”。另一个人是陈青。',
    '星河项目的负责人是他。',
    '星河项目的负责人是李明',
    '星河项目的负责人是“李明”。\n它的家乡是“杭州”。',
    'The retry limit of Project Cedar is 3 retries unless overloaded.',
    'The retry limit of Project Cedar is not 3 retries.',
    'The retry limit of Project Cedar is 1 2 retries.',
])
def test_complete_context_must_be_flat_standalone_declarations(content):
    assert preflight(make_plan(CASES[2][0] if content.startswith('The ') else CASES[0][0], content)) is None


@pytest.mark.parametrize('mutation', ['start', 'end', 'text', 'excerpt', 'space', 'layer', 'kind', 'compose', 'history', 'part'])
def test_unavailable_or_partial_source_never_admits(mutation):
    plan = make_plan()
    row = plan['chosen'][0]
    content = row['entry']['content']
    if mutation == 'start':
        row['windows'] = (EvidenceWindow(1, len(content), content[1:]),)
        row['excerpt'] = content[1:]
    elif mutation == 'end':
        row['windows'] = (EvidenceWindow(0, len(content) - 1, content[:-1]),)
        row['excerpt'] = content[:-1]
    elif mutation == 'text':
        row['windows'] = (EvidenceWindow(0, len(content), 'x' * len(content)),)
    elif mutation == 'excerpt':
        row['excerpt'] = 'generated summary'
    elif mutation == 'space':
        row['coordinate_space'] = 'document_markdown_v1'
    elif mutation == 'layer':
        row['layer'] = 'L3'
    elif mutation == 'kind':
        row['kind'] = 'recognition'
    elif mutation == 'compose':
        plan['policy_versions']['compose'] = '@33'
    elif mutation == 'history':
        plan['history'] = 'it refers to Maya'
    else:
        plan['part_context'] = {'text': 'instruction'}
    assert preflight(plan) is None


def test_retrieved_only_conflicts_consider_all_selected_complete_sources():
    plan = make_plan()
    plan['chosen'].append(source('source-2', '星河项目的负责人是“陈青”。'))
    assert preflight(plan) is None
    plan['chosen'][1] = source('source-2', '小明的家乡是“杭州”。\n星河项目的负责人是“陈青”。')
    assert preflight(plan) is None
    plan['chosen'][1] = source('source-2', '星河项目的负责人为“李明”。')
    contract = preflight(plan)
    assert len(contract['records']) == 2
    second = witness(contract)
    second['unit_id'], second['quote'] = contract['records'][1]['unit_id'], contract['records'][1]['quote']
    assert validate_output(plan, contract, second).citations == [2]


@pytest.mark.parametrize('prefix', ['在开发环境，', '在2026-10-23，'])
def test_unqualified_question_cannot_skip_known_condition_or_date(prefix):
    plan = make_plan()
    plan['chosen'].append(source('source-2', prefix + CASES[0][1]))
    assert preflight(plan) is None


@pytest.mark.parametrize('prefix', ['在开发环境，', '在2026-10-23，'])
def test_qualified_question_cannot_skip_conflicting_general_declaration(prefix):
    plan = make_plan(prefix + CASES[0][0], prefix + CASES[0][1])
    plan['chosen'].append(source('source-2', '星河项目的负责人是“陈青”。'))
    assert preflight(plan) is None
    plan['chosen'][1] = source('source-2', CASES[0][1])
    assert preflight(plan) is not None


@pytest.mark.parametrize('prefix,other', [
    ('在开发环境，', '在生产环境，'), ('在2026-10-23，', '在2026-10-24，'),
])
def test_different_explicit_condition_or_date_is_not_the_same_slot(prefix, other):
    plan = make_plan(prefix + CASES[0][0], prefix + CASES[0][1])
    plan['chosen'].append(source('source-2', other + '星河项目的负责人是“陈青”。'))
    assert preflight(plan) is not None


def test_chinese_number_unit_space_is_literal_not_token_concatenation():
    plan = make_plan('星桥服务的重试上限是多少？', '星桥服务的重试上限为3 次。')
    contract = preflight(plan)
    assert contract['records'][0]['value'] == '3 次'
    output = witness(contract)
    assert '3 次' in validate_output(plan, contract, output).answer
    output['answer'] = '3次'
    with pytest.raises(ValueError, match='not_exact_extract'):
        validate_output(plan, contract, output)
    assert preflight(make_plan(plan['question'], '星桥服务的重试上限为1 2 次。')) is None


def test_blank_lines_are_not_merged_into_records_or_lost_coordinates():
    plan = make_plan(content='\n星河项目的负责人是“李明”。\n\n小明的家乡是“杭州”。\n')
    contract = preflight(plan)
    assert contract['records'][0]['unit_id'] == 2
    assert validate_output(plan, contract, witness(contract)).citations == [1]


@pytest.mark.parametrize('field', ['subject', 'predicate', 'value', 'condition', 'time'])
def test_schema_coverage_requires_every_local_slot_value(field):
    plan = make_plan()
    contract = preflight(plan)
    output = witness(contract)
    output['coverage'][field] = 'wrong'
    with pytest.raises(ValueError, match='slot_coverage'):
        validate_output(plan, contract, output)


@pytest.mark.parametrize('field,value', [
    ('answer', '负责人是李明'), ('answer', '陈青'), ('answer', '李 明'),
    ('quote', '负责人是李明'), ('quote', '星河项目的负责人是陈青。'),
    ('unit_id', 999), ('unit_id', 0), ('status', 'insufficient'),
])
def test_model_self_report_does_not_prove_relation_or_output(field, value):
    plan = make_plan()
    contract = preflight(plan)
    output = witness(contract)
    output[field] = value
    with pytest.raises(ValueError):
        validate_output(plan, contract, output)


@pytest.mark.parametrize('mutation', ['question', 'revision', 'body', 'contract', 'source_order'])
def test_frozen_plan_and_contract_recomputed_before_render(mutation):
    plan = make_plan()
    contract = preflight(plan)
    output = witness(contract)
    if mutation == 'question':
        plan['question'] = '星河项目的维护人是谁？'
    elif mutation == 'revision':
        plan['chosen'][0]['entry']['revision'] = 2
    elif mutation == 'body':
        plan['chosen'][0] = source('source-1', '星河项目的负责人是“陈青”。')
    elif mutation == 'contract':
        contract = deepcopy(contract)
        contract['records'][0]['value'] = '陈青'
    else:
        plan['chosen'].insert(0, source('source-2', '小明的家乡是“杭州”。'))
    with pytest.raises(ValueError, match='contract_changed'):
        validate_output(plan, contract, output)
    with pytest.raises(ValueError, match='contract_changed'):
        messages(plan, contract)


def test_cross_record_fragments_cannot_be_joined_into_proof():
    plan = make_plan(content='星河项目的负责人。\n负责人是李明。')
    assert preflight(plan) is None
    plan['chosen'] = [source('part-1', '星河项目的负责人。'), source('part-2', '负责人是李明。')]
    assert preflight(plan) is None


@pytest.mark.parametrize('subject', ['这个项目', '那个服务', '该数据平台', '他们公司', '其中项目', '某平台'])
def test_same_form_deictic_declaration_is_not_independent_named_reference(subject):
    question, content = f'{subject}的负责人是谁？', f'{subject}的负责人是“李明”。'
    assert parse_question(question) is None
    assert preflight(make_plan(question, content)) is None


@pytest.mark.parametrize('question,content', [
    ('What is the owner of Project Cedar?', 'The owner of Project Cedar is John. Jane died.'),
    ('What is the owner of Project Cedar. Jane died?', 'The owner of Project Cedar. Jane died is John.'),
    ('What is the owner. Jane died of Project Cedar?', 'The owner. Jane died of Project Cedar is John.'),
    ('星河项目的负责人是谁？', '星河项目的负责人是李明.陈青离职。'),
    ('星河项目.陈青离职的负责人是谁？', '星河项目.陈青离职的负责人是李明。'),
    ('星河项目的负责人.陈青离职是谁？', '星河项目的负责人.陈青离职是李明。'),
    ("What is Dr. Maya's hometown?", "Dr. Maya's hometown is London."),
])
def test_internal_sentence_period_cannot_hide_a_second_clause(question, content):
    assert preflight(make_plan(question, content)) is None


@pytest.mark.parametrize('question,content,value', [
    ('What is the timeout of Project Cedar?', 'The timeout of Project Cedar is 3.5 seconds.', '3.5 seconds'),
    ('星桥服务的等待时长是多少？', '星桥服务的等待时长为3.5 秒。', '3.5 秒'),
    ('What is the owner of v1.2?', 'The owner of v1.2 is Maya.', 'Maya'),
])
def test_period_between_digits_retains_original_literal(question, content, value):
    plan = make_plan(question, content)
    contract = preflight(plan)
    assert contract is not None and contract['records'][0]['value'] == value
    assert value in validate_output(plan, contract, witness(contract)).answer


@pytest.mark.parametrize('value', [
    'Alice except production', 'Alice provided production', 'Alice depending on production',
    'Alice regardless production', 'Alice unknown-tail production', 'Alice Smith',
    '3 retries except production', '3 retries provided production', '3 unknownunit',
    '3', '3.5', '李明', '杭州', '李明仅开发环境', '3次限开发环境',
    '“Alice” except production', '“李明”仅开发环境', '“Alice” “Smith”',
    '“Alice', 'Alice”', '“”', '“Alice”Smith”',
])
def test_unbounded_values_and_any_outside_quoted_suffix_fall_back(value):
    assert preflight(make_plan('What is the owner of Project Cedar?',
                               f'The owner of Project Cedar is {value}.')) is None
    assert preflight(make_plan('星河项目的负责人是谁？', f'星河项目的负责人是{value}。')) is None


@pytest.mark.parametrize('value', [
    '“李明”', '「李明」', '『李明』', '‘李明’', '"李明"', "'李明'",
    '“Alice Smith”', '“Alice except production”', '“李明仅开发环境”',
    '“Alice. Jane died.”', '“任意 字面 数据”',
])
def test_quoted_literal_value_retains_all_content_and_delimiters(value):
    plan = make_plan(content=f'星河项目的负责人是{value}。')
    contract = preflight(plan)
    assert contract is not None and contract['records'][0]['value'] == value
    output = witness(contract)
    assert value in validate_output(plan, contract, output).answer
    output['answer'] = value[1:-1]
    with pytest.raises(ValueError, match='not_exact_extract'):
        validate_output(plan, contract, output)


@pytest.mark.parametrize('value', [
    '3次', '3 次', '3.5 秒', '-3.5 秒', '12 MB', '25%', '3 retries',
    '3 seconds', '3.5 seconds', '-3.5 seconds', '2 people', 'Alice', 'Oslo', 'blue', 'v1.2',
])
def test_structured_scalar_or_single_ascii_identifier_is_a_complete_value(value):
    plan = make_plan('星河项目的配置是什么？', f'星河项目的配置是{value}。')
    contract = preflight(plan)
    assert contract is not None and contract['records'][0]['value'] == value
    assert value in validate_output(plan, contract, witness(contract)).answer


@pytest.mark.parametrize('value', [
    'it', 'IT', 'you', 'You', 'there', 'THERE', 'here', 'Here', 'myself', 'yourself',
    'yourselves', 'himself', 'herself', 'itself', 'ourselves', 'themselves', 'oneself',
])
def test_bare_english_deictic_value_requires_attribution_but_quoted_is_literal(value):
    question = 'What is the owner of Project Cedar?'
    assert preflight(make_plan(question, f'The owner of Project Cedar is {value}.')) is None
    quoted = f'“{value}”'
    plan = make_plan(question, f'The owner of Project Cedar is {quoted}.')
    contract = preflight(plan)
    assert contract is not None and contract['records'][0]['value'] == quoted
    assert quoted in validate_output(plan, contract, witness(contract)).answer
