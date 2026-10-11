"""未采用首稿的具体字段诊断与严格最终资格。"""
from copy import deepcopy
import json

import pytest

from backend.memory_app.v2.answer_evidence import (
    DERIVED_NOTES, build_partition_messages, final_response_model, inspect_referenced_first_draft, reviewed_scope_answer,
)
from backend.memory_app.answer_proof import review_input_tokens
from backend.memory_app.v2.policies import ACTIVE
from tests.memory_app.v2.test_answer_contract_v27 import material as _material_fixture
from tests.memory_app.v2.test_answer_contract_v28 import final_for

material = _material_fixture


@pytest.mark.parametrize(('fault', 'field'), [
    ('ref', 'facts[0].date_refs[0]'), ('partial', 'facts[0].date_refs[0]'),
    ('quote', 'facts[0].evidence[0].quote'), ('unit', 'facts[0].evidence[0].unit_id'),
])
def test_first_model_field_error_can_be_corrected_without_mutating_raw(material, fault, field):
    chosen, catalog, first = material
    original_good = deepcopy(first)
    if fault == 'ref':
        first['facts'][0]['date_refs'] = [999]
    elif fault == 'partial':
        first['facts'][0]['evidence'][0]['quote'] = 'I painted this self-portrait last week'
    elif fault == 'quote':
        first['facts'][0]['evidence'][0]['quote'] = '23 August 2023'
    else:
        first['facts'][0]['evidence'][0]['unit_id'] = 999
    before = deepcopy(first)
    raw, normalized, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen,
        question='What did Maya paint?', compose_version='@29')
    assert raw.model_dump() == before and first == before
    assert diagnostics[0]['fact_index'] == 1
    assert any(issue['field_path'] == field and issue['repair'] for issue in diagnostics[0]['issues'])
    assert normalized.facts[0].derived_notes == []
    assert 999 not in normalized.partition.selected_ids
    final = final_for(original_good, action='correct')
    final['statements'][0]['date_refs'] = []
    result = reviewed_scope_answer(final, catalog, first, chosen=chosen,
        question='What did Maya paint?', compose_version='@29')
    assert result.statements[0].derived_notes == []
    final['statements'][0]['first_fact_uses'][0]['action'] = 'adopt'
    with pytest.raises(ValueError, match='invalid_first_fact_requires_correction'):
        reviewed_scope_answer(final, catalog, first, chosen=chosen, question='What?', compose_version='@29')
    with pytest.raises(ValueError):
        reviewed_scope_answer(final_for(first, action='correct'), catalog, first, chosen=chosen,
            question='What?', compose_version='@29')


def test_calendar_diagnostic_identifies_actual_text_date_not_empty_ref_field(material):
    chosen, catalog, first = material
    first['facts'][0].update(kind='inference', date_refs=[], text='Maya painted this before 23 August 2023.')
    _, _, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen,
        question='What did Maya paint?', compose_version='@29')
    issue, = [issue for issue in diagnostics[0]['issues'] if issue['code'] == 'grounded_calendar_provenance_invalid']
    assert issue['field_path'] == 'facts[0].text'
    left, right = issue['text_span']
    assert first['facts'][0]['text'][left:right] == '23 August 2023'
    assert issue['calendar_value'] == {'start': '2023-08-23', 'end': '2023-08-23', 'precision': 'day'}
    final = final_for(first, action='correct')
    final['statements'][0]['first_fact_uses'][0]['reason'] = 'Removed unsupported date refs.'
    with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
        reviewed_scope_answer(final, catalog, first, chosen=chosen, question='What?', compose_version='@29')
    final['statements'][0]['text'] = 'Maya painted this self-portrait.'
    assert reviewed_scope_answer(final, catalog, first, chosen=chosen, question='What?', compose_version='@29').statements


def test_support_diagnostic_requires_real_role_or_limitation_change(material):
    chosen, catalog, first = material
    first['facts'][0].update(kind='inference', date_refs=[])
    first['facts'][0]['evidence'][0]['role'] = 'context'
    _, _, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen, question='What?', compose_version='@29')
    issue, = diagnostics[0]['issues']
    assert issue['code'] == 'grounded_statement_without_support'
    assert issue['field_path'] == 'facts[0].evidence[*].role'
    assert issue['actual_roles'] == ['context']
    final = final_for(first, action='correct')
    final['statements'][0]['first_fact_uses'][0]['reason'] = 'Replaced context with support.'
    with pytest.raises(ValueError, match='grounded_statement_without_support'):
        reviewed_scope_answer(final, catalog, first, chosen=chosen, question='What?', compose_version='@29')
    final['statements'][0].update(kind='limitation', text='The precise date is not established.')
    assert reviewed_scope_answer(final, catalog, first, chosen=chosen, question='What?', compose_version='@29').statements


def test_invalid_positive_date_never_authorizes_later_precision(material):
    chosen, catalog, first = material
    first['facts'][0].update(kind='date', text='Maya painted it in 2023-08-14 to 2023-08-20.', date_refs=[999])
    limitation = deepcopy(first['facts'][0])
    limitation.update(kind='limitation', text='2023-08-14 to 2023-08-20 is only a week range.', date_refs=[1])
    limitation['evidence'][0]['role'] = 'context'
    first['facts'].append(limitation)
    _, normalized, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen, question='When?', compose_version='@29')
    assert [item['fact_index'] for item in diagnostics] == [1, 2]
    assert all(not item.derived_notes for item in normalized.facts)
    assert 'grounded_calendar_provenance_invalid' in diagnostics[1]['codes']


def test_soft_first_error_does_not_hide_later_forged_quote(material):
    chosen, catalog, first = material
    first['facts'][0]['date_refs'] = [999]
    first['facts'].append(deepcopy(first['facts'][0]))
    first['facts'][1].update(date_refs=[])
    first['facts'][1]['evidence'][0]['quote'] = 'invented quote'
    _, _, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen, question='What?', compose_version='@29')
    assert [item['fact_index'] for item in diagnostics] == [1, 2]
    assert 'grounded_evidence_invalid' in diagnostics[1]['codes']
    final = final_for(first, action='correct')
    final['statements'][0]['date_refs'] = []
    final['statements'].append({**deepcopy(first['facts'][1]), 'first_fact_uses': [
        {'fact_index': 2, 'action': 'correct', 'reason': 'Says corrected but quote is unchanged.'}]})
    with pytest.raises(ValueError, match='grounded_evidence_invalid'):
        reviewed_scope_answer(final, catalog, first, chosen=chosen, question='What?', compose_version='@29')


def test_frozen_catalog_corruption_is_not_a_model_field_diagnostic(material):
    chosen, catalog, first = material
    corrupt = deepcopy(catalog)
    corrupt['units'][0]['text'] = 'corrupt frozen input'
    with pytest.raises(ValueError, match='first_frozen_catalog_invalid'):
        inspect_referenced_first_draft(first, corrupt, chosen=chosen, question='What?', compose_version='@29')


def test_program_note_quote_is_diagnosable_but_never_final_evidence(material):
    chosen, catalog, first = material
    first['facts'][0].update(date_refs=[])
    first['facts'][0]['evidence'][0]['quote'] = DERIVED_NOTES + ' 2023-08-14 to 2023-08-20'
    _, normalized, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen, question='What?', compose_version='@29')
    assert diagnostics[0]['issues'][0]['field_path'] == 'facts[0].evidence[0].quote'
    assert normalized.facts[0].derived_notes == []
    with pytest.raises(ValueError, match='grounded_evidence_invalid'):
        reviewed_scope_answer(final_for(first, action='correct'), catalog, first, chosen=chosen, question='What?', compose_version='@29')
    final = final_for(first, action='correct')
    final.update(statements=[], excluded_facts=[{'fact_indices': [1], 'reason': 'Excluded invalid program-note quote.'}])
    assert reviewed_scope_answer(final, catalog, first, chosen=chosen, question='What?', compose_version='@29').statements == []


def test_duplicate_diagnostic_requires_correct_or_exclude_and_final_still_rejects_duplicates(material):
    chosen, catalog, first = material
    first['facts'].append(deepcopy(first['facts'][0]))
    first['facts'][1]['date_refs'] = []
    _, _, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen, question='What?', compose_version='@29')
    issue, = diagnostics[0]['issues']
    assert issue['field_path'] == 'facts[1].text' and issue['duplicate_of'] == 1
    final = final_for(first)
    final['statements'][0]['first_fact_uses'].append({'fact_index': 2, 'action': 'merge', 'reason': 'Same proposition.'})
    with pytest.raises(ValueError, match='invalid_first_fact_requires_correction'):
        reviewed_scope_answer(final, catalog, first, chosen=chosen, question='What?', compose_version='@29')
    final['statements'][0]['first_fact_uses'][1]['action'] = 'correct'
    assert len(reviewed_scope_answer(final, catalog, first, chosen=chosen, question='What?', compose_version='@29').statements) == 1
    duplicate = deepcopy(final['statements'][0])
    duplicate['date_refs'] = []
    duplicate['first_fact_uses'] = []
    final['statements'].append(duplicate)
    with pytest.raises(ValueError, match='grounded_statement_duplicate'):
        reviewed_scope_answer(final, catalog, first, chosen=chosen, question='What?', compose_version='@29')


def test_precise_diagnostics_fit_short_unknown_remote_budget_and_select_current_default(material):
    chosen, _, first = material
    first['facts'][0].update(kind='inference', date_refs=[], text='Maya painted this before 23 August 2023.')
    messages = build_partition_messages(chosen, 'What did Maya paint?', first_output=first, compose_version='@29')
    assert 'field_path' in messages[-1]['content']
    assert 'actual final fields' in messages[0]['content']
    assert review_input_tokens(messages, final_response_model('@29', 'What?')) <= 4500
    assert {name: ACTIVE[name] for name in ('compose', 'ladder', 'retrieve', 'rerank')} == {
        'compose': '@30', 'ladder': '@15', 'retrieve': '@5', 'rerank': '@2'}
    payload = json.loads(messages[-1]['content'].split('资料：\n', 1)[1].rsplit('\n\n问题：', 1)[0])
    assert list(payload) == ['untrusted_first_fact_plan', 'first_fact_positions', 'first_fact_diagnostics', 'date_provenance', 'evidence_catalog']
    assert messages == build_partition_messages(chosen, 'What did Maya paint?', first_output=deepcopy(first), compose_version='@29')


@pytest.mark.parametrize('text', [
    'Maya painted it relative to the 23 August 2023 session date: 2023-08-14 to 2023-08-20.',
    'Maya painted it relative to the 23 August 2023 session date.',
    'Maya painted it on 2023-08-23.',
])
def test_article_anchor_requires_same_bound_anchor_and_full_computed_result(material, text):
    chosen, catalog, first = material
    first['facts'][0].update(kind='date', text=text)
    _, _, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen, question='When?', compose_version='@29')
    if '2023-08-14 to 2023-08-20' in text:
        assert diagnostics == []
        assert reviewed_scope_answer(final_for(first), catalog, first, chosen=chosen, question='When?', compose_version='@29').statements
        _, _, old = inspect_referenced_first_draft(first, catalog, chosen=chosen, question='When?', compose_version='@28')
        assert old[0]['codes'] == ['grounded_calendar_provenance_invalid']
    else:
        assert diagnostics[0]['codes'] == ['grounded_calendar_provenance_invalid']
        with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
            reviewed_scope_answer(final_for(first, action='correct'), catalog, first, chosen=chosen, question='When?', compose_version='@29')
