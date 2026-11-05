"""Literal fast admission uses real confirmation/validity, without model calls."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.memory_app.v2.answer_evidence import build_evidence_catalog
from backend.memory_app.v2.initial_answer import assess, parse_question, validate_output, validate_plan
from backend.memory_app.v2.links import InsightLinks
from backend.memory_app.v2.policies import override
from backend.memory_app.workspace_query import WorkspaceQuery
from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore


BODY = 'Orion 的重试上限为 3 次。'
QUESTION = 'Orion 在开发环境的重试上限是多少？'
PUBLISHED = '2026-03-12T00:00:00+00:00'
REFERENCE = '2026-10-22T00:00:00+00:00'


@pytest.fixture
def env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    service = RecognitionService(records)
    models = SimpleNamespace(public=lambda: {},
        generation_budget_limits=lambda **_: {'window': 32000, 'reserve': 512})
    query = WorkspaceQuery(records, SQLiteDocumentRepository(records),
                           JsonObjectStore(tmp_path / '.rebuild-data'), models, service)
    return SimpleNamespace(records=records, service=service, query=query,
                           scope=WorkScope('local-user', 'alpha'))


def publish(env, body=BODY, conditions=('开发环境',), at=PUBLISHED):
    with patch('backend.recognition.service._now', return_value=at):
        source = env.service.stage_experience(scope=env.scope,
            content=('适用环境：' + '、'.join(conditions) + '\n' if conditions else '') + body)
        candidate = env.service.propose(scope=env.scope, content=body,
            conditions=conditions, source_experience_ids=[source])
        result = env.service.publish(scope=env.scope, candidate_id=candidate.id,
            expected_revision=candidate.revision, reviewer='local-user')
    return result


def plan_for(env, question=QUESTION):
    with override(compose='@34'):
        return env.query.prepare_ask('alpha', question)


def admitted(env, question=QUESTION):
    plan = plan_for(env, question)
    decision = assess(env.query, plan, reference=REFERENCE)
    assert decision['status'] == 'sufficient', decision
    proof = decision['proof']
    plan['chosen'] = [row for row in plan['chosen'] if row['id'] == proof['source_id']]
    plan['initial_answer_proof'] = proof
    return plan


@pytest.mark.parametrize('question', [
    'Orion 的重试上限是多少？', QUESTION, ' Orion 在 开发环境 的 重试上限 是多少? ',
])
def test_supported_question_consumes_all_slots(question):
    parsed = parse_question(question)
    assert parsed['subject'] == 'Orion' and parsed['attribute'] == '重试上限'


@pytest.mark.parametrize('subject,name', [
    ('星桥服务', '星桥服务'), ('「星桥服务」', '星桥服务'), ('“星桥服务”', '星桥服务'),
    ('「星桥云舟平台」', '星桥云舟平台'), ('“星桥云舟平台”', '星桥云舟平台'),
    ('「星河数据平台」', '星河数据平台'),
    ('数据服务', '数据服务'), ('「数据服务」', '数据服务'), ('“数据服务”', '数据服务'),
    ('Orion', 'Orion'),
])
@pytest.mark.parametrize('question_form', ['是多少', '为多少'])
def test_literal_chinese_names_and_narrow_question_equivalents_have_the_same_slots(subject, name, question_form):
    parsed = parse_question(f' {subject} 在 开发环境 的 重试上限 {question_form}？ ')
    assert parsed == {'subject': name, 'condition': '开发环境', 'attribute': '重试上限'}


@pytest.mark.parametrize('subject', [
    '星桥', '星桥云舟服务', '星 桥服务', '「星 桥」', '“星桥”服务', '“星桥」',
    '它', '它们服务', '自己服务', '今年星桥服务', '去年服务', '本周服务', '最近服务',
    '当前服务', '在生产环境星桥服务', '星桥和云桥服务', '「星桥和云桥」',
    '星桥服务与云舟服务', '非星桥服务', '「非星桥」', '「若星桥」', '「今年星桥」',
    '星桥A服务', 'Orion星桥', '「Orion星桥」', 'A和B', 'A 和 B',
])
def test_chinese_name_boundaries_do_not_swallow_pronouns_modifiers_or_multiple_subjects(subject):
    assert parse_question(f'{subject}的重试上限是多少？') is None


@pytest.mark.parametrize('name', [
    '所有服务', '全部服务', '任意服务', '任何项目', '部分系统', '过去服务', '过往服务',
    '一切服务', '一些服务', '少数服务', '多数服务', '诸多服务',
    '一类服务', '一批服务', '一组服务', '几种服务', '三个项目', '二十服务', '壹拾服务',
    '数项服务', '数家服务', '数套服务', '数名服务',
])
@pytest.mark.parametrize('quoted', [False, True])
def test_matching_quantified_or_past_subjects_are_not_single_named_entities(env, name, quoted):
    subject = f'「{name}」' if quoted else name
    body = f'{subject}的重试上限为 3 次。'
    question = f'{subject}在开发环境的重试上限是多少？'
    original = publish(env, body)
    assert env.service.get_recognition(scope=env.scope, recognition_id=original.id).authorized
    assert parse_question(question) is None
    decision = assess(env.query, plan_for(env, question), reference=REFERENCE)
    assert decision['status'] == 'unsupported' and decision['proof'] is None


@pytest.mark.parametrize('question', [
    '星桥服务现在的重试上限是多少？',
    '星桥服务在开发环境和生产环境的重试上限为多少？',
    '星桥服务的重试上限与并发上限为多少？',
    '星桥服务的重试上限是几次？',
])
def test_chinese_equivalents_do_not_add_time_conditions_multiple_slots_or_implicit_units(question):
    assert parse_question(question) is None


@pytest.mark.parametrize('question', [
    '它的重试上限是多少？', 'it 的重试上限是多少？',
    'Orion 的重试上限和并发上限是多少？', 'Orion 当前的重试上限是多少？',
    'Orion 的重试上限是多少？还要原因。', 'Orion 的全部重试设置是多少？',
    'Orion 在生产或开发环境的重试上限是多少？', 'Orion 的重试上限是多少？\n',
    'Orion 的重试上限不是多少？', 'Orion next weekday 重试上限是多少？',
    'Or ion 的重试上限是多少？', 'O rion 的重试上限是多少？',
    'Orion 在开发 环境的重试上限是多少？', 'Orion 的重试 上限是多少？',
])
def test_unconsumed_or_unsupported_requirements_do_not_make_a_slot(question):
    assert parse_question(question) is None


@pytest.mark.parametrize('relative', ['今年', '去年', '明年', '本周', '最近'])
def test_relative_words_are_outside_both_positive_label_allowlists(relative):
    assert parse_question(f'Orion 的{relative}重试上限是多少？') is None
    assert parse_question(f'Orion 在{relative}开发环境的重试上限是多少？') is None


@pytest.mark.parametrize('question', [
    'Orion 的任意配置是多少？', 'Orion 在自定义环境的重试上限是多少？',
])
def test_free_form_attributes_and_environment_labels_are_not_supported(question):
    assert parse_question(question) is None


def test_2025_confirmation_does_not_anchor_a_relative_attribute_in_2026(env):
    original = publish(env, 'Orion 的今年重试上限为 3 次。', at='2025-03-12T00:00:00+00:00')
    assert env.service.get_recognition(scope=env.scope, recognition_id=original.id).authorized
    question = 'Orion 在开发环境的今年重试上限是多少？'
    decision = assess(env.query, plan_for(env, question), reference=REFERENCE)
    assert decision['status'] == 'unsupported' and decision['proof'] is None


def test_real_confirmed_l3_has_bound_body_conditions_clock_and_source(env):
    original = publish(env)
    plan = admitted(env)
    proof = plan['initial_answer_proof']
    assert proof['source_id'] == original.id and proof['source_revision'] == 1
    assert proof['conditions'] == ['开发环境'] and proof['reference'] == REFERENCE
    assert proof['quote'] == BODY + '\n\n适用条件：\n- 开发环境'
    assert proof['matching_corpus'][0]['validity']['valid_from'] == PUBLISHED
    assert proof['matching_corpus'][0]['source_evidence']
    validate_plan(env.query, plan)


@pytest.mark.parametrize('body,question,answer', [
    ('星桥服务的重试上限为 3 次。', '星桥服务在开发环境的重试上限为多少？',
     '「星桥服务」的重试上限为 3 次。'),
    ('「星桥服务」的重试上限为 3 次。', '星桥服务在开发环境的重试上限是多少？',
     '星桥服务的重试上限为 3 次。'),
    ('“星桥云舟平台”的重试上限为 3 次。', '「星桥云舟平台」在开发环境的重试上限是多少？',
     '「星桥云舟平台」的重试上限为 3 次。'),
    ('数据服务的重试上限为 3 次。', '「数据服务」在开发环境的重试上限为多少？',
     '数据服务的重试上限为 3 次。'),
    ('「星河数据平台」的重试上限为 3 次。', '“星河数据平台”在开发环境的重试上限是多少？',
     '「星河数据平台」的重试上限为 3 次。'),
])
def test_confirmed_chinese_fact_preserves_owner_proof_and_only_normalizes_outer_name_quotes(env, body, question, answer):
    original = publish(env, body)
    plan = admitted(env, question)
    proof = plan['initial_answer_proof']
    assert proof['source_id'] == original.id and proof['statement'] == body
    assert proof['quote'] == body + '\n\n适用条件：\n- 开发环境'
    validate_plan(env.query, plan)
    output = {'statements': [{'kind': 'fact', 'text': answer, 'evidence': [
        {'role': 'support', 'unit_id': proof['unit_id'], 'quote': proof['quote']}]}]}
    catalog = build_evidence_catalog(plan['chosen'], compose_version='@34')
    validate_output(plan, output, catalog)
    name = proof['slot']['subject']
    split_name = name[:1] + ' ' + name[1:]
    for bad_text in (answer.replace('3', '4'), answer.replace(name, split_name, 1)):
        bad = deepcopy(output)
        bad['statements'][0]['text'] = bad_text
        with pytest.raises(ValueError, match='initial_answer_output_incomplete'):
            validate_output(plan, bad, catalog)


@pytest.mark.parametrize('suffix', ['服务', '系统', '项目', '组件', '模块'])
@pytest.mark.parametrize('quoted_source', [False, True])
def test_each_typed_chinese_suffix_has_identical_bare_and_quoted_entity_binding(env, suffix, quoted_source):
    name = '星桥' + suffix
    source_name = f'「{name}」' if quoted_source else name
    question_name = name if quoted_source else f'“{name}”'
    publish(env, f'{source_name}的重试上限为 3 次。')
    plan = admitted(env, f'{question_name}在开发环境的重试上限为多少？')
    proof = plan['initial_answer_proof']
    assert proof['slot']['subject'] == name == proof['request']['subject']
    validate_plan(env.query, plan)


@pytest.mark.parametrize('body', [
    '今年星桥服务的重试上限为 3 次。', '「今年星桥」的重试上限为 3 次。',
    '星 桥服务的重试上限为 3 次。', '星桥A服务的重试上限为 3 次。',
])
def test_ambiguous_or_split_chinese_source_names_do_not_support_a_clear_named_question(env, body):
    publish(env, body)
    decision = assess(env.query, plan_for(env, '星桥服务在开发环境的重试上限是多少？'), reference=REFERENCE)
    assert decision['status'] == 'insufficient' and decision['proof'] is None


@pytest.mark.parametrize('blocked', ['condition', 'expired', 'conflict', 'source'])
def test_chinese_name_support_keeps_the_existing_applicability_and_source_gates(env, blocked):
    body = '星桥服务的重试上限为 3 次。'
    original = publish(env, body, conditions=('生产环境',) if blocked == 'condition' else ('开发环境',))
    plan = plan_for(env, '星桥服务在开发环境的重试上限为多少？')
    if blocked == 'expired':
        with env.records.begin() as tx:
            marker = tx.read('v2_insight_validity', original.id)
            tx.put('v2_insight_validity', original.id, {**marker.payload, 'valid_until': REFERENCE},
                   expected_revision=marker.revision)
            tx.commit()
    elif blocked == 'conflict':
        publish(env, '星桥服务的重试上限为 4 次。')
    elif blocked == 'source':
        identity = original.source_experience_ids[0]
        source = env.records.read('recognition_experiences', identity)
        env.service.revoke_experience(scope=env.scope, experience_id=identity, expected_revision=source.revision)
    decision = assess(env.query, plan, reference=REFERENCE)
    assert decision['status'] == 'insufficient' and decision['proof'] is None


def test_quoted_and_bare_chinese_names_cannot_hide_a_conflicting_slot_value(env):
    original = publish(env, '星桥服务的重试上限为 3 次。')
    plan = admitted(env, '星桥服务在开发环境的重试上限是多少？')
    alternative = publish(env, '「星桥服务」的重试上限为 4 次。')
    assert [row['id'] for row in plan['chosen']] == [original.id]
    assert alternative.id != original.id
    decision = assess(env.query, plan, reference=REFERENCE)
    assert decision['status'] == 'insufficient' and decision['reason'] == 'matching_slot_values_conflict'
    with pytest.raises(RecognitionConflict, match='initial_answer_evidence_changed'):
        validate_plan(env.query, plan)


def test_unconditional_literal_requires_an_unconditional_question(env):
    publish(env, conditions=())
    plan = admitted(env, 'Orion 的重试上限是多少？')
    assert plan['initial_answer_proof']['conditions'] == []
    validate_plan(env.query, plan)


@pytest.mark.parametrize('conditions', [(), ('生产环境',), ('开发环境', '紧急情况')])
def test_missing_or_additional_conditions_fall_back(env, conditions):
    publish(env, conditions=conditions)
    assert assess(env.query, plan_for(env), reference=REFERENCE)['status'] == 'insufficient'


def test_separate_memories_cannot_supply_a_missing_applicability_condition(env):
    value = publish(env, conditions=('生产环境',))
    context = publish(env, '开发环境允许使用 Orion。', conditions=('开发环境',))
    plan = plan_for(env)
    collected = env.query.collect_candidates('alpha', QUESTION)
    rows = [row for row in collected['candidates'] if row['kind'] == 'recognition']
    assert {value.id, context.id} <= {row['id'] for row in rows}
    # Both are genuinely available, but the value applies to another environment;
    # the second statement cannot turn that value into a development fact.
    plan['chosen'] = rows
    decision = assess(env.query, plan, reference=REFERENCE)
    assert decision['status'] == 'insufficient' and decision['proof'] is None


@pytest.mark.parametrize('body', [
    'Hydra 的重试上限为 3 次。', 'Orion 的并发上限为 3 次。',
    'Orion 的重试上限可能为 3 次。', 'Orion 的重试上限不为 3 次。',
    'Orion 的重试上限为 3 次，生产环境为 4 次。',
    'Orion 的重试上限为 3 次但测试。',
])
def test_coverage_or_overlapping_words_do_not_prove_the_requested_slot(env, body):
    publish(env, body)
    assert assess(env.query, plan_for(env), reference=REFERENCE)['status'] == 'insufficient'


@pytest.mark.parametrize('body,question', [
    ('Or ion 的重试上限为 3 次。', QUESTION),
    ('O rion 的重试上限为 3 次。', QUESTION),
    ('Orion 的重试上限为 1 2 次。', QUESTION),
    ('Orion 的等待时长为 1 m s。', 'Orion 在开发环境的等待时长是多少？'),
    ('Orion 的缓存上限为 1 K B。', 'Orion 在开发环境的缓存上限是多少？'),
])
def test_spaces_inside_subject_number_or_unit_are_not_normalized_away(env, body, question):
    publish(env, body)
    decision = assess(env.query, plan_for(env, question), reference=REFERENCE)
    assert decision['status'] == 'insufficient' and decision['proof'] is None


def test_pending_does_not_create_confirmation_or_validity(env):
    source = env.service.stage_experience(scope=env.scope, content=BODY)
    env.service.propose(scope=env.scope, content=BODY, conditions=['开发环境'],
                        source_experience_ids=[source])
    assert assess(env.query, plan_for(env), reference=REFERENCE)['status'] == 'insufficient'


@pytest.mark.parametrize('end', [REFERENCE, '2026-10-21T00:00:00+00:00'])
def test_expired_or_exact_end_is_not_current(env, end):
    original = publish(env)
    with env.records.begin() as tx:
        row = tx.read('v2_insight_validity', original.id)
        tx.put('v2_insight_validity', original.id, {**row.payload, 'valid_until': end},
               expected_revision=row.revision)
        tx.commit()
    assert assess(env.query, plan_for(env), reference=REFERENCE)['status'] == 'insufficient'


def test_missing_real_confirmation_and_future_publication_fall_back(env):
    original = publish(env, at='2026-10-23T00:00:00+00:00')
    assert assess(env.query, plan_for(env), reference=REFERENCE)['status'] == 'insufficient'
    with env.records.begin() as tx:
        marker = tx.read('v2_insight_validity', original.id)
        tx.put('v2_insight_validity', original.id,
            {**marker.payload, 'valid_from': PUBLISHED}, expected_revision=marker.revision)
        tx.commit()
    assert assess(env.query, plan_for(env), reference=REFERENCE)['status'] == 'insufficient'


def test_missing_publication_facts_are_not_reconstructed_from_active_state(env):
    original = publish(env)
    with env.records.begin() as tx:
        version = tx.read('recognition_versions', original.id + '~v1')
        tx.delete('recognition_versions', version.object_id, expected_revision=version.revision)
        for candidate in tx.list('recognition_candidates'):
            if candidate.payload.get('recognition_id') == original.id:
                tx.delete('recognition_candidates', candidate.object_id, expected_revision=candidate.revision)
        tx.commit()
    assert env.service.get_recognition(scope=env.scope, recognition_id=original.id).authorized
    assert assess(env.query, plan_for(env), reference=REFERENCE)['status'] == 'insufficient'


def test_validity_revision_and_original_source_revocation_invalidate_frozen_admission(env):
    original = publish(env)
    plan = admitted(env)
    with env.records.begin() as tx:
        marker = tx.read('v2_insight_validity', original.id)
        tx.put('v2_insight_validity', original.id,
            {**marker.payload, 'valid_until': '2026-12-01T00:00:00+00:00'},
            expected_revision=marker.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict, match='initial_answer_evidence_changed'):
        validate_plan(env.query, plan)
    current = admitted(env)
    experience_id = original.source_experience_ids[0]
    source = env.records.read('recognition_experiences', experience_id)
    env.service.revoke_experience(scope=env.scope, experience_id=experience_id,
                                  expected_revision=source.revision)
    with pytest.raises(RecognitionConflict, match='initial_answer_evidence_changed'):
        validate_plan(env.query, current)


def test_unselected_conflicting_value_in_complete_owner_corpus_prevents_fast_answer(env):
    original = publish(env)
    plan = admitted(env)
    other = publish(env, 'Orion 的重试上限为 4 次。')
    assert [row['id'] for row in plan['chosen']] == [original.id]
    assert other.id != original.id
    result = assess(env.query, plan, reference=REFERENCE)
    assert result['reason'] == 'matching_slot_values_conflict'
    with pytest.raises(RecognitionConflict, match='initial_answer_evidence_changed'):
        validate_plan(env.query, plan)


def test_confirmed_refutation_falls_back_even_when_peer_is_not_selected(env):
    one = publish(env)
    two = publish(env)
    links = InsightLinks(env.records, env.service)
    proposal = links.propose('alpha', one.id, two.id, 'refutes', 'Synthetic explicit conflict')
    links.review('alpha', proposal['id'], proposal['revision'], True)
    result = assess(env.query, plan_for(env), reference=REFERENCE)
    assert result['status'] == 'insufficient' and result['reason'] == 'initial_unresolved_refutation'


def test_source_revision_or_window_change_invalidates_existing_proof(env):
    original = publish(env)
    plan = admitted(env)
    tampered = deepcopy(plan)
    row = tampered['chosen'][0]
    row['excerpt'] = BODY
    with pytest.raises(RecognitionConflict, match='initial_answer_evidence_changed'):
        validate_plan(env.query, tampered)
    env.service.revise(scope=env.scope, recognition_id=original.id, expected_revision=1,
                       content='Orion 的重试上限为 4 次。')
    with pytest.raises(RecognitionConflict, match='initial_answer_evidence_changed'):
        validate_plan(env.query, plan)


def test_final_answer_must_answer_the_bound_slot_and_cite_its_complete_atom(env):
    publish(env)
    plan = admitted(env)
    proof = plan['initial_answer_proof']
    catalog = build_evidence_catalog(plan['chosen'], compose_version='@34')
    output = {'statements': [{'kind': 'fact', 'text': BODY, 'evidence': [
        {'role': 'support', 'unit_id': proof['unit_id'], 'quote': proof['quote']}]}]}
    validate_output(plan, output, catalog)
    for changes in ({'text': 'Orion 的重试上限为 4 次。'}, {'kind': 'limitation'},
                    {'text': 'Or ion 的重试上限为 3 次。'},
                    {'text': 'O rion 的重试上限为 3 次。'},
                    {'text': 'Orion 的重试上限为 0 3 次。'},
                    {'evidence': [{'role': 'context', 'unit_id': 1, 'quote': proof['quote']}]},
                    {'evidence': [{'role': 'support', 'unit_id': 1, 'quote': BODY}]}):
        bad = deepcopy(output)
        bad['statements'][0].update(changes)
        with pytest.raises(ValueError, match='initial_answer_output_incomplete'):
            validate_output(plan, bad, catalog)
    with pytest.raises(ValueError, match='initial_answer_output_incomplete'):
        validate_output(plan, {'statements': []}, catalog)
